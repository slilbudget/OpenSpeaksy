"""
Caps Lock calls off a transcription that is taking too long, and a second tap
puts it back in the queue.

The rule the whole feature rests on: cancelling stops the retries, never the
audio. A user who gives up waiting must still be able to get the words back.
"""

import os
import struct
import time

import pytest

import main


class _Overlay:
    def __init__(self):
        self.events = []

    def hide(self, token=None):
        self.events.append(("hide", token))

    def show(self, mode, label=None, token=None):
        self.events.append((mode, label))

    def flash_error(self, message=None, duration=None, token=None):
        self.events.append(("error", message))

    def flash_notice(self, message, duration=None, sound=True):
        self.events.append(("notice", message))


def _wav(path, age_sec=None):
    """
    Written with an mtime older than CANCEL_GRACE_SEC by default: a cancel only
    reaches recordings that have been waiting, so a freshly written one is
    deliberately out of reach.
    """
    sr = 16000
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + sr * 2))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", sr * 2))
        f.write(b"\x10\x20" * sr)
    if age_sec is None:
        age_sec = main.CANCEL_GRACE_SEC + 5
    old = time.time() - age_sec
    os.utime(path, (old, old))
    return path


@pytest.fixture
def app(monkeypatch, tmp_path):
    overlay = _Overlay()
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "PENDING_DIR", tmp_path)
    monkeypatch.setattr(main, "FALLBACK_PENDING_DIR", tmp_path / "fallback")
    monkeypatch.setattr(main, "QUARANTINE_DIR", tmp_path / "quarantine")
    monkeypatch.setattr(main, "state", "idle")
    monkeypatch.setattr(main, "current_wav_path", None)
    monkeypatch.setattr(main, "copy_to_clipboard", lambda text: None)
    monkeypatch.setattr(main, "_last_cancel_tap", 0.0)
    return overlay


def test_cancelling_keeps_the_audio(app, tmp_path):
    """The point of the feature: stop waiting, do not lose what was said."""
    wav = _wav(tmp_path / "20260922-030000-a.dictate.wav")

    stopped, held, newly_held = main.cancel_everything()

    assert wav.exists()
    assert held == 1
    assert newly_held == 1
    assert wav.name in main._pending_cancelled


def test_a_cancelled_recording_is_not_retried(app, monkeypatch, tmp_path):
    wav = _wav(tmp_path / "20260922-030001-a.dictate.wav")
    calls = []
    monkeypatch.setattr(
        main.transcriber,
        "transcribe_dictation_sync",
        lambda p, language=None: calls.append(p) or "text ",
    )

    main.cancel_everything()
    main._pending_next_attempt.clear()  # even once the wait has elapsed
    main.recover_pending_recordings()

    assert calls == []
    assert wav.exists()


def test_a_second_tap_puts_it_back_in_the_queue(app, monkeypatch, tmp_path):
    wav = _wav(tmp_path / "20260922-030002-a.dictate.wav")
    calls = []
    monkeypatch.setattr(
        main.transcriber,
        "transcribe_dictation_sync",
        lambda p, language=None: calls.append(p) or "text ",
    )

    main.cancel_everything()
    released = main.resume_everything()
    main.recover_pending_recordings()

    assert released == 1
    assert calls == [wav]
    assert not main._pending_cancelled


def test_cancelling_a_live_job_makes_its_worker_abort(app, monkeypatch, tmp_path):
    """
    A worker that finishes after the cancel must not paste into whatever the
    user is doing by then — the same rule the watchdog reset relies on.
    """
    wav = _wav(tmp_path / "20260922-030003-a.dictate.wav")
    monkeypatch.setattr(main, "state", "processing")
    monkeypatch.setattr(main, "current_job_id", 7)
    monkeypatch.setattr(main, "current_wav_path", wav)
    pasted = []
    monkeypatch.setattr(
        main.transcriber, "transcribe_dictation_sync", lambda *a, **k: "late text "
    )
    monkeypatch.setattr(main, "paste_text", lambda t: pasted.append(t) or True)

    stopped, _, _ = main.cancel_everything()
    # The worker was already running with the old generation.
    main.process_pending_recording(wav, 7, main.MODE_DICTATE)

    assert stopped == "processing"
    assert pasted == []
    assert wav.exists()


def test_two_quick_taps_resume_instead_of_cancelling_twice(app, monkeypatch, tmp_path):
    _wav(tmp_path / "20260922-030004-a.dictate.wav")
    monkeypatch.setattr(main.time, "monotonic", lambda: 100.0)

    main.on_cancel_tap()
    assert main._pending_cancelled

    # Same instant, so well inside CANCEL_DOUBLE_TAP_SEC.
    main.on_cancel_tap()
    assert not main._pending_cancelled

    kinds = [m for k, m in app.events if k == "notice"]
    assert "Cancelled" in kinds[0]
    assert "Retrying" in kinds[1]


def test_two_slow_taps_are_two_cancels(app, monkeypatch, tmp_path):
    _wav(tmp_path / "20260922-030005-a.dictate.wav")
    clock = {"t": 100.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: clock["t"])

    main.on_cancel_tap()
    clock["t"] += main.CANCEL_DOUBLE_TAP_SEC + 1
    main.on_cancel_tap()

    # Still held: a slow second tap must not be read as "retry now".
    assert main._pending_cancelled


def test_a_tap_with_nothing_to_cancel_stays_silent(app):
    """
    Caps Lock is also how capitals get typed, so a tap that changes nothing
    must not put a pill on screen every time.
    """
    main.on_cancel_tap()

    assert app.events == []


def test_a_healthy_transcription_is_not_cancellable_yet(app, monkeypatch, tmp_path):
    """
    The whole point of the grace window: a normal transcription takes a second
    or two, and a capital letter typed in that moment must not kill it.
    """
    _wav(tmp_path / "20260922-030007-a.dictate.wav")
    monkeypatch.setattr(main, "state", "processing")
    monkeypatch.setattr(main, "state_ts", 100.0)
    monkeypatch.setattr(
        main.time, "monotonic", lambda: 100.0 + main.CANCEL_GRACE_SEC - 1
    )

    main.on_cancel_tap()

    assert app.events == []
    assert not main._pending_cancelled
    assert main.state == "processing"


def test_a_stuck_transcription_is_cancellable_after_the_grace(
    app, monkeypatch, tmp_path
):
    _wav(tmp_path / "20260922-030008-a.dictate.wav")
    monkeypatch.setattr(main, "state", "processing")
    monkeypatch.setattr(main, "state_ts", 100.0)
    monkeypatch.setattr(
        main.time, "monotonic", lambda: 100.0 + main.CANCEL_GRACE_SEC + 1
    )

    main.on_cancel_tap()

    assert main.state == "idle"
    assert main._pending_cancelled
    assert any(k == "notice" for k, _ in app.events)


def test_a_tap_while_recording_is_ignored(app, monkeypatch, tmp_path):
    """Speaking is not a stuck transcription; the key must not end a sentence."""
    monkeypatch.setattr(main, "state", "recording")

    main.on_cancel_tap()

    assert main.state == "recording"
    assert app.events == []


def test_two_stray_taps_do_not_release_a_queue_nobody_held(
    app, monkeypatch, tmp_path
):
    """Double-tap means "retry now" only after something was actually held."""
    _wav(tmp_path / "20260922-030009-a.dictate.wav")
    monkeypatch.setattr(main.time, "monotonic", lambda: 100.0)
    released = []
    monkeypatch.setattr(
        main, "resume_everything", lambda: released.append(1) or 0
    )

    main.on_cancel_tap()  # holds the queue
    main._pending_cancelled.clear()  # pretend it was already released
    main.on_cancel_tap()  # a stray second capital

    assert released == []


def test_every_caps_lock_press_counts_as_one_tap(app, monkeypatch, tmp_path):
    """
    Caps Lock is a toggle: one press emits one event whose flag is SET when it
    turns capitals on and CLEAR when it turns them off. Gating the handler on
    the flag being set would act on every second press only, and the double
    tap could never happen at all.
    """
    _wav(tmp_path / "20260922-030006-a.dictate.wav")
    taps = []
    monkeypatch.setattr(main, "on_cancel_tap", lambda: taps.append(1))

    class _Event:
        def __init__(self, flags):
            self.flags = flags

    def _fake_flags(event):
        return event.flags

    def _fake_keycode(event, field):
        return main.CANCEL_KEYCODE

    monkeypatch.setattr(main, "CGEventGetFlags", _fake_flags)
    monkeypatch.setattr(main, "CGEventGetIntegerValueField", _fake_keycode)

    # Press on (flag set), press off (flag clear): two presses, two taps.
    main.tap_callback(None, main.kCGEventFlagsChanged, _Event(main.CANCEL_FLAG), None)
    main.tap_callback(None, main.kCGEventFlagsChanged, _Event(0), None)

    assert len(taps) == 2


def test_repeat_taps_on_an_already_held_queue_stay_silent(app, monkeypatch, tmp_path):
    """
    Once a queue is held, further capitals change nothing — so they must not
    put the same pill up again and again.
    """
    _wav(tmp_path / "20260922-030010-a.dictate.wav")
    clock = {"t": 100.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: clock["t"])

    main.on_cancel_tap()
    assert len(app.events) == 1

    # Well past the double-tap window, so this is a plain repeat.
    clock["t"] += main.CANCEL_DOUBLE_TAP_SEC + 5
    main.on_cancel_tap()

    assert len(app.events) == 1


def test_a_freshly_queued_recording_is_out_of_reach(app, monkeypatch, tmp_path):
    """
    One rule, uniformly: a tap acts on what has been stuck for a while. A
    recording written seconds ago is still on track, so a capital letter typed
    just after speaking must not pull it out of the queue.
    """
    _wav(tmp_path / "20260922-030011-a.dictate.wav", age_sec=1)
    monkeypatch.setattr(main, "state", "idle")

    main.on_cancel_tap()

    assert app.events == []
    assert not main._pending_cancelled
