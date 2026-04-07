package com.mariamqu

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.pm.PackageManager
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Analytics
import androidx.compose.material.icons.filled.PauseCircle
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.RestartAlt
import androidx.compose.material.icons.filled.Savings
import androidx.compose.material.icons.filled.Slideshow
import androidx.compose.material.icons.filled.Speed
import androidx.compose.material.icons.filled.ThumbDown
import androidx.compose.material.icons.filled.ThumbUp
import androidx.compose.material.icons.filled.Tune
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Divider
import androidx.compose.material3.ElevatedCard
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.mariamqu.analysis.AnalyzerSettings
import com.mariamqu.analysis.CandleSnapshot
import com.mariamqu.analysis.Direction
import com.mariamqu.analysis.FrameAnalyzer
import com.mariamqu.analysis.RecentSignalItem
import com.mariamqu.analysis.Signal
import com.mariamqu.analysis.SignalEngine
import com.mariamqu.analysis.Stats
import com.mariamqu.analysis.StrategyType
import com.mariamqu.analysis.StrategyWeights
import com.mariamqu.capture.FrameBus
import com.mariamqu.capture.ScreenCaptureService
import com.mariamqu.data.SettingsRepository
import com.mariamqu.data.SignalStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Locale

class MainActivity : ComponentActivity() {

    private lateinit var captureLauncher: androidx.activity.result.ActivityResultLauncher<Intent>
    private lateinit var notificationPermissionLauncher: androidx.activity.result.ActivityResultLauncher<String>

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        notificationPermissionLauncher = registerForActivityResult(ActivityResultContracts.RequestPermission()) { _ ->
            requestCapturePermission()
        }

        captureLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            if (result.resultCode == Activity.RESULT_OK && result.data != null) {
                val captureIntent = Intent(this, ScreenCaptureService::class.java).apply {
                    putExtra(ScreenCaptureService.EXTRA_RESULT_CODE, result.resultCode)
                    putExtra(ScreenCaptureService.EXTRA_DATA_INTENT, result.data)
                    putExtra(ScreenCaptureService.EXTRA_WIDTH, screenWidth())
                    putExtra(ScreenCaptureService.EXTRA_HEIGHT, screenHeight())
                    putExtra(ScreenCaptureService.EXTRA_DENSITY, densityDpi())
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    startForegroundService(captureIntent)
                } else {
                    startService(captureIntent)
                }
            }
        }

        setContent {
            val context = LocalContext.current
            val vm: MainViewModel = viewModel(factory = MainViewModel.factory(context.applicationContext))
            MariamQuAppScreen(
                viewModel = vm,
                captureRunning = FrameBus.running.collectAsStateWithLifecycle().value,
                onStartCapture = { ensurePermissionsAndRequestCapture() },
                onStopCapture = { stopCaptureService() }
            )
        }
    }

    private fun ensurePermissionsAndRequestCapture() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            return
        }
        requestCapturePermission()
    }

    private fun requestCapturePermission() {
        val projectionManager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        captureLauncher.launch(projectionManager.createScreenCaptureIntent())
    }

    private fun stopCaptureService() {
        val stopIntent = Intent(this, ScreenCaptureService::class.java).apply {
            action = ScreenCaptureService.ACTION_STOP
        }
        startService(stopIntent)
    }

    private fun screenWidth(): Int {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val metrics = windowManager.currentWindowMetrics
            metrics.bounds.width()
        } else {
            @Suppress("DEPRECATION")
            resources.displayMetrics.widthPixels
        }
    }

    private fun screenHeight(): Int {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val metrics = windowManager.currentWindowMetrics
            metrics.bounds.height()
        } else {
            @Suppress("DEPRECATION")
            resources.displayMetrics.heightPixels
        }
    }

    private fun densityDpi(): Int = resources.displayMetrics.densityDpi
}

class MainViewModel(
    private val appContext: Context
) : ViewModel() {

    private val settingsRepository = SettingsRepository(appContext)
    private val signalStore = SignalStore(appContext)
    private val analyzer = FrameAnalyzer()
    private val signalEngine = SignalEngine()

    private val _latestSnapshot = MutableStateFlow<CandleSnapshot?>(null)
    val latestSnapshot: StateFlow<CandleSnapshot?> = _latestSnapshot.asStateFlow()

    private val _latestSignal = MutableStateFlow<Signal?>(null)
    val latestSignal: StateFlow<Signal?> = _latestSignal.asStateFlow()

    private val _banner = MutableStateFlow("Ready to analyze the chart")
    val banner: StateFlow<String> = _banner.asStateFlow()

    private var collectJob: Job? = null
    private var previousSnapshot: CandleSnapshot? = null
    private var lastRecordedDirection: Direction? = null
    private var lastRecordedAtMs: Long = 0L

    init {
        startCollectingFrames()
    }

    val settings: StateFlow<AnalyzerSettings> = settingsRepository.settings
    val stats: StateFlow<Stats> = signalStore.stats
    val weights: StateFlow<StrategyWeights> = signalStore.weights
    val recentSignals: StateFlow<List<RecentSignalItem>> = signalStore.recentSignals

    private fun startCollectingFrames() {
        collectJob?.cancel()
        collectJob = viewModelScope.launch(Dispatchers.Default) {
            FrameBus.frames.collectLatest { bitmap ->
                try {
                    val settings = settingsRepository.settings.value
                    val snapshot = analyzer.analyze(bitmap, settings, previousSnapshot)
                    previousSnapshot = snapshot
                    val signal = signalEngine.push(snapshot, signalStore.weights.value)

                    _latestSnapshot.value = snapshot
                    _latestSignal.value = signal
                    _banner.value = when (signal.direction) {
                        Direction.UP -> "Bullish structure detected"
                        Direction.DOWN -> "Bearish structure detected"
                        Direction.WAIT -> "Waiting for a cleaner edge"
                    }

                    if (shouldRecordSignal(signal, settings)) {
                        signalStore.recordSignal(signal)
                        lastRecordedDirection = signal.direction
                        lastRecordedAtMs = signal.timestampMs
                    }
                } catch (t: Throwable) {
                    _banner.value = "Analysis paused: ${t.message ?: "unexpected error"}"
                } finally {
                    bitmap.recycle()
                }
            }
        }
    }

    private fun shouldRecordSignal(signal: Signal, settings: AnalyzerSettings): Boolean {
        if (signal.direction == Direction.WAIT) return false
        if (signal.confidence < settings.confidenceThreshold) return false
        val directionChanged = signal.direction != lastRecordedDirection
        val cooldownPassed = (System.currentTimeMillis() - lastRecordedAtMs) > 6000L
        return directionChanged || cooldownPassed
    }

    fun updateSettings(transform: (AnalyzerSettings) -> AnalyzerSettings) {
        settingsRepository.update(transform)
    }

    fun resetSettings() {
        settingsRepository.resetDefaults()
    }

    fun resetAll() {
        signalEngine.reset()
        signalStore.reset()
        previousSnapshot = null
        lastRecordedDirection = null
        lastRecordedAtMs = 0L
        _latestSignal.value = null
        _latestSnapshot.value = null
        _banner.value = "Ready to analyze the chart"
    }

    fun markWin() {
        signalStore.markWin()
        _banner.value = "Win logged and weights adjusted"
    }

    fun markLoss() {
        signalStore.markLoss()
        _banner.value = "Loss logged and weights adjusted"
    }

    companion object {
        fun factory(context: Context): ViewModelProvider.Factory {
            return object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T {
                    return MainViewModel(context.applicationContext) as T
                }
            }
        }
    }
}

@Composable
fun MariamQuAppScreen(
    viewModel: MainViewModel,
    captureRunning: Boolean,
    onStartCapture: () -> Unit,
    onStopCapture: () -> Unit
) {
    val settings by viewModel.settings.collectAsStateWithLifecycle()
    val latestSnapshot by viewModel.latestSnapshot.collectAsStateWithLifecycle()
    val latestSignal by viewModel.latestSignal.collectAsStateWithLifecycle()
    val stats by viewModel.stats.collectAsStateWithLifecycle()
    val weights by viewModel.weights.collectAsStateWithLifecycle()
    val recentSignals by viewModel.recentSignals.collectAsStateWithLifecycle()
    val banner by viewModel.banner.collectAsStateWithLifecycle()

    MaterialTheme(colorScheme = pastelPinkScheme()) {
        Surface(modifier = Modifier.fillMaxSize()) {
            LazyColumn(
                modifier = Modifier
                    .fillMaxSize()
                    .background(MaterialTheme.colorScheme.background),
                contentPadding = PaddingValues(16.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                item {
                    HeroCard(
                        captureRunning = captureRunning,
                        banner = banner,
                        latestSignal = latestSignal,
                        onStartCapture = onStartCapture,
                        onStopCapture = onStopCapture
                    )
                }
                item {
                    ControlCard(
                        captureRunning = captureRunning,
                        onStartCapture = onStartCapture,
                        onStopCapture = onStopCapture,
                        onReset = viewModel::resetAll,
                        onMarkWin = viewModel::markWin,
                        onMarkLoss = viewModel::markLoss
                    )
                }
                item {
                    CalibrationCard(
                        settings = settings,
                        onSettingsChange = viewModel::updateSettings,
                        onResetDefaults = viewModel::resetSettings
                    )
                }
                item {
                    SignalOverviewCard(
                        snapshot = latestSnapshot,
                        signal = latestSignal,
                        stats = stats
                    )
                }
                item {
                    StrategyCard(
                        signal = latestSignal,
                        weights = weights
                    )
                }
                item {
                    PerformanceCard(
                        stats = stats,
                        recentSignals = recentSignals
                    )
                }
                item {
                    FooterCard()
                }
            }
        }
    }
}

@Composable
private fun HeroCard(
    captureRunning: Boolean,
    banner: String,
    latestSignal: Signal?,
    onStartCapture: () -> Unit,
    onStopCapture: () -> Unit
) {
    ElevatedCard(shape = RoundedCornerShape(30.dp)) {
        Column(modifier = Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Slideshow, contentDescription = null)
                Spacer(Modifier.width(10.dp))
                Column(Modifier.weight(1f)) {
                    Text("mariamqu", style = MaterialTheme.typography.headlineLarge, fontWeight = FontWeight.Black)
                    Text("Pastel screen analyzer for live chart reading", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }

            Text(banner, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)

            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                AssistChip(
                    onClick = {},
                    label = { Text(if (captureRunning) "Capture ON" else "Capture OFF") },
                    leadingIcon = { Icon(if (captureRunning) Icons.Default.PlayArrow else Icons.Default.PauseCircle, null) }
                )
                AssistChip(
                    onClick = {},
                    label = { Text(latestSignal?.direction?.name ?: "WAIT") },
                    leadingIcon = { Icon(Icons.Default.Analytics, null) }
                )
            }

            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                FilledTonalButton(onClick = onStartCapture, shape = RoundedCornerShape(18.dp)) {
                    Icon(Icons.Default.PlayArrow, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text("Start")
                }
                OutlinedButton(onClick = onStopCapture, shape = RoundedCornerShape(18.dp)) {
                    Icon(Icons.Default.PauseCircle, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text("Stop")
                }
            }
        }
    }
}

@Composable
private fun ControlCard(
    captureRunning: Boolean,
    onStartCapture: () -> Unit,
    onStopCapture: () -> Unit,
    onReset: () -> Unit,
    onMarkWin: () -> Unit,
    onMarkLoss: () -> Unit
) {
    OutlinedCard(shape = RoundedCornerShape(30.dp)) {
        Column(modifier = Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("Control center", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            Text(
                if (captureRunning) "Screen capture is active. Keep Quotex on one side and mariamqu on the other." else "Start capture to read the visible chart directly from the screen.",
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(onClick = onStartCapture, shape = RoundedCornerShape(16.dp)) {
                    Icon(Icons.Default.PlayArrow, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text("Start")
                }
                OutlinedButton(onClick = onStopCapture, shape = RoundedCornerShape(16.dp)) {
                    Icon(Icons.Default.PauseCircle, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text("Stop")
                }
                OutlinedButton(onClick = onReset, shape = RoundedCornerShape(16.dp)) {
                    Icon(Icons.Default.RestartAlt, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text("Reset all")
                }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                AssistChip(onClick = onMarkWin, label = { Text("Win +1") }, leadingIcon = { Icon(Icons.Default.ThumbUp, null) })
                AssistChip(onClick = onMarkLoss, label = { Text("Loss +1") }, leadingIcon = { Icon(Icons.Default.ThumbDown, null) })
            }
        }
    }
}

@Composable
private fun CalibrationCard(
    settings: AnalyzerSettings,
    onSettingsChange: (AnalyzerSettings) -> Unit,
    onResetDefaults: () -> Unit
) {
    OutlinedCard(shape = RoundedCornerShape(30.dp)) {
        Column(modifier = Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Tune, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Calibration & tuning", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            }

            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(modifier = Modifier.weight(1f)) {
                    Text("Auto crop", fontWeight = FontWeight.SemiBold)
                    Text("Use the default chart region or fine tune it manually.", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Switch(
                    checked = settings.useAutoCrop,
                    onCheckedChange = { onSettingsChange(settings.copy(useAutoCrop = it)) }
                )
            }

            CalibrationSlider(
                label = "Left edge",
                value = settings.roiLeft,
                rangeText = percentText(settings.roiLeft),
                onChange = { onSettingsChange(settings.copy(roiLeft = it)) }
            )
            CalibrationSlider(
                label = "Top edge",
                value = settings.roiTop,
                rangeText = percentText(settings.roiTop),
                onChange = { onSettingsChange(settings.copy(roiTop = it)) }
            )
            CalibrationSlider(
                label = "Right edge",
                value = settings.roiRight,
                rangeText = percentText(settings.roiRight),
                onChange = { onSettingsChange(settings.copy(roiRight = it)) }
            )
            CalibrationSlider(
                label = "Bottom edge",
                value = settings.roiBottom,
                rangeText = percentText(settings.roiBottom),
                onChange = { onSettingsChange(settings.copy(roiBottom = it)) }
            )
            CalibrationSlider(
                label = "Sample stride",
                value = settings.sampleStride.toFloat(),
                rangeText = settings.sampleStride.toString(),
                valueRange = 2f..16f,
                steps = 13,
                onChange = { onSettingsChange(settings.copy(sampleStride = it.toInt().coerceIn(2, 16))) }
            )
            CalibrationSlider(
                label = "Confidence threshold",
                value = settings.confidenceThreshold,
                rangeText = percentText(settings.confidenceThreshold),
                valueRange = 0.15f..0.85f,
                steps = 67,
                onChange = { onSettingsChange(settings.copy(confidenceThreshold = it)) }
            )
            CalibrationSlider(
                label = "History window",
                value = settings.historyWindow.toFloat(),
                rangeText = settings.historyWindow.toString(),
                valueRange = 4f..30f,
                steps = 24,
                onChange = { onSettingsChange(settings.copy(historyWindow = it.toInt().coerceIn(4, 30))) }
            )
            CalibrationSlider(
                label = "Smoothing",
                value = settings.smoothing,
                rangeText = percentText(settings.smoothing),
                valueRange = 0.10f..0.95f,
                steps = 85,
                onChange = { onSettingsChange(settings.copy(smoothing = it)) }
            )
            CalibrationSlider(
                label = "Color sensitivity",
                value = settings.colorSensitivity,
                rangeText = String.format(Locale.US, "%.2fx", settings.colorSensitivity),
                valueRange = 0.5f..2.0f,
                steps = 14,
                onChange = { onSettingsChange(settings.copy(colorSensitivity = it)) }
            )

            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                OutlinedButton(onClick = onResetDefaults, shape = RoundedCornerShape(16.dp)) {
                    Text("Restore defaults")
                }
            }
        }
    }
}

@Composable
private fun CalibrationSlider(
    label: String,
    value: Float,
    rangeText: String,
    onChange: (Float) -> Unit,
    valueRange: ClosedFloatingPointRange<Float> = 0f..1f,
    steps: Int = 0
) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(label, fontWeight = FontWeight.SemiBold)
            Text(rangeText, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Slider(value = value, onValueChange = onChange, valueRange = valueRange, steps = steps)
    }
}

@Composable
private fun SignalOverviewCard(
    snapshot: CandleSnapshot?,
    signal: Signal?,
    stats: Stats
) {
    OutlinedCard(shape = RoundedCornerShape(30.dp)) {
        Column(modifier = Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("Live signal", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            if (signal == null) {
                Text("Waiting for a stable chart frame...")
            } else {
                val directionLabel = when (signal.direction) {
                    Direction.UP -> "UP"
                    Direction.DOWN -> "DOWN"
                    Direction.WAIT -> "WAIT"
                }
                val directionColor = when (signal.direction) {
                    Direction.UP -> MaterialTheme.colorScheme.primary
                    Direction.DOWN -> MaterialTheme.colorScheme.error
                    Direction.WAIT -> MaterialTheme.colorScheme.secondary
                }
                Text(directionLabel, style = MaterialTheme.typography.displaySmall, fontWeight = FontWeight.Black, color = directionColor)
                Text("Confidence ${(signal.confidence * 100).toInt()}%", fontWeight = FontWeight.SemiBold)
                Text(signal.reason, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Divider()
            if (snapshot != null) {
                Text("Snapshot quality", fontWeight = FontWeight.SemiBold)
                MetricRow("Bullish", percentText(snapshot.bullishScore))
                MetricRow("Bearish", percentText(snapshot.bearishScore))
                MetricRow("Neutral", percentText(snapshot.neutralScore))
                MetricRow("Brightness", percentText(snapshot.brightness))
                MetricRow("Contrast", percentText(snapshot.contrast))
                MetricRow("Edge density", percentText(snapshot.edgeDensity))
            }
            Divider()
            Text("Accuracy ${stats.accuracy.toInt()}% · Predictions ${stats.predictions}")
        }
    }
}

@Composable
private fun MetricRow(label: String, value: String) {
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
private fun StrategyCard(
    signal: Signal?,
    weights: StrategyWeights
) {
    OutlinedCard(shape = RoundedCornerShape(30.dp)) {
        Column(modifier = Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("Strategy stack", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            Text("Weighted scores from the live chart engine.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            StrategyBar("Trend", signal?.strategyScores?.get(StrategyType.TREND) ?: 0f, weights.trend)
            StrategyBar("Momentum", signal?.strategyScores?.get(StrategyType.MOMENTUM) ?: 0f, weights.momentum)
            StrategyBar("Reversal", signal?.strategyScores?.get(StrategyType.REVERSAL) ?: 0f, weights.reversal)
            StrategyBar("Breakout", signal?.strategyScores?.get(StrategyType.BREAKOUT) ?: 0f, weights.breakout)
            StrategyBar("Volatility", signal?.strategyScores?.get(StrategyType.VOLATILITY) ?: 0f, weights.volatility)
        }
    }
}

@Composable
private fun StrategyBar(label: String, score: Float, weight: Float) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(label, fontWeight = FontWeight.SemiBold)
            Text("${formatSigned(score)} · x${String.format(Locale.US, "%.2f", weight)}", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        LinearProgressIndicator(
            progress = { abs(score).coerceIn(0f, 1f) },
            modifier = Modifier.fillMaxWidth().height(8.dp)
        )
    }
}

@Composable
private fun PerformanceCard(
    stats: Stats,
    recentSignals: List<RecentSignalItem>
) {
    OutlinedCard(shape = RoundedCornerShape(30.dp)) {
        Column(modifier = Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Speed, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Performance", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                PerformanceChip("Predictions", stats.predictions.toString())
                PerformanceChip("Wins", stats.wins.toString())
                PerformanceChip("Losses", stats.losses.toString())
                PerformanceChip("Best streak", stats.bestStreak.toString())
            }
            Text("Current streak: ${stats.currentStreak}")
            Divider()
            Text("Recent signals", fontWeight = FontWeight.SemiBold)
            if (recentSignals.isEmpty()) {
                Text("No logged signals yet.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    recentSignals.take(6).forEach { item ->
                        RecentSignalRow(item)
                    }
                }
            }
        }
    }
}

@Composable
private fun PerformanceChip(title: String, value: String) {
    Card(shape = RoundedCornerShape(18.dp), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer)) {
        Column(modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
            Text(title, fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.onPrimaryContainer)
            Text(value, fontWeight = FontWeight.Black, color = MaterialTheme.colorScheme.onPrimaryContainer)
        }
    }
}

@Composable
private fun RecentSignalRow(item: RecentSignalItem) {
    val label = when (item.direction) {
        Direction.UP -> "UP"
        Direction.DOWN -> "DOWN"
        Direction.WAIT -> "WAIT"
    }
    val tone = when (item.direction) {
        Direction.UP -> MaterialTheme.colorScheme.primary
        Direction.DOWN -> MaterialTheme.colorScheme.error
        Direction.WAIT -> MaterialTheme.colorScheme.secondary
    }
    Card(shape = RoundedCornerShape(18.dp)) {
        Column(modifier = Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(label, fontWeight = FontWeight.Black, color = tone)
                Text("${(item.confidence * 100).toInt()}%", fontWeight = FontWeight.SemiBold)
            }
            Text(item.reason, color = MaterialTheme.colorScheme.onSurfaceVariant)
            item.outcome?.let { outcome ->
                Text(
                    text = when (outcome) {
                        com.mariamqu.analysis.Outcome.WIN -> "Outcome: WIN"
                        com.mariamqu.analysis.Outcome.LOSS -> "Outcome: LOSS"
                    },
                    fontWeight = FontWeight.SemiBold
                )
            }
        }
    }
}

@Composable
private fun FooterCard() {
    OutlinedCard(shape = RoundedCornerShape(30.dp)) {
        Column(modifier = Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Savings, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Pro workflow", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            }
            Text("Use split-screen: Quotex on one side and mariamqu on the other. Keep the chart stable, then tune the ROI sliders until the live metrics are clean.")
        }
    }
}

@Composable
private fun pastelPinkScheme() = lightColorScheme(
    primary = Color(0xFFE85D9E),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFFFD8E7),
    onPrimaryContainer = Color(0xFF4A102C),
    secondary = Color(0xFFF2A6C6),
    onSecondary = Color(0xFF3A1630),
    tertiary = Color(0xFFFFC1D9),
    background = Color(0xFFFFF7FB),
    onBackground = Color(0xFF2C1B26),
    surface = Color(0xFFFFF9FC),
    onSurface = Color(0xFF2C1B26),
    surfaceVariant = Color(0xFFF7E2EB),
    onSurfaceVariant = Color(0xFF56404D),
    error = Color(0xFFD64C7F),
    outline = Color(0xFFCFA2B7)
)

private fun percentText(value: Float): String = "${(value * 100).toInt()}%"

private fun formatSigned(value: Float): String = String.format(Locale.US, "%+.2f", value)
