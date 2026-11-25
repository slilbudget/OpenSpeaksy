import numpy as np
import pytest

import recorder as recorder_module


class FakeStream:
    def __init__(self, *, stop_error=None, close_error=None):
        self.active = False
        self.stop_error = stop_error
        self.close_error = close_error
        self.started = False
        self.closed = False

    def start(self):
        self.started = True
        self.active = True

    def stop(self):
        self.active = False
        if self.stop_error:
            raise self.stop_error

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


def test_coreaudio_stream_never_changes_device_parameters(monkeypatch):
    captured = {}
    stream = FakeStream()

    def fake_settings(**kwargs):
        captured["settings"] = kwargs
        return "coreaudio-settings"

    def fake_input_stream(**kwargs):
        captured["stream"] = kwargs
        return stream

    monkeypatch.setattr(recorder_module.sd, "CoreAudioSettings", fake_settings)
    monkeypatch.setattr(recorder_module.sd, "InputStream", fake_input_stream)

    recorder = recorder_module.Recorder()
    recorder.start()
    recorder.stop()

    assert captured["settings"] == {
        "change_device_parameters": False,
        "conversion_quality": "max",
    }
    assert captured["stream"]["extra_settings"] == "coreaudio-settings"
    assert captured["stream"]["samplerate"] == 16000
    assert stream.started and stream.closed


def test_stop_preserves_audio_when_device_cleanup_fails(monkeypatch):
    stream = FakeStream(
        stop_error=RuntimeError("device disconnected"),
        close_error=RuntimeError("device unavailable"),
    )
    errors = []
    monkeypatch.setattr(recorder_module.sd, "CoreAudioSettings", lambda **kwargs: object())
    monkeypatch.setattr(recorder_module.sd, "InputStream", lambda **kwargs: stream)
    monkeypatch.setattr(recorder_module.logger, "error", errors.append)

    recorder = recorder_module.Recorder()
    recorder.start()
    recorder._callback(np.ones((4, 1), dtype=np.float32), 4, None, None)

    audio = recorder.stop()

    np.testing.assert_array_equal(audio, np.ones(4, dtype=np.float32))
    assert any("stream stop error" in message for message in errors)
    assert any("stream close error" in message for message in errors)


def test_failed_stream_creation_resets_recording_state(monkeypatch):
    def fail_input_stream(**kwargs):
        raise RuntimeError("microphone unavailable")

    monkeypatch.setattr(recorder_module.sd, "CoreAudioSettings", lambda **kwargs: object())
    monkeypatch.setattr(recorder_module.sd, "InputStream", fail_input_stream)
    recorder = recorder_module.Recorder()

    with pytest.raises(RuntimeError, match="microphone unavailable"):
        recorder.start()

    assert recorder._recording is False
    assert recorder._stream is None
    assert recorder.stop().size == 0


def test_transient_stream_creation_failure_is_retried(monkeypatch):
    stream = FakeStream()
    calls = {"count": 0}

    def flaky_input_stream(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("default input is switching")
        return stream

    monkeypatch.setattr(recorder_module.sd, "CoreAudioSettings", lambda **kwargs: object())
    monkeypatch.setattr(recorder_module.sd, "InputStream", flaky_input_stream)
    monkeypatch.setattr(recorder_module, "START_RETRY_DELAY_SEC", 0)

    recorder = recorder_module.Recorder()
    recorder.start()

    assert calls["count"] == 2
    assert stream.started
    recorder.stop()


def test_a_stream_that_never_stops_does_not_hold_the_audio(monkeypatch):
    """Core Audio can deadlock in stop: the take keeps its audio, the caller
    moves on, and the flag asks the app to restart."""
    import threading

    release = threading.Event()

    class HangingStream(FakeStream):
        def stop(self):
            release.wait(5)                 # stuck inside Core Audio

    stream = HangingStream()
    monkeypatch.setattr(recorder_module.sd, "CoreAudioSettings", lambda **kw: None)
    monkeypatch.setattr(recorder_module.sd, "InputStream", lambda **kw: stream)
    monkeypatch.setattr(recorder_module, "STREAM_CLOSE_WAIT_SEC", 0.05)
    recorder_module.audio_stuck.clear()

    recorder = recorder_module.Recorder()
    recorder.start()
    recorder._callback(np.ones((160, 1), dtype="float32"), 160, None, None)
    audio = recorder.stop()

    assert len(audio) == 160
    assert recorder_module.audio_stuck.is_set()
    release.set()
    recorder_module.audio_stuck.clear()


def test_a_normal_stop_is_not_stuck(monkeypatch):
    stream = FakeStream()
    monkeypatch.setattr(recorder_module.sd, "CoreAudioSettings", lambda **kw: None)
    monkeypatch.setattr(recorder_module.sd, "InputStream", lambda **kw: stream)
    recorder_module.audio_stuck.clear()
    recorder = recorder_module.Recorder()
    recorder.start()
    recorder.stop()
    assert stream.closed and not recorder_module.audio_stuck.is_set()
