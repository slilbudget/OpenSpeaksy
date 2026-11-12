"""
A key-up event for a hotkey that did NOT start the current cycle must be
ignored. Without this, tapping the other hotkey mid-record (e.g. right Option
while holding right Command) would end the cycle and trigger transcription
of a half-spoken phrase. The invariant is enforced by on_key_up checking
current_hotkey before ending the recording.
"""
import main


def _reset_state(monkeypatch):
    monkeypatch.setattr(main, "state", "idle")
    monkeypatch.setattr(main, "state_ts", 0.0)
    monkeypatch.setattr(main, "current_hotkey", None)
    monkeypatch.setattr(main, "current_mode", None)


def test_on_key_up_ignores_non_owning_hotkey(monkeypatch):
    _reset_state(monkeypatch)
    # Simulate: dictate cycle is in flight, owned by HOTKEY_KEYCODE
    monkeypatch.setattr(main, "state", "recording")
    monkeypatch.setattr(main, "current_hotkey", main.HOTKEY_KEYCODE)
    monkeypatch.setattr(main, "current_mode", main.MODE_DICTATE)

    # A spurious key-up for the OTHER hotkey must not transition out of recording
    main.on_key_up(main.TRANSLATE_KEYCODE)

    assert main.state == "recording"
    assert main.current_hotkey == main.HOTKEY_KEYCODE
    assert main.current_mode == main.MODE_DICTATE


def test_on_key_up_for_owning_hotkey_ends_the_recording(monkeypatch):
    """Positive control: the owning hotkey's key-up DOES leave 'recording'."""
    _reset_state(monkeypatch)
    monkeypatch.setattr(main, "state", "recording")
    monkeypatch.setattr(main, "current_hotkey", main.TRANSLATE_KEYCODE)
    monkeypatch.setattr(main, "current_mode", main.MODE_TRANSLATE)

    # Stub recorder/overlay so we don't touch real hardware
    class _StubRecorder:
        def stop(self):
            import numpy as np
            return np.zeros(0, dtype="float32")  # below MIN_AUDIO_SAMPLES — short-circuits

    class _StubOverlay:
        def hide(self): pass
        def show(self, mode, label=None): pass
        def flash_error(self, message=None, duration=None, **_): pass

    monkeypatch.setattr(main, "recorder", _StubRecorder())
    monkeypatch.setattr(main, "overlay", _StubOverlay())

    main.on_key_up(main.TRANSLATE_KEYCODE)

    # begin_processing freed the slot and queued a job; the short audio then dropped it
    assert main.state == "idle"
    assert main._jobs == {}
    # current_hotkey was cleared by begin_processing
    assert main.current_hotkey is None
    assert main.current_mode is None
