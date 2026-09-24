package com.statmaker.app

import java.io.File
import java.time.LocalDate
import java.util.Base64

// I/O-only adapters for production modelPerformanceConfidenceContext; no formula is reimplemented.
internal data class InputMatch(val competition: String, val leagueCode: String)
internal data class InputSelection(val market: String)
internal data class PatternBackedSelection(val odd: Double, val match: InputMatch, val oddsSelection: InputSelection, val identity: NormalizedMarketIdentity?)
internal data class OpponentInput(val modelProbability: Double)
internal data class BookmakerInput(val posteriorProbability: Double, val marketProbability: Double)
internal data class SharedBettingEvidence(val opponentAdjusted: OpponentInput?, val bookmaker: BookmakerInput)
internal fun normalizedMarketIdentity(selection: PatternBackedSelection): NormalizedMarketIdentity? = selection.identity

private fun cells(line: String): List<String?> = line.split('\t').map {
    if (it == "~") null else String(Base64.getDecoder().decode(it), Charsets.UTF_8)
}

fun main(args: Array<String>) {
    require(args.size == 3) { "entries.tsv contexts.tsv asOfDate" }
    val today = LocalDate.parse(args[2])
    val entries = File(args[0]).readLines().filter(String::isNotEmpty).map { line ->
        val c = cells(line)
        require(c.size == 15)
        ModelPerformanceEntry(
            id=c[0]!!, createdAt=0L, matchKey=c[0]!!, matchDate=c[1]!!,
            competition=c[2]!!, leagueCode=c[3]!!, season="", homeTeam="", awayTeam="",
            market=c[4]!!, selectionText="", team=null, line=null, odd=c[5]!!.toDouble(),
            broadGroup=null, family=c[6], subMarketKey=c[7], teamSide=null, selectionSide=null,
            selectionToken=null, marketProbability=null, modelProbability=c[8]?.toDouble(),
            edge=c[9]?.toDouble(), reliability=null, valueTier=c[10]!!,
            outcome=MyBetLegOutcome.valueOf(c[11]!!), settlementReturn=c[12]?.toDouble(),
            settledAt=null, predictionSource=c[13], sourceKind=c[14]!!
        )
    }
    val snapshot = ModelPerformanceConfidenceSnapshot.fromEntries(entries, today)
    File(args[1]).forEachLine { line ->
        if (line.isNotEmpty()) {
            val c = cells(line)
            require(c.size == 9)
            val identity = c[1]?.let { NormalizedMarketIdentity(MarketBroadGroup.GOALS, it, "", null, null, MarketSelectionSide.UNKNOWN, c[2]!!, null) }
            val selection = PatternBackedSelection(c[3]!!.toDouble(), InputMatch(c[7]!!, c[8]!!), InputSelection(c[2]!!), identity)
            val evidence = c[5]?.let { posterior -> SharedBettingEvidence(c[4]?.let { OpponentInput(it.toDouble()) }, BookmakerInput(posterior.toDouble(), c[6]!!.toDouble())) }
            val context = modelPerformanceConfidenceContext(selection, evidence)
            val result = snapshot.classify(context)
            println(listOf(c[0]!!, result.tier.name, result.score, result.support,
                result.positiveSignals, result.negativeSignals).joinToString("\t"))
        }
    }
}
