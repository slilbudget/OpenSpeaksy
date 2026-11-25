import logging
import threading
import time

import numpy as np
import sounddevice as sd

logger = logging.getLogger("openspeaksy")

START_MAX_ATTEMPTS = 2
START_RETRY_DELAY_SEC = 0.1
# How long stop() waits for Core Audio to let go of the stream. Normally a
# few milliseconds; see Recorder.stuck for when it never does.
STREAM_CLOSE_WAIT_SEC = 2.0

# Set when a stream never finished closing. Stopping a Core Audio input can
# deadlock inside the system (seen live: the caller stuck in
# AudioOutputUnitStop on a HAL mutex, Core Audio's IO thread stuck in
# PortAudio's start/stop listener waiting for the AudioUnit). The close runs
# on its own thread so the audio is never held hostage; the app restarts once
# idle (main.py), since that Core Audio state does not recover.
audio_stuck = threading.Event()


class Recorder:
    def __init__(self, samplerate=16000):
        self.samplerate = samplerate
        self._stream = None
        self._chunks = []
        self._lock = threading.Lock()
        self._recording = False

    def _callback(self, indata, _frames, _time_info, status):
        try:
            if status:
                logger.warning(f"audio status: {status}")
            with self._lock:
                if self._recording:
                    self._chunks.append(indata.copy())
        except Exception as e:
            logger.error(f"audio callback error: {e}")

    @staticmethod
    def _close_stream(stream, context):
        try:
            if stream.active:
                stream.stop()
        except Exception as e:
            logger.error(f"{context} stream stop error: {e}")
        try:
            stream.close()
        except Exception as e:
            logger.error(f"{context} stream close error: {e}")

    def _close_aside(self, stream, context):
        """
        Close the stream on its own thread and wait up to
        STREAM_CLOSE_WAIT_SEC. Returns whether it closed; if not, the thread
        stays blocked where Core Audio holds it, and audio_stuck is set.
        """
        closed = threading.Event()

        def close():
            self._close_stream(stream, context)
            closed.set()

        threading.Thread(target=close, name="audio-close", daemon=True).start()
        if closed.wait(STREAM_CLOSE_WAIT_SEC):
            return True
        audio_stuck.set()
        logger.error(
            f"{context} stream did not stop within {STREAM_CLOSE_WAIT_SEC:g} s "
            "(Core Audio stuck); continuing without it"
        )
        return False

    def start(self):
        # Defensive: clean up orphan stream if previous recording was abandoned
        # (watchdog reset, lost key-up event)
        with self._lock:
            orphan = self._stream
            self._stream = None
        if orphan is not None:
            self._close_aside(orphan, "orphan")

        # Core Audio stays in shared mode: never change device parameters or
        # take exclusive ownership from FaceTime, browser calls, or recording
        # apps. Its converter handles the device's native rate → 16 kHz PCM.
        # A Bluetooth/Continuity device switch can make the first open fail
        # transiently while Core Audio refreshes its default input. Retry once
        # before abandoning the cycle.
        for attempt in range(1, START_MAX_ATTEMPTS + 1):
            with self._lock:
                self._chunks = []
                self._recording = True

            stream = None
            try:
                stream = sd.InputStream(
                    samplerate=self.samplerate,
                    channels=1,
                    dtype="float32",
                    callback=self._callback,
                    extra_settings=sd.CoreAudioSettings(
                        change_device_parameters=False,
                        conversion_quality="max",
                    ),
                )
                self._stream = stream
                stream.start()
                return
            except Exception as error:
                self._stream = None
                with self._lock:
                    self._recording = False
                    self._chunks = []
                if stream is not None:
                    self._close_stream(stream, "failed-start")
                if attempt == START_MAX_ATTEMPTS:
                    raise
                logger.warning(
                    f"audio input start failed on attempt "
                    f"{attempt}/{START_MAX_ATTEMPTS}: {error}; retrying"
                )
                time.sleep(START_RETRY_DELAY_SEC)

    def stop(self):
        with self._lock:
            self._recording = False

        stream = self._stream
        self._stream = None
        if stream is not None:
            # Device changes and Continuity/Bluetooth disconnects can make
            # stop/close fail, or hang. Preserve already-captured audio
            # regardless.
            self._close_aside(stream, "recording")

        with self._lock:
            if self._chunks:
                audio = np.concatenate(self._chunks, axis=0).flatten()
            else:
                audio = np.array([], dtype="float32")
            self._chunks = []
        return audio
