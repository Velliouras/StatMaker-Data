package com.statmaker.app

import android.app.Activity
import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.os.Bundle
import android.util.Log
import java.security.MessageDigest

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

    private data class SourceSelection(
        val competitionId: String,
        val snapshotVersion: String,
        val selection: PatternBackedSelection,
        val sourceOrder: Int
    )

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
            val candidates = buildCandidates(context, historyDb, loadSources(store, versions))
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

    private fun loadSources(
        store: PreparedBettingSnapshotStore,
        versions: Map<String, String>
    ): List<SourceSelection> = buildList {
        var sourceOrder = 0
        competitions.forEach { competitionId ->
            val version = versions[competitionId] ?: return@forEach
            val selections = store.loadAllPatternSelectionsForPublisher(
                competitionId = competitionId,
                snapshotVersion = version
            )?.selections.orEmpty()
            Log.i(
                TAG,
                "stage=recommendations_source_loaded competition=$competitionId " +
                    "snapshot=${version.take(12)} selections=${selections.size}"
            )
            selections.forEach { add(SourceSelection(competitionId, version, it, sourceOrder++)) }
        }
    }

    private fun buildCandidates(
        context: Context,
        historyDb: StatMakerDb,
        sources: List<SourceSelection>
    ): List<Candidate> {
        val allSelections = sources.map(SourceSelection::selection)
        val evidenceByKey = sharedBettingEvidenceBatch(allSelections)
        val leagues = DomesticApiRegistry.bundledLeagueSources()
        val maturityResolver = CurrentSeasonMaturityResolver(
            context = context,
            db = historyDb,
            leagueSources = { leagues },
            primaryLeagueForMatch = { match ->
                val code = DomesticApiRegistry.normalizeLeagueCode(match.leagueCode)
                leagues.firstOrNull { DomesticApiRegistry.normalizeLeagueCode(it.code) == code }
            }
        )
        val maturityByMatch = HashMap<String, CurrentSeasonMatchEvidence?>()

        return sources.map { source ->
            val selection = source.selection
            val evidence = evidenceByKey[PatternTrendCoverageAudit.selectionKey(selection)]
            val strict = strictEvidence(selection)
            val identity = normalizedMarketIdentity(selection)
            val signal = BettingValueFirstRanking.signal(selection, evidenceByKey)
            val eligible = selection.odd >= visibleMinimumOdd(selection) &&
                passesPatternRecommendationGate(selection) && identity != null && isSaneExactOdd(selection)
            val policy = if (eligible) {
                RecommendationPolicyV2.evaluate(
                    selection = selection,
                    evidence = evidence,
                    maturity = maturityByMatch.getOrPut(selection.match.key) {
                        maturityResolver.resolve(selection.match)
                    }
                )
            } else null
            val marketLabel = identity?.let(::bettingMarketFilterLabel)?.takeIf(String::isNotBlank)
                ?: canonicalMarketFamilyLabel(selection.oddsSelection.market)
            val evidenceScore = BettingEvidenceScorer.evaluate(selection)?.value ?: 0.0

            Candidate(
                competitionId = source.competitionId,
                snapshotVersion = source.snapshotVersion,
                selectionKey = preparedSelectionKey(selection),
                matchKey = selection.match.key,
                localDate = bettingLocalDate(selection.match),
                continent = sharedContinentForCountry(selection.match.country),
                country = selection.match.country,
                league = selection.match.leagueCode.ifBlank { selection.match.competition },
                marketFamily = marketLabel,
                odd = selection.odd,
                exactRecommendationKey = exactRecommendationKey(selection),
                selectionScore = selectionScore(selection),
                evidenceScore = evidenceScore,
                sourceOrder = source.sourceOrder,
                strictHitRate = strict?.hitRate ?: 0.0,
                strictSample = strict?.sample ?: 0,
                valueTier = signal?.tier?.name,
                recommendationEligible = eligible,
                policyPremiumEligible = policy?.premiumEligible == true,
                policyRejectionReason = policy?.rejectionReason?.name
            )
        }
    }

    private fun exactRecommendationKey(selection: PatternBackedSelection): String {
        val identity = normalizedMarketIdentity(selection)
        return identity?.exactKey ?: listOf(
            selection.oddsSelection.market,
            selection.oddsSelection.selection,
            selection.oddsSelection.line?.toString().orEmpty(),
            selection.oddsSelection.team.orEmpty()
        ).joinToString("|")
    }

    private fun selectionScore(selection: PatternBackedSelection): Double {
        val evidence = strictEvidence(selection) ?: return 0.0
        val familyRank = sharedMarketFamilyOrder(marketFamily(selection)).let { if (it <= 0) 1.0 else 1.0 / it.toDouble() }
        val priceScore = when {
            selection.odd <= 1.80 -> 0.86
            selection.odd <= 2.60 -> 1.00
            selection.odd <= 3.50 -> 0.72
            else -> 0.45
        }
        val bookmakerScore = bookmakerSelectionScore(selection)
        return bookmakerScore * 0.72 +
            priceScore * 0.08 +
            familyRank * 0.04 +
            (evidence.sample.coerceAtMost(20) / 20.0) * 0.08 +
            (evidence.hits.coerceAtMost(15) / 15.0) * 0.08 +
            ResultMarketPolicy.scoreBonus(selection) +
            if (ResultMarketPolicy.isDoubleChance(selection)) -0.08 else 0.0
    }

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
