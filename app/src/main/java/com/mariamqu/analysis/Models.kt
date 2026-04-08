package com.mariamqu.analysis

enum class Direction {
    UP, DOWN, WAIT
}

enum class StrategyType {
    TREND,
    MOMENTUM,
    REVERSAL,
    BREAKOUT,
    VOLATILITY
}

enum class Outcome {
    WIN,
    LOSS
}

data class AnalyzerSettings(
    val roiLeft: Float = 0.12f,
    val roiTop: Float = 0.10f,
    val roiRight: Float = 0.92f,
    val roiBottom: Float = 0.88f,
    val sampleStride: Int = 6,
    val confidenceThreshold: Float = 0.46f,
    val historyWindow: Int = 12,
    val smoothing: Float = 0.72f,
    val useAutoCrop: Boolean = true,
    val colorSensitivity: Float = 1.0f
)

data class StrategyWeights(
    val trend: Float = 1.20f,
    val momentum: Float = 1.05f,
    val reversal: Float = 0.95f,
    val breakout: Float = 1.10f,
    val volatility: Float = 0.90f
) {
    fun normalized(): StrategyWeights {
        fun clamp(value: Float) = value.coerceIn(0.50f, 2.00f)
        return copy(
            trend = clamp(trend),
            momentum = clamp(momentum),
            reversal = clamp(reversal),
            breakout = clamp(breakout),
            volatility = clamp(volatility)
        )
    }
}

data class CandleSnapshot(
    val bullishScore: Float,
    val bearishScore: Float,
    val neutralScore: Float,
    val brightness: Float,
    val contrast: Float,
    val edgeDensity: Float,
    val momentum: Float,
    val structureScore: Float,
    val confidence: Float,
    val timestampMs: Long = System.currentTimeMillis()
) {
    val bias: Float get() = bullishScore - bearishScore
}

data class Signal(
    val direction: Direction,
    val confidence: Float,
    val reason: String,
    val strategyScores: Map<StrategyType, Float> = emptyMap(),
    val timestampMs: Long = System.currentTimeMillis()
)

data class RecentSignalItem(
    val direction: Direction,
    val confidence: Float,
    val reason: String,
    val outcome: Outcome? = null,
    val timestampMs: Long = System.currentTimeMillis()
)

data class Stats(
    val predictions: Int = 0,
    val wins: Int = 0,
    val losses: Int = 0,
    val accuracy: Float = 0f,
    val bestStreak: Int = 0,
    val currentStreak: Int = 0
)
