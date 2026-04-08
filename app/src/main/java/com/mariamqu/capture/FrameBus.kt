package com.mariamqu.capture

import android.graphics.Bitmap
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

object FrameBus {
    private val _frames = MutableSharedFlow<Bitmap>(
        extraBufferCapacity = 4,
        onBufferOverflow = BufferOverflow.DROP_OLDEST
    )
    val frames: SharedFlow<Bitmap> = _frames

    private val _running = MutableStateFlow(false)
    val running: StateFlow<Boolean> = _running.asStateFlow()

    fun emit(bitmap: Bitmap) {
        _frames.tryEmit(bitmap)
    }

    fun setRunning(running: Boolean) {
        _running.value = running
    }
}
