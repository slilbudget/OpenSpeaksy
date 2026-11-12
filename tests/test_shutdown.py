import numpy as np
import pytest

import main


class ExitCalled(Exception):
    pass


def test_shutdown_preserves_active_recording(monkeypatch, tmp_path):
    class StubRecorder:
        def stop(self):
            return np.zeros(main.MIN_AUDIO_SAMPLES, dtype=np.float32)

    saved = []

    def exit_process(code):
        raise ExitCalled(code)

    monkeypatch.setattr(main, "state", "recording")
    monkeypatch.setattr(main, "current_hotkey", main.HOTKEY_KEYCODE)
    monkeypatch.setattr(main, "current_mode", main.MODE_DICTATE)
    monkeypatch.setattr(main, "recorder", StubRecorder())
    monkeypatch.setattr(
        main,
        "save_pending_recording",
        lambda audio, mode: saved.append((len(audio), mode)) or tmp_path / "saved.wav",
    )
    monkeypatch.setattr(main.os, "_exit", exit_process)
    main._jobs[1] = main.Job(1, main.MODE_DICTATE)

    with pytest.raises(ExitCalled):
        main.handle_shutdown(15, None)

    assert main._jobs == {}     # no worker may paste while the process goes down
    assert saved == [(main.MIN_AUDIO_SAMPLES, main.MODE_DICTATE)]
    assert main.state == "idle"
    assert main.current_hotkey is None
    assert main.current_mode is None


def test_signal_waiter_routes_sigterm_to_preserving_shutdown(monkeypatch):
    calls = []

    def stop_after_one_signal(signum, frame):
        calls.append((signum, frame))
        raise ExitCalled(signum)

    monkeypatch.setattr(main, "shutdown_read_fd", 123)
    monkeypatch.setattr(
        main.os, "read", lambda fd, count: bytes([main.signal.SIGTERM])
    )
    monkeypatch.setattr(main, "handle_shutdown", stop_after_one_signal)

    with pytest.raises(ExitCalled):
        main.shutdown_signal_loop()

    assert calls == [(main.signal.SIGTERM, None)]
