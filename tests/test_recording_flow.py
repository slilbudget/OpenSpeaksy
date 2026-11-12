
import numpy as np

import main


class _Overlay:
    def __init__(self):
        self.events = []

    def hide(self, **_):
        self.events.append(("hide", None))

    def show(self, mode, label=None, queued=0, **_):
        self.events.append((mode, label))

    def flash_error(self, message=None, duration=None, **_):
        # Record the message so tests can assert what the user actually sees.
        self.events.append(("error", message))


def _recording_state(monkeypatch):
    monkeypatch.setattr(main, "state", "recording")
    monkeypatch.setattr(main, "state_ts", 0.0)
    monkeypatch.setattr(main, "current_hotkey", main.HOTKEY_KEYCODE)
    monkeypatch.setattr(main, "current_mode", main.MODE_DICTATE)
    monkeypatch.setattr(main, "current_job_id", 0)


def test_recording_above_minimum_is_saved(
    monkeypatch, tmp_path
):
    _recording_state(monkeypatch)
    audio = np.zeros(main.MIN_AUDIO_SAMPLES, dtype=np.float32)
    overlay = _Overlay()
    saved = []
    started = []

    class Recorder:
        def stop(self):
            return audio

    class Thread:
        def __init__(self, *, target, args=(), daemon):
            self.target = target
            self.args = args

        def start(self):
            if self.target is main.process_pending_recording:
                started.append(self.args)

    wav = tmp_path / "short-word.dictate.wav"
    monkeypatch.setattr(main, "recorder", Recorder())
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(
        main,
        "save_pending_recording",
        lambda captured, mode: saved.append((len(captured), mode)) or wav,
    )
    monkeypatch.setattr(main.threading, "Thread", Thread)

    main.on_key_up(main.HOTKEY_KEYCODE)

    assert saved == [(main.MIN_AUDIO_SAMPLES, main.MODE_DICTATE)]
    assert started and started[0][0] == wav
    # The recording slot is free for the next take at once; this one is queued.
    assert main.state == "idle"
    assert [job.path for job in main._jobs.values()] == [wav]


def test_accidental_tap_is_still_ignored(monkeypatch):
    _recording_state(monkeypatch)
    overlay = _Overlay()
    logs = []

    class Recorder:
        def stop(self):
            return np.zeros(15, dtype=np.float32)

    monkeypatch.setattr(main, "recorder", Recorder())
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "log", logs.append)

    main.on_key_up(main.HOTKEY_KEYCODE)

    assert main.state == "idle"
    assert main._jobs == {}
    assert any("recording ignored" in message for message in logs)


def test_worker_start_failure_keeps_saved_wav(monkeypatch, tmp_path):
    _recording_state(monkeypatch)
    overlay = _Overlay()
    wav = tmp_path / "preserved.dictate.wav"

    class Recorder:
        def stop(self):
            return np.zeros(main.MIN_AUDIO_SAMPLES, dtype=np.float32)

    class Thread:
        def __init__(self, *, target, args=(), daemon):
            self.target = target

        def start(self):
            if self.target is main.process_pending_recording:
                raise RuntimeError("thread unavailable")

    def _save(audio, mode):
        wav.write_bytes(b"RIFF----WAVEfmt ")
        return wav

    monkeypatch.setattr(main, "recorder", Recorder())
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "save_pending_recording", _save)
    monkeypatch.setattr(main.threading, "Thread", Thread)

    main.on_key_up(main.HOTKEY_KEYCODE)

    assert main.state == "idle"
    assert ("error", "Will retry") in overlay.events
    # The point of the test, and previously unasserted: the audio is still on
    # disk for the retry loop. The stub used to return a path it never wrote,
    # so "keeps_saved_wav" was checking nothing.
    assert wav.exists()
    assert main._jobs == {}


def test_recorder_start_failure_flashes_error(monkeypatch):
    monkeypatch.setattr(main, "state", "idle")
    monkeypatch.setattr(main, "current_hotkey", None)
    monkeypatch.setattr(main, "current_mode", None)
    overlay = _Overlay()

    class Recorder:
        def start(self):
            raise RuntimeError("microphone unavailable")

    monkeypatch.setattr(main, "recorder", Recorder())
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "_microphone_access_is_blocked", lambda: False)

    main.on_key_down(main.HOTKEY_KEYCODE, main.MODE_DICTATE)

    assert main.state == "idle"
    assert main.current_hotkey is None
    assert ("error", "Mic unavailable") in overlay.events


def test_denied_microphone_is_rejected_before_recorder_start(monkeypatch):
    monkeypatch.setattr(main, "state", "idle")
    monkeypatch.setattr(main, "current_hotkey", None)
    monkeypatch.setattr(main, "current_mode", None)
    overlay = _Overlay()
    starts = []

    class Recorder:
        def start(self):
            starts.append(True)

    monkeypatch.setattr(main, "recorder", Recorder())
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "_microphone_access_is_blocked", lambda: True)

    main.on_key_down(main.HOTKEY_KEYCODE, main.MODE_DICTATE)

    assert starts == []
    assert main.state == "idle"
    assert ("error", "Mic blocked") in overlay.events


def test_primary_save_failure_uses_fallback(monkeypatch, tmp_path):
    fallback_dir = tmp_path / "fallback"
    fallback_wav = fallback_dir / "preserved.dictate.wav"
    calls = []

    def save(audio, mode, pending_dir=None):
        calls.append(pending_dir)
        if pending_dir is None:
            raise OSError("primary storage unavailable")
        return fallback_wav

    monkeypatch.setattr(main, "FALLBACK_PENDING_DIR", fallback_dir)
    monkeypatch.setattr(main, "save_pending_recording", save)

    result = main.save_recording_with_fallback(
        np.ones(main.MIN_AUDIO_SAMPLES, dtype=np.float32),
        main.MODE_DICTATE,
    )

    assert result == fallback_wav
    assert calls == [None, fallback_dir]


def test_both_save_locations_failing_flashes_error(monkeypatch):
    _recording_state(monkeypatch)
    overlay = _Overlay()

    class Recorder:
        def stop(self):
            return np.ones(main.MIN_AUDIO_SAMPLES, dtype=np.float32)

    def fail_save(audio, mode, pending_dir=None):
        raise OSError("storage unavailable")

    monkeypatch.setattr(main, "recorder", Recorder())
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "save_pending_recording", fail_save)

    main.on_key_up(main.HOTKEY_KEYCODE)

    assert main.state == "idle"
    assert ("error", "Not saved") in overlay.events
