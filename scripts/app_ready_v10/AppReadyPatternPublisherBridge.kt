package com.statmaker.app

import android.app.Activity
import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.os.Bundle
import android.util.Log
import org.json.JSONObject
import java.security.MessageDigest
import java.time.LocalDate
import java.time.format.DateTimeFormatter
import java.util.Locale

internal const val APP_READY_PATTERN_RULES_FINGERPRINT = "pattern-policy-v2-final-read-model-v3"
internal const val APP_READY_PATTERN_SCHEMA_VERSION = 10

internal object AppReadyPatternSchema {
    fun create(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS prepared_pattern_generation (
                generation_id TEXT NOT NULL PRIMARY KEY,
                source_fingerprint TEXT NOT NULL,
                rules_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                built_at_ms INTEGER NOT NULL,
                candidate_count INTEGER NOT NULL,
                proposal_count INTEGER NOT NULL DEFAULT 0
            )
            """.trimIndent()
        )
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS prepared_pattern_candidates (
                generation_id TEXT NOT NULL,
                competition_id TEXT NOT NULL,
                snapshot_version TEXT NOT NULL,
                selection_key TEXT NOT NULL,
                match_key TEXT NOT NULL,
                local_date TEXT NOT NULL,
                continent TEXT NOT NULL,
                country TEXT NOT NULL,
                league_code TEXT NOT NULL,
                market_family TEXT NOT NULL,
                selection_odd REAL NOT NULL,
                exact_recommendation_key TEXT NOT NULL,
                selection_score REAL NOT NULL,
                evidence_score REAL NOT NULL DEFAULT 0,
                source_order INTEGER NOT NULL DEFAULT 0,
                strict_hit_rate REAL NOT NULL,
                strict_sample INTEGER NOT NULL,
                value_tier TEXT,
                recommendation_eligible INTEGER NOT NULL,
                policy_premium_eligible INTEGER NOT NULL,
                policy_rejection_reason TEXT,
                PRIMARY KEY (generation_id, competition_id, selection_key)
            )
            """.trimIndent()
        )
        db.execSQL("CREATE INDEX IF NOT EXISTS idx_prepared_pattern_generation_ready ON prepared_pattern_generation(state, built_at_ms)")
        db.execSQL(
            """
            CREATE INDEX IF NOT EXISTS idx_prepared_pattern_candidates_scope
            ON prepared_pattern_candidates(
                generation_id, competition_id, local_date, match_key,
                recommendation_eligible, selection_odd
            )
            """.trimIndent()
        )
        db.execSQL("CREATE INDEX IF NOT EXISTS idx_prepared_pattern_candidates_rank ON prepared_pattern_candidates(generation_id, evidence_score DESC, source_order ASC)")
        db.execSQL("CREATE INDEX IF NOT EXISTS idx_prepared_pattern_candidates_competition_rank ON prepared_pattern_candidates(generation_id, competition_id, evidence_score DESC, source_order ASC)")
    }
}

internal data class AppReadyPatternPublishReport(
    val generationId: String,
    val candidateCount: Int,
    val reused: Boolean,
    val elapsedMs: Long
)

internal object AppReadyPatternPublisher {
    private const val TAG = "StatMakerAppReady"
    private val competitions = listOf("domestic", "champions_league", "europa_league", "conference_league")

    private data class Candidate(
        val competitionId: String,
        val snapshotVersion: String,
        val selectionKey: String,
        val matchKey: String,
        val localDate: String,
        val continent: String,
        val country: String,
        val league: String,
        val marketFamily: String,
        val odd: Double,
        val exactRecommendationKey: String,
        val selectionScore: Double,
        val evidenceScore: Double,
        val sourceOrder: Int,
        val strictHitRate: Double,
        val strictSample: Int,
        val valueTier: String?,
        val recommendationEligible: Boolean,
        val policyPremiumEligible: Boolean,
        val policyRejectionReason: String?
    )

    fun publish(context: Context, historyDb: StatMakerDb): AppReadyPatternPublishReport {
        val startedAt = System.nanoTime()
        Log.i(TAG, "stage=recommendations_store_helper_begin")
        val store = PreparedBettingSnapshotStore(context.applicationContext)
        Log.i(TAG, "stage=recommendations_store_helper_created")
        try {
            Log.i(TAG, "stage=recommendations_versions_begin")
            val versions = readyVersions(store)
            Log.i(TAG, "stage=recommendations_versions_complete ready=${versions.size}")
            check(versions.keys.containsAll(competitions)) {
                "Prepared source snapshots are incomplete: ${competitions.toSet() - versions.keys}"
            }
            val sourceFingerprint = sourceFingerprint(versions)
            val generationId = sha256("$sourceFingerprint|$APP_READY_PATTERN_RULES_FINGERPRINT")
            Log.i(TAG, "stage=recommendations_generation_lookup_begin generation=${generationId.take(12)}")
            existingCandidateCount(store, generationId)?.let { count ->
                Log.i(TAG, "stage=recommendations_generation_reused generation=${generationId.take(12)} candidates=$count")
                return AppReadyPatternPublishReport(generationId, count, true, elapsedMilliseconds(startedAt))
            }
            Log.i(TAG, "stage=recommendations_generation_lookup_complete generation=${generationId.take(12)}")

            Log.i(TAG, "stage=recommendations_candidates_begin generation=${generationId.take(12)}")
            val candidates = buildCandidates(context, historyDb, store, versions)
            Log.i(TAG, "stage=recommendations_candidates_complete count=${candidates.size}")
            writeGeneration(store, generationId, sourceFingerprint, candidates)
            return AppReadyPatternPublishReport(
                generationId = generationId,
                candidateCount = candidates.size,
                reused = false,
                elapsedMs = elapsedMilliseconds(startedAt)
            )
        } finally {
            store.close()
        }
    }

    private fun readyVersions(store: PreparedBettingSnapshotStore): Map<String, String> =
        store.readableDatabase.rawQuery(
            "SELECT competition_id, snapshot_version FROM prepared_snapshot_meta WHERE state='ready'",
            null
        ).use { cursor ->
            buildMap { while (cursor.moveToNext()) put(cursor.getString(0), cursor.getString(1)) }
        }

    private fun sourceFingerprint(versions: Map<String, String>): String = sha256(
        versions.toSortedMap().entries.joinToString("\n") { (competition, version) -> "$competition|$version" }
    )

    private fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8))
        .joinToString("") { byte -> "%02x".format(byte) }

    private fun existingCandidateCount(store: PreparedBettingSnapshotStore, generationId: String): Int? =
        store.readableDatabase.rawQuery(
            """
            SELECT candidate_count, rules_fingerprint
            FROM prepared_pattern_generation
            WHERE generation_id=? AND state='ready'
            LIMIT 1
            """.trimIndent(),
            arrayOf(generationId)
        ).use { cursor ->
            if (!cursor.moveToFirst()) return@use null
            cursor.getInt(0).takeIf { cursor.getString(1) == APP_READY_PATTERN_RULES_FINGERPRINT }
        }


    private data class IndexedFixture(
        val season: String,
        val date: LocalDate,
        val key: String
    )

    /**
     * Scalar maturity index used only by the off-device publisher.
     *
     * The old candidate phase called CurrentSeasonMaturityResolver once per match. That resolver
     * repeatedly queried every league roster/team history, turning ~400 upcoming matches into
     * tens of thousands of SQLite/history scans. The index below preserves the same season/date/
     * team identity semantics, but reads Domestic history and UEFA snapshots once.
     */
    private class FastMaturityIndex(
        context: Context,
        historyDb: StatMakerDb
    ) {
        private val domesticByTeam: Map<String, List<IndexedFixture>>
        private val europeanByTeam: Map<String, List<IndexedFixture>>
        private val teamKeyCache = HashMap<String, String>()
        private val dateFormatters = listOf(
            DateTimeFormatter.ISO_LOCAL_DATE,
            DateTimeFormatter.ofPattern("dd/MM/yyyy"),
            DateTimeFormatter.ofPattern("dd-MM-yyyy"),
            DateTimeFormatter.ofPattern("dd_MM_yyyy")
        )
        private val shortSeasonRangeRegex = Regex("(20\\d{2})\\s*[-_/]\\s*(\\d{2})(?!\\d)")
        private val seasonYearRegex = Regex("20\\d{2}")

        init {
            val domestic = linkedMapOf<String, MutableList<IndexedFixture>>()
            historyDb.readableDatabase.rawQuery(
                "SELECT season, division, date_text, home_team, away_team FROM matches",
                null
            ).use { cursor ->
                var rows = 0
                while (cursor.moveToNext()) {
                    rows += 1
                    val date = parseDate(cursor.getString(2)) ?: continue
                    val home = normalizedTeam(cursor.getString(3))
                    val away = normalizedTeam(cursor.getString(4))
                    if (home.isBlank() || away.isBlank()) continue
                    val fixture = IndexedFixture(
                        season = cursor.getString(0),
                        date = date,
                        key = listOf(
                            date,
                            home,
                            away,
                            cursor.getString(1)
                        ).joinToString("|")
                    )
                    domestic.getOrPut(home) { mutableListOf() }.add(fixture)
                    if (away != home) domestic.getOrPut(away) { mutableListOf() }.add(fixture)
                    if (rows % 5000 == 0) {
                        Log.i(TAG, "stage=recommendations_maturity_domestic_rows rows=$rows teams=${domestic.size}")
                    }
                }
                Log.i(TAG, "stage=recommendations_maturity_domestic_complete rows=$rows teams=${domestic.size}")
            }
            domesticByTeam = domestic.mapValues { (_, rows) -> rows.toList() }

            val european = linkedMapOf<String, MutableList<IndexedFixture>>()
            val snapshots = UefaStatsSnapshotStore(context.applicationContext)
            listOf("champions_league", "europa_league", "conference_league").forEach { competitionId ->
                snapshots.load(competitionId)?.matches.orEmpty()
                    .asSequence()
                    .filter(::isUefaCompetitive)
                    .forEach { row ->
                        val date = parseDate(row.date) ?: return@forEach
                        val home = normalizedTeam(row.homeTeam)
                        val away = normalizedTeam(row.awayTeam)
                        if (home.isBlank() || away.isBlank()) return@forEach
                        val fixture = IndexedFixture(
                            season = row.season,
                            date = date,
                            key = listOf(
                                date,
                                home,
                                away,
                                normalizedCompetition(row.competition)
                            ).joinToString("|")
                        )
                        european.getOrPut(home) { mutableListOf() }.add(fixture)
                        if (away != home) european.getOrPut(away) { mutableListOf() }.add(fixture)
                    }
            }
            europeanByTeam = european.mapValues { (_, rows) -> rows.toList() }
            Log.i(TAG, "stage=recommendations_maturity_uefa_complete teams=${europeanByTeam.size}")
        }

        fun resolve(match: OddsMatch): CurrentSeasonMatchEvidence? {
            val fixtureDate = parseDate(bettingLocalDate(match).ifBlank { match.date }) ?: return null
            return CurrentSeasonMatchEvidence(
                home = teamEvidence(match, home = true, fixtureDate = fixtureDate),
                away = teamEvidence(match, home = false, fixtureDate = fixtureDate)
            )
        }

        private fun teamEvidence(
            match: OddsMatch,
            home: Boolean,
            fixtureDate: LocalDate
        ): CurrentSeasonTeamEvidence {
            val canonical = if (home) {
                match.canonicalHomeTeam?.takeIf(String::isNotBlank) ?: match.homeTeam
            } else {
                match.canonicalAwayTeam?.takeIf(String::isNotBlank) ?: match.awayTeam
            }
            val aliases = buildSet {
                add(canonical)
                add(if (home) match.homeTeam else match.awayTeam)
                add(if (home) match.providerHomeTeam else match.providerAwayTeam)
            }.asSequence()
                .filter(String::isNotBlank)
                .map(::normalizedTeam)
                .filter(String::isNotBlank)
                .toSet()

            fun count(index: Map<String, List<IndexedFixture>>): Int {
                val keys = linkedSetOf<String>()
                aliases.forEach { alias ->
                    index[alias].orEmpty().forEach { fixture ->
                        if (
                            fixture.date.isBefore(fixtureDate) &&
                            seasonMatchesFixture(fixture.season, match.season, fixtureDate)
                        ) {
                            keys += fixture.key
                        }
                    }
                }
                return keys.size
            }

            return CurrentSeasonTeamEvidence(
                team = canonical,
                domesticMatches = count(domesticByTeam),
                europeanMatches = count(europeanByTeam)
            )
        }

        private fun isUefaCompetitive(row: ChampionsLeagueMatchStats): Boolean {
            val descriptor = "${row.competition} ${row.stage} ${row.sourceLabel}".lowercase(Locale.US)
            if (descriptor.contains("friendly") || descriptor.contains("club friendly")) return false
            return descriptor.contains("champions league") ||
                descriptor.contains("europa league") ||
                descriptor.contains("conference league") ||
                descriptor.contains("uefa")
        }

        private fun seasonMatchesFixture(
            sourceSeason: String,
            fixtureSeason: String,
            fixtureDate: LocalDate
        ): Boolean {
            val sourceYears = seasonYears(sourceSeason, fixtureDate)
            val fixtureYears = seasonYears(fixtureSeason, fixtureDate)
            return when {
                sourceYears.size == 1 -> sourceYears.first() in fixtureYears
                fixtureYears.size == 1 -> fixtureYears.first() in sourceYears
                else -> sourceYears == fixtureYears
            }
        }

        private fun seasonYears(raw: String, fallbackDate: LocalDate): Set<Int> {
            val shortRange = shortSeasonRangeRegex.find(raw)
            if (shortRange != null) {
                val first = shortRange.groupValues[1].toInt()
                return setOf(first, 2000 + shortRange.groupValues[2].toInt())
            }
            val years = seasonYearRegex.findAll(raw).map { it.value.toInt() }.toSet()
            if (years.isNotEmpty()) return years
            val digits = raw.filter(Char::isDigit)
            if (digits.length == 4 && !digits.startsWith("20")) {
                val first = 2000 + digits.take(2).toInt()
                return setOf(first, 2000 + digits.takeLast(2).toInt())
            }
            return if (fallbackDate.monthValue >= 7) {
                setOf(fallbackDate.year, fallbackDate.year + 1)
            } else {
                setOf(fallbackDate.year - 1, fallbackDate.year)
            }
        }

        private fun normalizedTeam(value: String): String =
            teamKeyCache.getOrPut(value) {
                when (val key = DomesticNormalizedStatsRepository.normalizedTeamKey(value)) {
                    "bod_glimt", "bodoe_glimt" -> "bodo_glimt"
                    "olympiacos", "olympiakos" -> "olympiakos_piraeus"
                    "aek_athens" -> "aek_athens_fc"
                    else -> key
                }
            }

        private fun normalizedCompetition(value: String): String = value.trim().lowercase(Locale.US)
            .replace(Regex("[^\\p{L}\\p{N}]+"), " ")
            .trim()

        private fun parseDate(value: String): LocalDate? {
            val text = value.trim().take(10)
            return dateFormatters.firstNotNullOfOrNull { formatter ->
                runCatching { LocalDate.parse(text, formatter) }.getOrNull()
            }
        }
    }

    private fun buildCandidates(
        context: Context,
        historyDb: StatMakerDb,
        store: PreparedBettingSnapshotStore,
        versions: Map<String, String>
    ): List<Candidate> {
        PreparedBettingDecisionRegistry.clear()
        PreparedBettingEvidenceRegistry.clear()

        Log.i(TAG, "stage=recommendations_maturity_index_begin")
        val maturityIndex = FastMaturityIndex(context, historyDb)
        Log.i(TAG, "stage=recommendations_maturity_index_complete")

        val maturityByMatch = HashMap<String, CurrentSeasonMatchEvidence?>()
        val candidates = ArrayList<Candidate>(16_384)
        var sourceOrder = 0

        competitions.forEach { competitionId ->
            val version = versions[competitionId] ?: return@forEach
            val matches = loadMinimalMatches(store, competitionId, version)
            Log.i(
                TAG,
                "stage=recommendations_match_index_complete competition=$competitionId matches=${matches.size}"
            )

            var rowCount = 0
            store.readableDatabase.rawQuery(
                """
                SELECT selection_key, match_key, local_date,
                       selection_market, selection_name, selection_team, selection_line, selection_odd,
                       category,
                       bm_hits, bm_sample, bm_hit_rate,
                       bm_market_probability, bm_market_probability_source,
                       bm_empirical_probability, bm_posterior_probability, bm_market_edge,
                       bm_sample_reliability, bm_normalized_positive_edge, bm_raw_implied_probability,
                       bm_market_overround, bm_bookmaker_margin,
                       identity_broad_group, identity_family, identity_sub_market_key,
                       identity_team_side, identity_line, identity_selection_side,
                       identity_source_market, identity_team, identity_selection_token,
                       score_value, score_tier, score_bookmaker_base,
                       score_model_adjustment, score_trend_adjustment,
                       qualifies_builder
                FROM prepared_selections
                WHERE competition_id=? AND snapshot_version=? AND qualifies_pattern=1
                """.trimIndent(),
                arrayOf(competitionId, version)
            ).use { cursor ->
                while (cursor.moveToNext()) {
                    val order = sourceOrder++
                    rowCount += 1
                    val selectionKey = cursor.getString(0)
                    val preparedMatchKey = cursor.getString(1)
                    val match = matches[preparedMatchKey] ?: continue
                    val odd = cursor.getDouble(7)

                    if (
                        cursor.isNull(9) || cursor.isNull(10) || cursor.isNull(11) ||
                        cursor.isNull(12) || cursor.isNull(15) || cursor.isNull(17) ||
                        cursor.isNull(18) || cursor.isNull(22) || cursor.isNull(31)
                    ) {
                        continue
                    }

                    val identity = runCatching {
                        NormalizedMarketIdentity(
                            broadGroup = MarketBroadGroup.valueOf(cursor.getString(22)),
                            family = cursor.getString(23),
                            subMarketKey = cursor.getString(24),
                            teamSide = cursor.getStringOrNull(25),
                            line = cursor.getDoubleOrNull(26),
                            selectionSide = MarketSelectionSide.valueOf(cursor.getString(27)),
                            sourceMarket = cursor.getString(28),
                            team = cursor.getStringOrNull(29),
                            selectionToken = cursor.getString(30)
                        )
                    }.getOrNull() ?: continue

                    val strict = StrictEvidence(
                        hits = cursor.getInt(9),
                        sample = cursor.getInt(10),
                        hitRate = cursor.getDouble(11)
                    )
                    val bookmaker = BookmakerAlignedEvidence(
                        strict = strict,
                        marketProbability = cursor.getDouble(12),
                        marketProbabilitySource = cursor.getString(13),
                        empiricalProbability = cursor.getDouble(14),
                        posteriorProbability = cursor.getDouble(15),
                        marketEdge = cursor.getDouble(16),
                        sampleReliability = cursor.getDouble(17),
                        normalizedPositiveEdge = cursor.getDouble(18),
                        rawImpliedProbability = cursor.getDouble(19),
                        marketOverround = cursor.getDoubleOrNull(20),
                        bookmakerMargin = cursor.getDoubleOrNull(21)
                    )
                    val score = BettingEvidenceScore(
                        value = cursor.getDouble(31),
                        tier = BettingEvidenceTier.valueOf(cursor.getString(32)),
                        bookmakerBase = cursor.getDouble(33),
                        modelAdjustment = cursor.getDouble(34),
                        trendAdjustment = cursor.getDouble(35)
                    )

                    val oddsSelection = OddsSelection(
                        market = cursor.getString(3),
                        selection = cursor.getString(4),
                        team = cursor.getStringOrNull(5),
                        line = cursor.getDoubleOrNull(6),
                        odd = odd
                    )
                    val selection = PatternBackedSelection(
                        match = match,
                        oddsSelection = oddsSelection,
                        category = cursor.getString(8),
                        patternSupport = "${strict.hits}/${strict.sample}",
                        hitRate = strict.hitRate,
                        sample = strict.sample,
                        reasoning = "",
                        preparedSelectionKey = selectionKey
                    )

                    PreparedBettingEvidenceRegistry.register(selection, bookmaker)
                    PreparedBettingDecisionRegistry.register(
                        selection,
                        PreparedBettingDecisionRegistry.Decision(
                            identity = identity,
                            score = score,
                            qualifiesForPattern = true,
                            qualifiesForBuilder = cursor.getInt(36) == 1
                        )
                    )

                    val eligible = selection.odd >= visibleMinimumOdd(selection) &&
                        passesPatternRecommendationGate(selection) &&
                        isSaneExactOdd(selection)
                    val sharedEvidence = SharedBettingEvidence(
                        bookmaker = bookmaker,
                        trend = null
                    )
                    val policy = if (eligible) {
                        RecommendationPolicyV2.evaluate(
                            selection = selection,
                            evidence = sharedEvidence,
                            maturity = maturityByMatch.getOrPut(selection.match.key) {
                                maturityIndex.resolve(selection.match)
                            }
                        )
                    } else null
                    val signal = BettingValueSignalPolicy.evaluate(selection, sharedEvidence)
                    val marketLabel = bettingMarketFilterLabel(identity)
                        .takeIf(String::isNotBlank)
                        ?: canonicalMarketFamilyLabel(selection.oddsSelection.market)

                    candidates += Candidate(
                        competitionId = competitionId,
                        snapshotVersion = version,
                        selectionKey = selectionKey,
                        matchKey = selection.match.key,
                        localDate = cursor.getString(2),
                        continent = sharedContinentForCountry(match.country),
                        country = match.country,
                        league = match.leagueCode.ifBlank { match.competition },
                        marketFamily = marketLabel,
                        odd = odd,
                        exactRecommendationKey = identity.exactKey,
                        selectionScore = selectionScore(
                            selection = selection,
                            strict = strict,
                            bookmaker = bookmaker
                        ),
                        evidenceScore = score.value,
                        sourceOrder = order,
                        strictHitRate = strict.hitRate,
                        strictSample = strict.sample,
                        valueTier = signal?.tier?.name,
                        recommendationEligible = eligible,
                        policyPremiumEligible = policy?.premiumEligible == true,
                        policyRejectionReason = policy?.rejectionReason?.name
                    )

                    if (rowCount % 2500 == 0) {
                        Log.i(
                            TAG,
                            "stage=recommendations_scalar_rows competition=$competitionId " +
                                "rows=$rowCount candidates=${candidates.size}"
                        )
                    }
                }
            }
            Log.i(
                TAG,
                "stage=recommendations_source_complete competition=$competitionId " +
                    "rows=$rowCount candidates=${candidates.size}"
            )
        }

        return candidates
    }

    private fun loadMinimalMatches(
        store: PreparedBettingSnapshotStore,
        competitionId: String,
        snapshotVersion: String
    ): Map<String, OddsMatch> =
        store.readableDatabase.rawQuery(
            """
            SELECT match_key, local_date, payload
            FROM prepared_matches
            WHERE competition_id=? AND snapshot_version=?
            """.trimIndent(),
            arrayOf(competitionId, snapshotVersion)
        ).use { cursor ->
            buildMap {
                while (cursor.moveToNext()) {
                    val preparedMatchKey = cursor.getString(0)
                    val localDate = cursor.getString(1)
                    val payload = cursor.getString(2)
                    val header = payload.substringBefore(",\"markets\":")
                    val jsonText = if (header.length < payload.length) "$header}" else payload
                    val json = runCatching { JSONObject(jsonText) }.getOrNull() ?: continue
                    val home = json.optString("homeTeam").trim()
                    val away = json.optString("awayTeam").trim()
                    if (home.isBlank() || away.isBlank()) continue

                    put(
                        preparedMatchKey,
                        OddsMatch(
                            id = json.optString("id"),
                            date = json.optString("date").ifBlank { localDate },
                            kickoff = json.optString("kickoff"),
                            leagueCode = json.optString("leagueCode"),
                            country = json.optString("country"),
                            competition = json.optString("competition"),
                            season = json.optString("season"),
                            providerHomeTeam = json.optString("providerHomeTeam"),
                            providerAwayTeam = json.optString("providerAwayTeam"),
                            homeTeam = home,
                            awayTeam = away,
                            canonicalHomeTeam = json.optNullableString("canonicalHomeTeam"),
                            canonicalAwayTeam = json.optNullableString("canonicalAwayTeam"),
                            homeTeamLogo = json.optNullableString("homeTeamLogo"),
                            awayTeamLogo = json.optNullableString("awayTeamLogo"),
                            teamMappingStatus = json.optString("teamMappingStatus", "matched"),
                            usableForStats = json.optBoolean("usableForStats", true),
                            markets = emptyList(),
                            venue = json.optNullableString("venue")
                        )
                    )
                }
            }
        }

    private fun JSONObject.optNullableString(name: String): String? {
        if (!has(name) || isNull(name)) return null
        return optString(name).trim().takeIf(String::isNotBlank)
    }

    private fun selectionScore(
        selection: PatternBackedSelection,
        strict: StrictEvidence,
        bookmaker: BookmakerAlignedEvidence
    ): Double {
        val familyRank = sharedMarketFamilyOrder(marketFamily(selection))
            .let { if (it <= 0) 1.0 else 1.0 / it.toDouble() }
        val priceScore = when {
            selection.odd <= 1.80 -> 0.86
            selection.odd <= 2.60 -> 1.00
            selection.odd <= 3.50 -> 0.72
            else -> 0.45
        }
        val bookmakerScore =
            bookmaker.posteriorProbability * 0.78 +
                bookmaker.sampleReliability * 0.12 +
                bookmaker.normalizedPositiveEdge * 0.10
        return bookmakerScore * 0.72 +
            priceScore * 0.08 +
            familyRank * 0.04 +
            (strict.sample.coerceAtMost(20) / 20.0) * 0.08 +
            (strict.hits.coerceAtMost(15) / 15.0) * 0.08 +
            ResultMarketPolicy.scoreBonus(selection) +
            if (ResultMarketPolicy.isDoubleChance(selection)) -0.08 else 0.0
    }

    private fun android.database.Cursor.getStringOrNull(index: Int): String? =
        if (index < 0 || isNull(index)) null else getString(index)

    private fun android.database.Cursor.getDoubleOrNull(index: Int): Double? =
        if (index < 0 || isNull(index)) null else getDouble(index)

    private fun writeGeneration(
        store: PreparedBettingSnapshotStore,
        generationId: String,
        sourceFingerprint: String,
        candidates: List<Candidate>
    ) {
        val db = store.writableDatabase
        db.beginTransaction()
        try {
            db.delete("prepared_pattern_candidates", "generation_id=?", arrayOf(generationId))
            db.delete("prepared_pattern_generation", "generation_id=?", arrayOf(generationId))
            db.insertOrThrow("prepared_pattern_generation", null, ContentValues().apply {
                put("generation_id", generationId)
                put("source_fingerprint", sourceFingerprint)
                put("rules_fingerprint", APP_READY_PATTERN_RULES_FINGERPRINT)
                put("state", "building")
                put("built_at_ms", System.currentTimeMillis())
                put("candidate_count", 0)
                put("proposal_count", 0)
            })
            candidates.forEach { candidate ->
                db.insertOrThrow("prepared_pattern_candidates", null, ContentValues().apply {
                    put("generation_id", generationId)
                    put("competition_id", candidate.competitionId)
                    put("snapshot_version", candidate.snapshotVersion)
                    put("selection_key", candidate.selectionKey)
                    put("match_key", candidate.matchKey)
                    put("local_date", candidate.localDate)
                    put("continent", candidate.continent)
                    put("country", candidate.country)
                    put("league_code", candidate.league)
                    put("market_family", candidate.marketFamily)
                    put("selection_odd", candidate.odd)
                    put("exact_recommendation_key", candidate.exactRecommendationKey)
                    put("selection_score", candidate.selectionScore)
                    put("evidence_score", candidate.evidenceScore)
                    put("source_order", candidate.sourceOrder)
                    put("strict_hit_rate", candidate.strictHitRate)
                    put("strict_sample", candidate.strictSample)
                    if (candidate.valueTier == null) putNull("value_tier") else put("value_tier", candidate.valueTier)
                    put("recommendation_eligible", if (candidate.recommendationEligible) 1 else 0)
                    put("policy_premium_eligible", if (candidate.policyPremiumEligible) 1 else 0)
                    if (candidate.policyRejectionReason == null) putNull("policy_rejection_reason")
                    else put("policy_rejection_reason", candidate.policyRejectionReason)
                })
            }
            check(db.update(
                "prepared_pattern_generation",
                ContentValues().apply {
                    put("state", "ready")
                    put("candidate_count", candidates.size)
                    put("proposal_count", 0)
                },
                "generation_id=? AND state='building'",
                arrayOf(generationId)
            ) == 1) { "Could not activate final recommendation generation" }
            db.delete("prepared_pattern_candidates", "generation_id<>?", arrayOf(generationId))
            db.delete("prepared_pattern_generation", "generation_id<>?", arrayOf(generationId))
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }
}


/**
 * Publisher-only entry point used by GitHub Actions after the expensive prepared snapshots
 * have been checkpointed. It deliberately bypasses WelcomeDataUpdater so a restart cannot
 * short-circuit on "local read model ready" before final recommendation materialization.
 */
internal class AppReadyPatternPublisherActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Thread({
            val db = StatMakerDb(applicationContext)
            try {
                Log.i("StatMakerAppReady", "stage=recommendations_resume_begin")
                val report = AppReadyPatternPublisher.publish(applicationContext, db)
                Log.i(
                    "StatMakerAppReady",
                    "stage=recommendations_complete generation=${report.generationId} " +
                        "candidates=${report.candidateCount} reused=${report.reused} " +
                        "elapsedMs=${report.elapsedMs}"
                )
            } catch (error: Throwable) {
                Log.e("StatMakerAppReady", "stage=recommendations_failed ${error.message.orEmpty()}", error)
            } finally {
                runCatching { db.close() }
                runOnUiThread { finish() }
            }
        }, "StatMaker-AppReady-Recommendations").start()
    }
}
