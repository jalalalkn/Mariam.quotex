package com.mariamqu.analysis

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.sign

class SignalEngine(
    private val defaultWindowSize: Int = 12
) {
    private val history = ArrayDeque<CandleSnapshot>()

    fun push(snapshot: CandleSnapshot, weights: StrategyWeights): Signal {
        history.addLast(snapshot)
        while (history.size > defaultWindowSize) {
            history.removeFirst()
        }

        val ordered = history.toList()
        val biasSeries = ordered.map { it.bias }
        val momentumSeries = ordered.map { it.momentum }

        val trendScore = weightedAverage(biasSeries)
        val momentumScore = weightedAverage(momentumSeries)
        val reversalScore = reversalScore(ordered)
        val breakoutScore = breakoutScore(snapshot, ordered)
        val volatilityScore = volatilityScore(snapshot, ordered)

        val strategyScores = mapOf(
            StrategyType.TREND to trendScore,
            StrategyType.MOMENTUM to momentumScore,
            StrategyType.REVERSAL to reversalScore,
            StrategyType.BREAKOUT to breakoutScore,
            StrategyType.VOLATILITY to volatilityScore
        )

        val upForce =
            positive(trendScore) * weights.trend +
                positive(momentumScore) * weights.momentum +
                positive(reversalScore) * weights.reversal +
                positive(breakoutScore) * weights.breakout +
                positive(volatilityScore) * weights.volatility

        val downForce =
            positive(-trendScore) * weights.trend +
                positive(-momentumScore) * weights.momentum +
                positive(-reversalScore) * weights.reversal +
                positive(-breakoutScore) * weights.breakout +
                positive(-volatilityScore) * weights.volatility

        val raw = upForce - downForce
        val agreement = agreementScore(strategyScores)
        val threshold = 0.08f + (1f - snapshot.confidence) * 0.06f

        val direction = when {
            raw > threshold -> Direction.UP
            raw < -threshold -> Direction.DOWN
            else -> Direction.WAIT
        }

        val topStrategy = strategyScores.maxBy { abs(it.value) }.key
        val confidence = ((abs(raw) * 0.78f) + (snapshot.confidence * 0.20f) + (agreement * 0.35f))
            .coerceIn(0f, 1f)

        val reason = buildReason(direction, topStrategy, snapshot, trendScore, momentumScore)

        return Signal(
            direction = direction,
            confidence = confidence,
            reason = reason,
            strategyScores = strategyScores
        )
    }

    fun reset() {
        history.clear()
    }

    private fun buildReason(
        direction: Direction,
        topStrategy: StrategyType,
        snapshot: CandleSnapshot,
        trendScore: Float,
        momentumScore: Float
    ): String {
        val biasDescription = when {
            snapshot.bias > 0.10f -> "buyers dominate"
            snapshot.bias < -0.10f -> "sellers dominate"
            else -> "the chart is balanced"
        }
        val topText = when (topStrategy) {
            StrategyType.TREND -> "trend flow"
            StrategyType.MOMENTUM -> "momentum"
            StrategyType.REVERSAL -> "reversal pressure"
            StrategyType.BREAKOUT -> "breakout pressure"
            StrategyType.VOLATILITY -> "volatility structure"
        }
        return when (direction) {
            Direction.UP -> "$biasDescription, $topText points UP (trend ${formatScore(trendScore)}, momentum ${formatScore(momentumScore)})"
            Direction.DOWN -> "$biasDescription, $topText points DOWN (trend ${formatScore(trendScore)}, momentum ${formatScore(momentumScore)})"
            Direction.WAIT -> "No clean edge yet; $topText is not strong enough to commit"
        }
    }

    private fun reversalScore(history: List<CandleSnapshot>): Float {
        if (history.size < 4) return 0f
        val recent = history.takeLast(3).map { it.bias }.average().toFloat()
        val older = history.dropLast(2).takeLast(max(2, history.size / 2)).map { it.bias }.average().toFloat()
        val delta = recent - older
        return when {
            older > 0.09f && recent < -0.03f -> -0.85f
            older < -0.09f && recent > 0.03f -> 0.85f
            abs(delta) < 0.02f -> 0f
            else -> (-older * 0.4f + delta * 1.2f).coerceIn(-1f, 1f)
        }
    }

    private fun breakoutScore(snapshot: CandleSnapshot, history: List<CandleSnapshot>): Float {
        val avgStructure = history.map { it.structureScore }.average().toFloat().coerceIn(0f, 1f)
        return when {
            snapshot.structureScore > 0.58f && abs(snapshot.bias) > 0.08f -> snapshot.bias.sign * (snapshot.structureScore + snapshot.edgeDensity * 0.5f)
            snapshot.edgeDensity > (avgStructure + 0.12f) && abs(snapshot.momentum) > 0.05f -> snapshot.momentum.sign * (snapshot.edgeDensity + 0.2f)
            else -> 0f
        }.coerceIn(-1f, 1f)
    }

    private fun volatilityScore(snapshot: CandleSnapshot, history: List<CandleSnapshot>): Float {
        val averageVolatility = history.map { it.structureScore }.average().toFloat().coerceIn(0f, 1f)
        return when {
            snapshot.structureScore < 0.18f -> 0f
            snapshot.structureScore > averageVolatility + 0.10f && abs(snapshot.bias) < 0.06f -> snapshot.momentum.sign * 0.35f
            else -> snapshot.bias * 0.25f
        }.coerceIn(-1f, 1f)
    }

    private fun weightedAverage(values: List<Float>): Float {
        if (values.isEmpty()) return 0f
        var totalWeight = 0f
        var weightedSum = 0f
        values.forEachIndexed { index, value ->
            val weight = (index + 1).toFloat()
            totalWeight += weight
            weightedSum += value * weight
        }
        return (weightedSum / totalWeight).coerceIn(-1f, 1f)
    }

    private fun agreementScore(strategyScores: Map<StrategyType, Float>): Float {
        if (strategyScores.isEmpty()) return 0f
        val agreeing = strategyScores.count { it.value > 0f } + strategyScores.count { it.value < 0f }
        val intensity = strategyScores.values.map { abs(it) }.average().toFloat()
        return ((agreeing.toFloat() / strategyScores.size.toFloat()) * 0.55f + intensity * 0.45f).coerceIn(0f, 1f)
    }

    private fun positive(value: Float): Float = max(0f, value)

    private fun formatScore(value: Float): String = "%.2f"
        .format(value)
        .replace(",", ".")
}
