"""
The take queue: a new recording can start while earlier takes are still
transcribing, and every take pastes in the order it was spoken, never while a
modifier key is down, and never at the cost of another take's text.
"""
import struct
import threading
import time

import numpy as np
import pytest

import main
from transcriber import RequestRejectedError, TranscriptionError


class _Overlay:
    def __init__(self):
        self.events = []

    def show(self, mode, label=None, queued=0, **_):
        self.events.append((mode, label, queued))

    def hide(self, **_):
        self.events.append(("hide",))

    def flash_error(self, message=None, duration=None, **_):
        self.events.append(("error", message))

    def flash_notice(self, message, duration=None, sound=True):
        self.events.append(("notice", message))


def _wav(path):
    sr = 16000
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + sr * 2) + b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16))
        f.write(b"data" + struct.pack("<I", sr * 2) + b"\x10\x20" * sr)
    return path


@pytest.fixture
def q(monkeypatch, tmp_path):
    overlay = _Overlay()
    pasted = []
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "state", "idle")
    monkeypatch.setattr(main, "PENDING_DIR", tmp_path)
    monkeypatch.setattr(main, "QUARANTINE_DIR", tmp_path / "quarantine")
    monkeypatch.setattr(main, "paste_text", lambda text: pasted.append(text) or True)
    monkeypatch.setattr(main, "log", lambda message: None)

    class Q:
        def queue(self, job_id, name=None, mode=main.MODE_DICTATE):
            job = main.Job(job_id, mode)
            job.path = _wav(tmp_path / f"{name or job_id}.{mode}.wav")
            main._jobs[job_id] = job
            return job.path

        def finish(self, job_id, result):
            """Run job_id's worker with a transcriber that returns or raises result."""
            def transcribe(*_a, **_k):
                if isinstance(result, Exception):
                    raise result
                return result
            monkeypatch.setattr(main.transcriber, "transcribe_dictation_sync", transcribe)
            main.process_pending_recording(main._jobs[job_id].path, job_id, main.MODE_DICTATE)

    q = Q()
    q.pasted = pasted
    q.overlay = overlay
    return q


def test_a_take_that_finishes_early_waits_for_the_one_before_it(q):
    first, second = q.queue(1), q.queue(2)

    q.finish(2, "second ")
    assert q.pasted == []
    assert second.exists()

    q.finish(1, "first ")
    # One paste, in spoken order, a blank line apart: two ⌘V back to back
    # can race the clipboard.
    assert q.pasted == ["first\n\nsecond "]
    assert not first.exists() and not second.exists()
    assert main._jobs == {}


def test_a_failed_take_does_not_hold_up_the_ones_after_it(q, monkeypatch):
    monkeypatch.setattr(main, "_schedule_pending_retry", lambda path, error=None: 10)
    failed, ok = q.queue(1), q.queue(2)

    q.finish(2, "kept text ")
    q.finish(1, TranscriptionError("HTTP Error 503"))

    assert q.pasted == ["kept text "]
    assert failed.exists()              # the retry loop owns it now
    assert not ok.exists()
    assert ("error", "HTTP Error 503 · retry in 10s") in q.overlay.events


def test_a_rejected_take_in_the_middle_is_quarantined(q, tmp_path):
    q.queue(1)
    bad = q.queue(2)
    q.queue(3)

    q.finish(1, "a ")
    q.finish(3, "c ")
    q.finish(2, RequestRejectedError("recording is too large"))

    assert q.pasted == ["a ", "c "]
    assert (tmp_path / "quarantine" / bad.name).exists()


def test_nothing_pastes_while_a_modifier_is_down(q, monkeypatch):
    held = {"now": True}
    timers = []
    monkeypatch.setattr(main, "_modifiers_held", lambda: held["now"])

    started = []

    class _Timer:
        def __init__(self, delay, fn):
            timers.append(fn)
            self.daemon = True

        def start(self):
            started.append(1)

    monkeypatch.setattr(main.threading, "Timer", _Timer)
    q.queue(1)

    q.finish(1, "text ")
    assert q.pasted == []
    assert timers and started, "a held key must not strand the paste: it looks again"

    held["now"] = False
    timers[-1]()
    assert q.pasted == ["text "]


def test_the_last_modifier_up_releases_a_waiting_paste(q, monkeypatch):
    drains = []
    monkeypatch.setattr(main, "_schedule_drain", lambda: drains.append(1))
    monkeypatch.setattr(main, "CGEventGetIntegerValueField", lambda e, f: 0x38)  # left Shift
    monkeypatch.setattr(main, "CGEventGetFlags", lambda e: 0)
    q.queue(1)
    main._jobs[1].done = True

    main.tap_callback(None, main.kCGEventFlagsChanged, object(), None)
    assert drains == [1]

    # With a key still down, or nothing waiting, the tap stays out of it.
    monkeypatch.setattr(main, "CGEventGetFlags", lambda e: main.kCGEventFlagMaskShift)
    main.tap_callback(None, main.kCGEventFlagsChanged, object(), None)
    main._jobs[1].done = False
    monkeypatch.setattr(main, "CGEventGetFlags", lambda e: 0)
    main.tap_callback(None, main.kCGEventFlagsChanged, object(), None)
    assert drains == [1]


def test_a_paste_the_app_refuses_lands_in_the_clipboard(q, monkeypatch):
    copied = []
    monkeypatch.setattr(main, "paste_text", lambda text: False)
    monkeypatch.setattr(main, "copy_to_clipboard", copied.append)
    a, b = q.queue(1), q.queue(2)

    q.finish(1, "a ")
    q.finish(2, "b ")

    assert copied == ["a ", "b "]
    assert not a.exists() and not b.exists()
    assert ("notice", "Copied · ⌘V") in q.overlay.events


def test_a_take_neither_pasted_nor_copied_keeps_its_file(q, monkeypatch):
    def refuse(text):
        raise RuntimeError("pasteboard rejected transcription")

    monkeypatch.setattr(main, "paste_text", lambda text: False)
    monkeypatch.setattr(main, "copy_to_clipboard", refuse)
    a = q.queue(1)

    q.finish(1, "a ")

    assert a.exists()
    assert ("error", "Paste failed · saved") in q.overlay.events


def test_a_late_worker_for_a_cancelled_take_pastes_nothing(q, monkeypatch):
    wav = q.queue(1)
    main.cancel_everything()

    monkeypatch.setattr(main.transcriber, "transcribe_dictation_sync", lambda *a, **k: "late ")
    main.process_pending_recording(wav, 1, main.MODE_DICTATE)

    assert q.pasted == []
    assert wav.exists()


def test_the_watchdog_drops_a_stuck_take_and_the_next_one_pastes(q, monkeypatch):
    monkeypatch.setattr(main, "_schedule_drain", main._drain_ready_jobs)
    stuck = q.queue(1)
    q.queue(2)
    main._jobs[1].started -= main._take_timeout(main._jobs[1]) + 1
    main._jobs[2].done = True
    main._jobs[2].text = "next "

    main._watchdog_tick()

    assert q.pasted == ["next "]
    assert stuck.exists()
    assert ("error", "Timed out · saved") in q.overlay.events


def test_a_new_take_can_start_while_others_transcribe(q):
    q.queue(1)
    q.queue(2)

    assert main._begin_recording(main.HOTKEY_KEYCODE, main.MODE_DICTATE)
    main._refresh_overlay()
    assert q.overlay.events[-1] == ("recording", None, 2)


def test_only_one_take_records_at_a_time(q):
    assert main._begin_recording(main.HOTKEY_KEYCODE, main.MODE_DICTATE)
    assert not main._begin_recording(main.TRANSLATE_KEYCODE, main.MODE_TRANSLATE)


def test_the_pill_follows_the_queue(q):
    q.queue(1, mode=main.MODE_TRANSLATE)
    q.queue(2)
    main._refresh_overlay()
    assert q.overlay.events[-1] == ("loading", "English", 2)

    main._jobs.clear()
    main._refresh_overlay()
    assert q.overlay.events[-1] == ("hide",)


def test_a_take_voided_while_saving_is_left_to_the_retry_loop(q, monkeypatch, tmp_path):
    """A cancel can land between the recorder stopping and the file landing."""
    wav = _wav(tmp_path / "saved.dictate.wav")
    workers = []

    class Recorder:
        def stop(self):
            return np.ones(main.MIN_AUDIO_SAMPLES, dtype=np.float32)

    def save(audio, mode):
        main._jobs.clear()          # the cancel
        return wav

    monkeypatch.setattr(main, "recorder", Recorder())
    monkeypatch.setattr(main, "save_recording_with_fallback", save)
    monkeypatch.setattr(main, "process_pending_recording", lambda *a: workers.append(a))
    monkeypatch.setattr(main, "state", "recording")
    monkeypatch.setattr(main, "current_hotkey", main.HOTKEY_KEYCODE)
    monkeypatch.setattr(main, "current_mode", main.MODE_DICTATE)

    main.on_key_up(main.HOTKEY_KEYCODE)
    time.sleep(0.05)

    assert workers == []
    assert wav.exists()


def test_back_to_back_pastes_leave_the_app_time_to_read_the_clipboard(monkeypatch):
    slept = []
    monkeypatch.setattr(main, "PASTE_SETTLE_SEC", 0.3)
    monkeypatch.setattr(main.time, "sleep", slept.append)
    monkeypatch.setattr(main, "copy_to_clipboard", lambda text: None)
    monkeypatch.setattr(main, "CGPreflightPostEventAccess", lambda: False)
    main._last_paste_at = main.time.monotonic()

    main.paste_text("x")

    assert slept and 0.2 < slept[0] <= 0.3


def test_three_takes_end_to_end_paste_in_spoken_order(monkeypatch, tmp_path):
    """
    Real worker threads: the second take transcribes fastest, the first
    slowest. Each take is started while the earlier ones are still in flight.
    """
    overlay = _Overlay()
    pasted = []
    delays = {"1": 0.3, "2": 0.05, "3": 0.15}
    saved = iter(["1", "2", "3"])

    class Recorder:
        def start(self):
            pass

        def stop(self):
            return np.ones(main.MIN_AUDIO_SAMPLES, dtype=np.float32)

    def save(audio, mode):
        return _wav(tmp_path / f"{next(saved)}.dictate.wav")

    def transcribe(path, language=None):
        name = path.name.split(".")[0]
        time.sleep(delays[name])
        return f"take{name} "

    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "log", lambda message: None)
    monkeypatch.setattr(main, "recorder", Recorder())
    monkeypatch.setattr(main, "save_recording_with_fallback", save)
    monkeypatch.setattr(main, "_microphone_access_is_blocked", lambda: False)
    monkeypatch.setattr(main.transcriber, "transcribe_dictation_sync", transcribe)
    monkeypatch.setattr(main, "paste_text", lambda text: pasted.append(text) or True)
    monkeypatch.setattr(main, "state", "idle")
    monkeypatch.setattr(main, "current_hotkey", None)

    for _ in range(3):
        main.on_key_down(main.HOTKEY_KEYCODE, main.MODE_DICTATE)
        main.on_key_up(main.HOTKEY_KEYCODE)

    deadline = time.monotonic() + 3
    while main._jobs and time.monotonic() < deadline:
        time.sleep(0.02)

    assert main._jobs == {}
    # How they batch depends on timing; the order and the words never do.
    assert "".join(pasted).split() == ["take1", "take2", "take3"]
    assert list(tmp_path.glob("*.wav")) == []
    assert overlay.events[-1] == ("hide",)
    # Let the drain threads the last paste spawned wind down.
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.daemon:
            thread.join(timeout=0.5)


# --- review round 2: races, a stuck key, and the pill at each step -----------

def _held_timers(monkeypatch):
    timers = []

    class _Timer:
        def __init__(self, delay, fn):
            timers.append(fn)
            self.daemon = True

        def start(self):
            pass

    monkeypatch.setattr(main.threading, "Timer", _Timer)
    return timers


def test_one_recheck_timer_however_many_drains_see_the_key_held(q, monkeypatch):
    monkeypatch.setattr(main, "_modifiers_held", lambda: True)
    monkeypatch.setattr(main, "_drain_recheck_armed", False)
    timers = _held_timers(monkeypatch)
    q.queue(1)
    main._jobs[1].done = True

    for _ in range(5):
        main._drain_ready_jobs()
    assert len(timers) == 1

    timers[0]()                         # fires, still held: re-arms once
    assert len(timers) == 2


def test_a_stuck_modifier_sends_the_text_to_the_clipboard(q, monkeypatch):
    """A modifier that never reads as up must not hold the queue forever."""
    copied = []
    monkeypatch.setattr(main, "_modifiers_held", lambda: True)
    monkeypatch.setattr(main, "copy_to_clipboard", copied.append)
    _held_timers(monkeypatch)
    a = q.queue(1)
    q.finish(1, "text ")
    assert q.pasted == [] and copied == []

    main._held_since -= main.MODIFIER_HOLD_MAX_SEC + 1
    main._drain_ready_jobs()

    assert q.pasted == []               # no ⌘V under a held modifier, ever
    assert copied == ["text "]
    assert not a.exists()
    assert ("notice", "Copied · ⌘V") in q.overlay.events


def test_a_key_held_for_a_long_take_is_not_taken_for_stuck(q, monkeypatch):
    """
    A take that waited behind a minute-long held recording must still paste
    when the key comes up, even if the system's modifier state lags the key-up
    for a moment: the stuck-key clock starts when nothing is recording.
    """
    held = {"now": True}
    monkeypatch.setattr(main, "_modifiers_held", lambda: held["now"])
    monkeypatch.setattr(main, "copy_to_clipboard", lambda t: pytest.fail("copied"))
    _held_timers(monkeypatch)
    clock = {"t": main.time.monotonic()}
    monkeypatch.setattr(main.time, "monotonic", lambda: clock["t"])
    q.queue(1)
    q.finish(1, "text ")                # done while idle, key down: clock starts
    monkeypatch.setattr(main, "state", "recording")   # the next take, key down
    main._drain_ready_jobs()            # ...and stops: a recording is no stuck key
    clock["t"] += 60
    main._drain_ready_jobs()
    assert q.pasted == [] and list(main._jobs) == [1]

    monkeypatch.setattr(main, "state", "idle")         # key-up, state lags
    main._drain_ready_jobs()
    held["now"] = False
    main._drain_ready_jobs()
    assert q.pasted == ["text "]


def test_a_silent_take_deletes_its_file(q):
    wav = q.queue(1)
    q.finish(1, "")
    assert q.pasted == []
    assert not wav.exists()


def test_recovery_waits_for_a_paste_in_progress(q, monkeypatch, tmp_path):
    """
    The drain takes the jobs out of _jobs before pasting and deletes their
    files after. A recovery pass in between used to see an idle app and a
    file nobody owned: it transcribed it again and copied it over the user's
    clipboard.
    """
    seen = []
    monkeypatch.setattr(main, "FALLBACK_PENDING_DIR", tmp_path / "fallback")
    monkeypatch.setattr(
        main.transcriber, "transcribe_dictation_sync",
        lambda path, language=None: seen.append(path) or "again ",
    )
    recovery = []

    def paste(text):
        t = threading.Thread(target=main.recover_pending_recordings)
        t.start()
        recovery.append(t)
        time.sleep(0.1)
        assert seen == [], "recovery ran in the middle of a paste"
        q.pasted.append(text)
        return True

    monkeypatch.setattr(main, "paste_text", paste)
    q.queue(1)
    main._jobs[1].done = True
    main._jobs[1].text = "once "

    main._drain_ready_jobs()
    recovery[0].join(timeout=2)

    assert q.pasted == ["once "]
    assert seen == []


def test_recovery_leaves_a_take_that_is_still_being_saved(q, monkeypatch, tmp_path):
    """Its file can be on disk a moment before its job knows the path."""
    monkeypatch.setattr(main, "FALLBACK_PENDING_DIR", tmp_path / "fallback")
    monkeypatch.setattr(
        main.transcriber, "transcribe_dictation_sync",
        lambda *a, **k: pytest.fail("transcribed a live take"),
    )
    wav = _wav(tmp_path / "20261112-000001-a.dictate.wav")
    main._jobs[1] = main.Job(1, main.MODE_DICTATE)      # path not set yet

    main.recover_pending_recordings()

    assert wav.exists()


def test_the_recorder_stops_before_the_slot_frees(q, monkeypatch, tmp_path):
    """
    Otherwise a key-down between the two starts the next take, resets the
    buffer, and the watchdog's stop of a 20-minute take returns the new one.
    """
    seen = []

    class Recorder:
        def stop(self):
            seen.append(main.state)
            return np.ones(main.MIN_AUDIO_SAMPLES, dtype=np.float32)

    monkeypatch.setattr(main, "recorder", Recorder())
    monkeypatch.setattr(
        main, "save_recording_with_fallback", lambda a, m: _wav(tmp_path / "s.dictate.wav")
    )
    monkeypatch.setattr(main, "process_pending_recording", lambda *a: None)
    monkeypatch.setattr(main, "state", "recording")
    monkeypatch.setattr(main, "current_hotkey", main.HOTKEY_KEYCODE)
    monkeypatch.setattr(main, "current_mode", main.MODE_DICTATE)

    main.on_key_up(main.HOTKEY_KEYCODE)
    main.on_key_up(main.HOTKEY_KEYCODE)      # the watchdog, a moment late

    assert seen == ["recording"]
    assert main.state == "idle" and len(main._jobs) == 1


def test_cancel_after_finalizing_a_recording_voids_its_take(q, monkeypatch, tmp_path):
    workers = []
    real_worker = main.process_pending_recording

    class Recorder:
        def stop(self):
            return np.ones(main.MIN_AUDIO_SAMPLES, dtype=np.float32)

    monkeypatch.setattr(main, "recorder", Recorder())
    monkeypatch.setattr(
        main, "save_recording_with_fallback", lambda a, m: _wav(tmp_path / "c.dictate.wav")
    )
    monkeypatch.setattr(main, "process_pending_recording", lambda *a: workers.append(a))
    monkeypatch.setattr(main, "state", "recording")
    monkeypatch.setattr(main, "current_hotkey", main.HOTKEY_KEYCODE)
    monkeypatch.setattr(main, "current_mode", main.MODE_DICTATE)

    stopped, _, _ = main.cancel_everything()
    time.sleep(0.05)

    assert stopped == "recording"
    assert main._jobs == {}
    assert (tmp_path / "c.dictate.wav").exists()
    # A worker that did start finds its job gone and pastes nothing.
    assert len(workers) == 1
    monkeypatch.setattr(main.transcriber, "transcribe_dictation_sync", lambda *a, **k: "x ")
    real_worker(*workers[0])
    assert q.pasted == []


def test_the_pill_at_each_step_of_a_take(q, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_schedule_drain", main._drain_ready_jobs)
    monkeypatch.setattr(main, "_microphone_access_is_blocked", lambda: False)
    samples = {"n": main.MIN_AUDIO_SAMPLES}

    class Recorder:
        def start(self):
            pass

        def stop(self):
            return np.ones(samples["n"], dtype=np.float32)

    monkeypatch.setattr(main, "recorder", Recorder())
    monkeypatch.setattr(
        main, "save_recording_with_fallback", lambda a, m: _wav(tmp_path / "p.dictate.wav")
    )
    monkeypatch.setattr(main, "process_pending_recording", lambda *a: None)
    monkeypatch.setattr(main, "current_hotkey", None)

    main.on_key_down(main.HOTKEY_KEYCODE, main.MODE_DICTATE)
    assert q.overlay.events[-1] == ("recording", main.MODE_LABELS.get(main.MODE_DICTATE), 0)

    main.on_key_up(main.HOTKEY_KEYCODE)
    assert q.overlay.events[-1] == ("loading", main.MODE_LABELS.get(main.MODE_DICTATE), 1)

    main._jobs.clear()
    samples["n"] = 10                    # an accidental tap
    main.on_key_down(main.HOTKEY_KEYCODE, main.MODE_DICTATE)
    main.on_key_up(main.HOTKEY_KEYCODE)
    assert q.overlay.events[-1] == ("hide",)


def test_a_mic_that_fails_to_start_lets_a_waiting_paste_through(q, monkeypatch):
    drains = []
    monkeypatch.setattr(main, "_schedule_drain", lambda: drains.append(1))
    monkeypatch.setattr(main, "_microphone_access_is_blocked", lambda: False)

    class Recorder:
        def start(self):
            raise RuntimeError("no input device")

    monkeypatch.setattr(main, "recorder", Recorder())
    monkeypatch.setattr(main, "current_hotkey", None)

    main.on_key_down(main.HOTKEY_KEYCODE, main.MODE_DICTATE)

    assert main.state == "idle"
    assert drains == [1]


def test_a_key_pressed_during_the_paste_turns_it_into_a_copy(monkeypatch):
    """The drain looked at the keys a moment before; ⌘V must look again."""
    posted, copied = [], []
    monkeypatch.setattr(main, "copy_to_clipboard", copied.append)
    monkeypatch.setattr(main, "CGPreflightPostEventAccess", lambda: True)
    monkeypatch.setattr(main, "CGEventPost", lambda *a: posted.append(a))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "_modifiers_held", lambda: True)

    assert main.paste_text("x") is False
    assert copied == ["x"] and posted == []


def test_cancel_leaves_a_done_take_to_paste(q, monkeypatch):
    """Take 2 finished and only waits for the stuck take 1: it is not stuck."""
    q.queue(1)
    q.queue(2)
    main._jobs[2].done = True
    main._jobs[2].text = "ready "
    for job in main._jobs.values():
        job.started -= main.CANCEL_GRACE_SEC + 1
    monkeypatch.setattr(main, "_schedule_drain", main._drain_ready_jobs)

    main.cancel_everything(min_age=main.CANCEL_GRACE_SEC)

    assert q.pasted == ["ready "]
    assert main._jobs == {}


def test_a_voided_take_is_held_even_if_its_file_is_young(q):
    """
    The file lands a moment after its job starts, so at the grace boundary
    the job can be cancellable while the file is not yet old enough to hold.
    """
    wav = q.queue(1)                       # written just now
    main._jobs[1].started -= main.CANCEL_GRACE_SEC + 1

    main.cancel_everything(min_age=main.CANCEL_GRACE_SEC)

    assert wav.name in main._pending_cancelled


def test_three_takes_ready_together_are_three_paragraphs(q):
    q.queue(1), q.queue(2), q.queue(3)
    q.finish(3, "third ")
    q.finish(2, "second.\n")
    q.finish(1, "first ")
    assert q.pasted == ["first\n\nsecond.\n\nthird "]


def test_a_take_that_is_only_whitespace_adds_no_gap(q):
    blank = [q.queue(1), q.queue(2), q.queue(3)][1]
    q.finish(2, "  ")
    q.finish(3, "c ")
    q.finish(1, "a ")
    assert q.pasted == ["a\n\nc "]
    assert not blank.exists()


def test_a_long_take_gets_longer_before_the_watchdog_drops_it(q, tmp_path):
    """
    A 20-minute take's STT timeout alone is 130 s per attempt, on two models.
    A flat limit would void it while its worker is still on track.
    """
    short = q.queue(1)
    long_wav = tmp_path / "long.dictate.wav"
    with open(long_wav, "wb") as f:
        f.truncate(20 * 60 * 32000)
    main._jobs[2] = main.Job(2, main.MODE_DICTATE)
    main._jobs[2].path = long_wav

    assert main._take_timeout(main._jobs[1]) < main.TAKE_TIMEOUT_SEC + 1
    assert main._take_timeout(main._jobs[2]) >= main.TAKE_TIMEOUT_SEC + 20 * main.TAKE_TIMEOUT_SEC_PER_AUDIO_MIN
    assert short.exists()
