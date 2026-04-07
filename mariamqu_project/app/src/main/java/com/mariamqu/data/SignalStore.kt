package com.mariamqu.data

import android.content.Context
import androidx.core.content.edit
import com.mariamqu.analysis.Direction
import com.mariamqu.analysis.Outcome
import com.mariamqu.analysis.RecentSignalItem
import com.mariamqu.analysis.Signal
import com.mariamqu.analysis.StrategyType
import com.mariamqu.analysis.StrategyWeights
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject

class SignalStore(context: Context) {

    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private val _stats = MutableStateFlow(readStats())
    val stats: StateFlow<Stats> = _stats.asStateFlow()

    private val _weights = MutableStateFlow(readWeights())
    val weights: StateFlow<StrategyWeights> = _weights.asStateFlow()

    private val _recentSignals = MutableStateFlow(readRecentSignals())
    val recentSignals: StateFlow<List<RecentSignalItem>> = _recentSignals.asStateFlow()

    fun recordSignal(signal: Signal) {
        val predictions = prefs.getInt(KEY_PREDICTIONS, 0) + 1
        prefs.edit {
            putInt(KEY_PREDICTIONS, predictions)
            putString(KEY_LAST_SIGNAL, encodeSignal(signal))
        }
        addRecentSignal(
            RecentSignalItem(
                direction = signal.direction,
                confidence = signal.confidence,
                reason = signal.reason,
                outcome = null
            )
        )
        _stats.value = readStats()
    }

    fun markWin() {
        prefs.edit { putInt(KEY_WINS, prefs.getInt(KEY_WINS, 0) + 1) }
        updateStreak(isWin = true)
        learnFromLastSignal(Outcome.WIN)
        markLatestOutcome(Outcome.WIN)
        _stats.value = readStats()
    }

    fun markLoss() {
        prefs.edit { putInt(KEY_LOSSES, prefs.getInt(KEY_LOSSES, 0) + 1) }
        updateStreak(isWin = false)
        learnFromLastSignal(Outcome.LOSS)
        markLatestOutcome(Outcome.LOSS)
        _stats.value = readStats()
    }

    fun reset() {
        prefs.edit { clear() }
        _stats.value = readStats()
        _weights.value = readWeights()
        _recentSignals.value = emptyList()
    }

    private fun readStats(): Stats {
        val predictions = prefs.getInt(KEY_PREDICTIONS, 0)
        val wins = prefs.getInt(KEY_WINS, 0)
        val losses = prefs.getInt(KEY_LOSSES, 0)
        val bestStreak = prefs.getInt(KEY_BEST_STREAK, 0)
        val currentStreak = prefs.getInt(KEY_CURRENT_STREAK, 0)
        val evaluated = wins + losses
        val accuracy = if (evaluated > 0) (wins.toFloat() / evaluated.toFloat()) * 100f else 0f
        return Stats(
            predictions = predictions,
            wins = wins,
            losses = losses,
            accuracy = accuracy,
            bestStreak = bestStreak,
            currentStreak = currentStreak
        )
    }

    private fun readWeights(): StrategyWeights {
        return StrategyWeights(
            trend = prefs.getFloat(KEY_WEIGHT_TREND, 1.20f),
            momentum = prefs.getFloat(KEY_WEIGHT_MOMENTUM, 1.05f),
            reversal = prefs.getFloat(KEY_WEIGHT_REVERSAL, 0.95f),
            breakout = prefs.getFloat(KEY_WEIGHT_BREAKOUT, 1.10f),
            volatility = prefs.getFloat(KEY_WEIGHT_VOLATILITY, 0.90f)
        ).normalized()
    }

    private fun readRecentSignals(): List<RecentSignalItem> {
        val raw = prefs.getString(KEY_RECENT_SIGNALS, null) ?: return emptyList()
        return try {
            val array = JSONArray(raw)
            buildList {
                for (i in 0 until array.length()) {
                    val item = array.getJSONObject(i)
                    add(
                        RecentSignalItem(
                            direction = Direction.valueOf(item.getString("direction")),
                            confidence = item.getDouble("confidence").toFloat(),
                            reason = item.getString("reason"),
                            outcome = if (item.isNull("outcome")) null else Outcome.valueOf(item.getString("outcome")),
                            timestampMs = item.getLong("timestampMs")
                        )
                    )
                }
            }
        } catch (_: Throwable) {
            emptyList()
        }
    }

    private fun addRecentSignal(item: RecentSignalItem) {
        val updated = (listOf(item) + _recentSignals.value).take(12)
        _recentSignals.value = updated
        prefs.edit { putString(KEY_RECENT_SIGNALS, encodeRecentSignals(updated)) }
    }

    private fun markLatestOutcome(outcome: Outcome) {
        val updated = _recentSignals.value.toMutableList()
        if (updated.isNotEmpty()) {
            updated[0] = updated[0].copy(outcome = outcome)
            _recentSignals.value = updated
            prefs.edit { putString(KEY_RECENT_SIGNALS, encodeRecentSignals(updated)) }
        }
    }

    private fun updateStreak(isWin: Boolean) {
        val current = prefs.getInt(KEY_CURRENT_STREAK, 0)
        val best = prefs.getInt(KEY_BEST_STREAK, 0)
        val updatedCurrent = if (isWin) current + 1 else 0
        val updatedBest = maxOf(best, updatedCurrent)
        prefs.edit {
            putInt(KEY_CURRENT_STREAK, updatedCurrent)
            putInt(KEY_BEST_STREAK, updatedBest)
        }
    }

    private fun learnFromLastSignal(outcome: Outcome) {
        val lastRaw = prefs.getString(KEY_LAST_SIGNAL, null) ?: return
        val signal = decodeSignal(lastRaw) ?: return
        val directionFactor = when (signal.direction) {
            Direction.UP -> if (outcome == Outcome.WIN) 1f else -1f
            Direction.DOWN -> if (outcome == Outcome.LOSS) 1f else -1f
            Direction.WAIT -> 0f
        }
        if (directionFactor == 0f) return

        val current = _weights.value
        val next = current.copy(
            trend = current.trend + adjust(signal, StrategyType.TREND, directionFactor),
            momentum = current.momentum + adjust(signal, StrategyType.MOMENTUM, directionFactor),
            reversal = current.reversal + adjust(signal, StrategyType.REVERSAL, directionFactor),
            breakout = current.breakout + adjust(signal, StrategyType.BREAKOUT, directionFactor),
            volatility = current.volatility + adjust(signal, StrategyType.VOLATILITY, directionFactor)
        ).normalized()

        prefs.edit {
            putFloat(KEY_WEIGHT_TREND, next.trend)
            putFloat(KEY_WEIGHT_MOMENTUM, next.momentum)
            putFloat(KEY_WEIGHT_REVERSAL, next.reversal)
            putFloat(KEY_WEIGHT_BREAKOUT, next.breakout)
            putFloat(KEY_WEIGHT_VOLATILITY, next.volatility)
        }
        _weights.value = next
    }

    private fun adjust(signal: Signal, strategyType: StrategyType, directionFactor: Float): Float {
        val score = signal.strategyScores[strategyType] ?: 0f
        val confidenceFactor = (signal.confidence.coerceIn(0f, 1f) * 0.05f).coerceAtLeast(0.01f)
        return (score * directionFactor * confidenceFactor).coerceIn(-0.05f, 0.05f)
    }

    private fun encodeRecentSignals(items: List<RecentSignalItem>): String {
        val array = JSONArray()
        items.forEach { item ->
            array.put(
                JSONObject()
                    .put("direction", item.direction.name)
                    .put("confidence", item.confidence)
                    .put("reason", item.reason)
                    .put("outcome", item.outcome?.name)
                    .put("timestampMs", item.timestampMs)
            )
        }
        return array.toString()
    }

    private fun encodeSignal(signal: Signal): String {
        val scores = JSONObject()
        signal.strategyScores.forEach { (strategy, value) ->
            scores.put(strategy.name, value)
        }
        return JSONObject()
            .put("direction", signal.direction.name)
            .put("confidence", signal.confidence)
            .put("reason", signal.reason)
            .put("timestampMs", signal.timestampMs)
            .put("strategyScores", scores)
            .toString()
    }

    private fun decodeSignal(raw: String): Signal? {
        return try {
            val json = JSONObject(raw)
            val strategyScoresJson = json.optJSONObject("strategyScores")
            val strategyScores = buildMap {
                if (strategyScoresJson != null) {
                    StrategyType.entries.forEach { type ->
                        if (strategyScoresJson.has(type.name)) {
                            put(type, strategyScoresJson.getDouble(type.name).toFloat())
                        }
                    }
                }
            }
            Signal(
                direction = Direction.valueOf(json.getString("direction")),
                confidence = json.getDouble("confidence").toFloat(),
                reason = json.getString("reason"),
                strategyScores = strategyScores,
                timestampMs = json.getLong("timestampMs")
            )
        } catch (_: Throwable) {
            null
        }
    }

    companion object {
        private const val PREFS_NAME = "mariamqu_stats"
        private const val KEY_PREDICTIONS = "predictions"
        private const val KEY_WINS = "wins"
        private const val KEY_LOSSES = "losses"
        private const val KEY_BEST_STREAK = "best_streak"
        private const val KEY_CURRENT_STREAK = "current_streak"
        private const val KEY_LAST_SIGNAL = "last_signal"
        private const val KEY_RECENT_SIGNALS = "recent_signals"
        private const val KEY_WEIGHT_TREND = "weight_trend"
        private const val KEY_WEIGHT_MOMENTUM = "weight_momentum"
        private const val KEY_WEIGHT_REVERSAL = "weight_reversal"
        private const val KEY_WEIGHT_BREAKOUT = "weight_breakout"
        private const val KEY_WEIGHT_VOLATILITY = "weight_volatility"
    }
}
