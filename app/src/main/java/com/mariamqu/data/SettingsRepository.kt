package com.mariamqu.data

import android.content.Context
import androidx.core.content.edit
import com.mariamqu.analysis.AnalyzerSettings
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

class SettingsRepository(context: Context) {
    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private val _settings = MutableStateFlow(readSettings())
    val settings: StateFlow<AnalyzerSettings> = _settings.asStateFlow()

    fun update(transform: (AnalyzerSettings) -> AnalyzerSettings) {
        val updated = transform(_settings.value).coerce()
        saveSettings(updated)
        _settings.value = updated
    }

    fun resetDefaults() {
        val defaults = AnalyzerSettings()
        saveSettings(defaults)
        _settings.value = defaults
    }

    private fun readSettings(): AnalyzerSettings {
        return AnalyzerSettings(
            roiLeft = prefs.getFloat(KEY_ROI_LEFT, 0.12f),
            roiTop = prefs.getFloat(KEY_ROI_TOP, 0.10f),
            roiRight = prefs.getFloat(KEY_ROI_RIGHT, 0.92f),
            roiBottom = prefs.getFloat(KEY_ROI_BOTTOM, 0.88f),
            sampleStride = prefs.getInt(KEY_SAMPLE_STRIDE, 6),
            confidenceThreshold = prefs.getFloat(KEY_CONFIDENCE_THRESHOLD, 0.46f),
            historyWindow = prefs.getInt(KEY_HISTORY_WINDOW, 12),
            smoothing = prefs.getFloat(KEY_SMOOTHING, 0.72f),
            useAutoCrop = prefs.getBoolean(KEY_AUTO_CROP, true),
            colorSensitivity = prefs.getFloat(KEY_COLOR_SENSITIVITY, 1.0f)
        ).coerce()
    }

    private fun saveSettings(settings: AnalyzerSettings) {
        prefs.edit {
            putFloat(KEY_ROI_LEFT, settings.roiLeft)
            putFloat(KEY_ROI_TOP, settings.roiTop)
            putFloat(KEY_ROI_RIGHT, settings.roiRight)
            putFloat(KEY_ROI_BOTTOM, settings.roiBottom)
            putInt(KEY_SAMPLE_STRIDE, settings.sampleStride)
            putFloat(KEY_CONFIDENCE_THRESHOLD, settings.confidenceThreshold)
            putInt(KEY_HISTORY_WINDOW, settings.historyWindow)
            putFloat(KEY_SMOOTHING, settings.smoothing)
            putBoolean(KEY_AUTO_CROP, settings.useAutoCrop)
            putFloat(KEY_COLOR_SENSITIVITY, settings.colorSensitivity)
        }
    }

    private fun AnalyzerSettings.coerce(): AnalyzerSettings {
        val left = roiLeft.coerceIn(0.02f, 0.75f)
        val top = roiTop.coerceIn(0.02f, 0.75f)
        val right = roiRight.coerceIn(left + 0.05f, 0.98f)
        val bottom = roiBottom.coerceIn(top + 0.05f, 0.98f)
        return copy(
            roiLeft = left,
            roiTop = top,
            roiRight = right,
            roiBottom = bottom,
            sampleStride = sampleStride.coerceIn(2, 16),
            confidenceThreshold = confidenceThreshold.coerceIn(0.15f, 0.85f),
            historyWindow = historyWindow.coerceIn(4, 30),
            smoothing = smoothing.coerceIn(0.10f, 0.95f),
            colorSensitivity = colorSensitivity.coerceIn(0.5f, 2.0f)
        )
    }

    companion object {
        private const val PREFS_NAME = "mariamqu_settings"
        private const val KEY_ROI_LEFT = "roi_left"
        private const val KEY_ROI_TOP = "roi_top"
        private const val KEY_ROI_RIGHT = "roi_right"
        private const val KEY_ROI_BOTTOM = "roi_bottom"
        private const val KEY_SAMPLE_STRIDE = "sample_stride"
        private const val KEY_CONFIDENCE_THRESHOLD = "confidence_threshold"
        private const val KEY_HISTORY_WINDOW = "history_window"
        private const val KEY_SMOOTHING = "smoothing"
        private const val KEY_AUTO_CROP = "auto_crop"
        private const val KEY_COLOR_SENSITIVITY = "color_sensitivity"
    }
}
