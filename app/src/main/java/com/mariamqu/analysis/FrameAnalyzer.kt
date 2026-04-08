package com.mariamqu.analysis

import android.graphics.Bitmap
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

class FrameAnalyzer {

    fun analyze(
        bitmap: Bitmap,
        settings: AnalyzerSettings,
        previous: CandleSnapshot? = null
    ): CandleSnapshot {
        val region = cropRegion(bitmap, settings)
        val scaled = if (region.width > 180 || region.height > 180) {
            Bitmap.createScaledBitmap(region, 180, 180, true)
        } else {
            region
        }

        val stride = settings.sampleStride.coerceIn(2, 16)
        val sensitivity = settings.colorSensitivity.coerceIn(0.5f, 2.0f)
        val greenThreshold = (16f * sensitivity).toInt().coerceAtLeast(10)
        val redThreshold = (16f * sensitivity).toInt().coerceAtLeast(10)

        var bullish = 0f
        var bearish = 0f
        var neutral = 0f
        var total = 0f
        var sumLuma = 0f
        var sumLumaSq = 0f
        var edgeCount = 0f
        var gradientCount = 0f
        val previousRowLuma = FloatArray(scaled.width)

        for (y in 0 until scaled.height step stride) {
            var lastLuma = -1f
            for (x in 0 until scaled.width step stride) {
                val color = scaled.getPixel(x, y)
                val r = android.graphics.Color.red(color)
                val g = android.graphics.Color.green(color)
                val b = android.graphics.Color.blue(color)
                val luma = (0.299f * r) + (0.587f * g) + (0.114f * b)
                val maxChannel = max(r, max(g, b))
                val minChannel = min(r, min(g, b))
                val saturationProxy = maxChannel - minChannel

                sumLuma += luma
                sumLumaSq += luma * luma
                total += 1f

                if (saturationProxy < 24) {
                    neutral += 1f
                } else if (g > r + greenThreshold && g > b + 8) {
                    bullish += 1f
                } else if (r > g + redThreshold && r > b + 8) {
                    bearish += 1f
                } else {
                    neutral += 1f
                }

                if (lastLuma >= 0f && abs(luma - lastLuma) > 14f) {
                    edgeCount += 1f
                }
                if (previousRowLuma[x] > 0f && abs(luma - previousRowLuma[x]) > 14f) {
                    gradientCount += 1f
                }
                previousRowLuma[x] = luma
                lastLuma = luma
            }
        }

        if (total <= 0f) {
            return CandleSnapshot(0f, 0f, 1f, 0.5f, 0.2f, 0.05f, 0f, 0.1f, 0.1f)
        }

        val bullishScore = bullish / total
        val bearishScore = bearish / total
        val neutralScore = neutral / total
        val meanLuma = sumLuma / total
        val variance = ((sumLumaSq / total) - (meanLuma * meanLuma)).coerceAtLeast(0f)
        val contrast = (sqrt(variance) / 255f).coerceIn(0f, 1f)
        val brightness = (meanLuma / 255f).coerceIn(0f, 1f)
        val edgeDensity = ((edgeCount + gradientCount) / (total * 1.2f)).coerceIn(0f, 1f)

        val previousBias = previous?.bias ?: 0f
        val currentBias = bullishScore - bearishScore
        val momentum = ((currentBias * 0.78f) + (previousBias * (1f - settings.smoothing) * 0.45f)).coerceIn(-1f, 1f)
        val structureScore = ((contrast * 0.62f) + (edgeDensity * 0.38f)).coerceIn(0f, 1f)
        val confidence = ((abs(currentBias) * 1.35f) + (1f - neutralScore) * 0.22f + structureScore * 0.35f)
            .coerceIn(0f, 1f)

        if (scaled !== region) {
            scaled.recycle()
        }
        if (region !== bitmap) {
            region.recycle()
        }

        return CandleSnapshot(
            bullishScore = bullishScore,
            bearishScore = bearishScore,
            neutralScore = neutralScore,
            brightness = brightness,
            contrast = contrast,
            edgeDensity = edgeDensity,
            momentum = momentum,
            structureScore = structureScore,
            confidence = confidence
        )
    }

    private fun cropRegion(bitmap: Bitmap, settings: AnalyzerSettings): Bitmap {
        val width = bitmap.width
        val height = bitmap.height

        if (!settings.useAutoCrop) {
            return Bitmap.createBitmap(bitmap, 0, 0, width, height)
        }

        val left = (width * settings.roiLeft).toInt().coerceIn(0, width - 1)
        val top = (height * settings.roiTop).toInt().coerceIn(0, height - 1)
        val right = (width * settings.roiRight).toInt().coerceIn(left + 1, width)
        val bottom = (height * settings.roiBottom).toInt().coerceIn(top + 1, height)

        return Bitmap.createBitmap(bitmap, left, top, right - left, bottom - top)
    }
}
