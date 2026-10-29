"""
The live worker and the pending-recovery pass: what happens to the audio and to
the clipboard when transcription fails, and what each is allowed to touch while
the other is running. Both were entirely untested.
"""
import struct
import threading

import pytest

import main
from transcriber import (
    NothingRecognizedError,
    ProviderUnavailableError,
    RequestRejectedError,
    TranscriptionError,
)


class _Overlay:
    def __init__(self):
        self.events = []

    def hide(self, token=None):
        self.events.append(("hide", token))

    def show(self, mode, label=None, token=None):
        self.events.append((mode, label))

    def flash_error(self, message=None, duration=None, token=None):
        self.events.append(("error", message))


def _wav(path, loud=True):
    samplerate = 16000
    data_size = samplerate * 2
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, samplerate, samplerate * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write((b"\x10\x20" if loud else b"\x00\x00") * samplerate)
    return path


@pytest.fixture
def worker(monkeypatch, tmp_path):
    """A processing job that owns the current generation."""
    overlay = _Overlay()
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "state", "processing")
    monkeypatch.setattr(main, "current_job_id", 7)
    monkeypatch.setattr(main, "PENDING_DIR", tmp_path)
    monkeypatch.setattr(main, "QUARANTINE_DIR", tmp_path / "quarantine")
    return overlay


def test_a_rejected_recording_is_quarantined_not_retried_forever(
    worker, monkeypatch, tmp_path
):
    """
    A payload the provider will never accept (too long for the backend) used to
    stay in .pending, where pending_retry_loop re-sent it every 5 minutes for
    the rest of the session. Set it aside instead: the audio is kept, the queue
    stops eating quota on it, and the user is told why.
    """
    wav = _wav(tmp_path / "20260827-000000-abc.dictate.wav")
    monkeypatch.setattr(
        main.transcriber,
        "transcribe_dictation_sync",
        lambda *a, **k: (_ for _ in ()).throw(
            RequestRejectedError("recording is too large for Mistral upload")
        ),
    )

    main.process_pending_recording(wav, 7, main.MODE_DICTATE)

    assert not wav.exists()
    assert (tmp_path / "quarantine" / wav.name).exists()
    assert ("error", "Recording is too long to transcribe") in worker.events


def test_a_take_with_nothing_recognized_is_set_aside_quietly(
    worker, monkeypatch, tmp_path
):
    """
    A click loud enough to pass the speech check, for which the provider keeps
    returning no text, used to sit behind an error pill for 9 minutes of
    retries. It goes straight to quarantine, audio kept, with a plain notice.
    """
    wav = _wav(tmp_path / "20261029-000000-abc.dictate.wav")
    monkeypatch.setattr(
        main.transcriber,
        "transcribe_dictation_sync",
        lambda *a, **k: (_ for _ in ()).throw(NothingRecognizedError("empty")),
    )

    main.process_pending_recording(wav, 7, main.MODE_DICTATE)

    assert not wav.exists()
    assert (tmp_path / "quarantine" / wav.name).exists()
    assert ("error", "Nothing recognized") in worker.events


def test_a_transient_failure_keeps_the_recording_for_retry(
    worker, monkeypatch, tmp_path
):
    wav = _wav(tmp_path / "20260827-000001-abc.dictate.wav")
    monkeypatch.setattr(
        main.transcriber,
        "transcribe_dictation_sync",
        lambda *a, **k: (_ for _ in ()).throw(
            ProviderUnavailableError("[Errno 8] nodename nor servname provided")
        ),
    )

    main.process_pending_recording(wav, 7, main.MODE_DICTATE)

    assert wav.exists()
    assert not (tmp_path / "quarantine").exists()
    # The countdown is the point: a saved, scheduled recording must not read
    # like a lost one.
    assert (
        "error",
        "No connection to the transcription service — retrying in 10s",
    ) in worker.events


def test_the_worker_hides_only_its_own_overlay_cycle(worker, monkeypatch, tmp_path):
    """
    The worker claims the job and state goes idle BEFORE the paste, so the user
    can legitimately start a new recording while it is still pasting. Passing
    the job id means a late hide cannot collapse the new cycle's pill.
    """
    wav = _wav(tmp_path / "20260827-000002-abc.dictate.wav")
    monkeypatch.setattr(
        main.transcriber, "transcribe_dictation_sync", lambda *a, **k: "text "
    )
    monkeypatch.setattr(main, "paste_text", lambda text: True)

    main.process_pending_recording(wav, 7, main.MODE_DICTATE)

    assert ("hide", 7) in worker.events


def test_a_stale_worker_touches_nothing(worker, monkeypatch, tmp_path):
    """A watchdog reset bumped the generation; this worker lost its claim."""
    wav = _wav(tmp_path / "20260827-000003-abc.dictate.wav")
    pasted = []
    monkeypatch.setattr(
        main.transcriber, "transcribe_dictation_sync", lambda *a, **k: "text "
    )
    monkeypatch.setattr(main, "paste_text", lambda text: pasted.append(text) or True)

    main.process_pending_recording(wav, 3, main.MODE_DICTATE)

    assert pasted == []
    assert wav.exists()
    assert worker.events == []


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    overlay = _Overlay()
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "PENDING_DIR", tmp_path)
    monkeypatch.setattr(main, "FALLBACK_PENDING_DIR", tmp_path / "fallback")
    monkeypatch.setattr(main, "QUARANTINE_DIR", tmp_path / "quarantine")
    monkeypatch.setattr(main, "state", "idle")
    monkeypatch.setattr(main, "current_wav_path", None)
    clipboard = []
    monkeypatch.setattr(main, "copy_to_clipboard", clipboard.append)
    return clipboard


def test_recovery_leaves_the_live_workers_recording_alone(
    recovery, monkeypatch, tmp_path
):
    """
    A cycle can start between the retry loop's idle check and the glob below it.
    Transcribing the live worker's file too would spend the quota twice and
    delete a file that worker still needs.
    """
    live = _wav(tmp_path / "20260827-000010-live.dictate.wav")
    queued = _wav(tmp_path / "20260827-000009-old.dictate.wav")
    monkeypatch.setattr(main, "current_wav_path", live)
    seen = []

    def _transcribe(path, language=None):
        seen.append(path)
        return "recovered "

    monkeypatch.setattr(main.transcriber, "transcribe_dictation_sync", _transcribe)

    main.recover_pending_recordings()

    assert seen == [queued]
    assert live.exists()
    assert not queued.exists()
    assert recovery == ["recovered "]


def test_recovery_quarantines_a_payload_the_provider_refuses(
    recovery, monkeypatch, tmp_path
):
    bad = _wav(tmp_path / "20260827-000011-big.dictate.wav")
    good = _wav(tmp_path / "20260827-000012-ok.dictate.wav")

    def _transcribe(path, language=None):
        if path == bad:
            raise RequestRejectedError("recording is too large")
        return "fine "

    monkeypatch.setattr(main.transcriber, "transcribe_dictation_sync", _transcribe)

    main.recover_pending_recordings()

    assert (tmp_path / "quarantine" / bad.name).exists()
    assert not good.exists()
    assert recovery == ["fine "]


def test_recovery_does_not_hold_the_clipboard_gate_across_the_network(
    recovery, monkeypatch, tmp_path
):
    """
    The bug this covers: the retry loop held _clipboard_gate for the whole pass,
    including every provider call. A live dictation finishing in that window
    blocked on the gate before its paste — long enough for the watchdog to
    void the job and drop the transcript back into .pending.
    """
    _wav(tmp_path / "20260827-000013-a.dictate.wav")
    held_during_network = []

    def _transcribe(path, language=None):
        held_during_network.append(main._clipboard_gate.locked())
        return "text "

    monkeypatch.setattr(main.transcriber, "transcribe_dictation_sync", _transcribe)

    main.recover_pending_recordings()

    assert held_during_network == [False]


def test_the_retry_loop_skips_a_pass_while_a_job_is_in_flight(monkeypatch):
    monkeypatch.setattr(main, "state", "processing")
    monkeypatch.setattr(main, "current_wav_path", None)
    ran = []
    monkeypatch.setattr(main, "recover_pending_recordings", lambda: ran.append(1))
    monkeypatch.setattr(main.time, "sleep", lambda _s: (_ for _ in ()).throw(_Stop()))

    with pytest.raises(_Stop):
        main.pending_retry_loop()
    assert ran == []


class _Stop(Exception):
    """Breaks an intentionally infinite loop out of its first sleep."""


def test_a_recovery_clipboard_failure_keeps_every_recording(
    recovery, monkeypatch, tmp_path
):
    kept = _wav(tmp_path / "20260827-000014-a.dictate.wav")
    monkeypatch.setattr(
        main.transcriber, "transcribe_dictation_sync", lambda *a, **k: "text "
    )
    monkeypatch.setattr(
        main,
        "copy_to_clipboard",
        lambda text: (_ for _ in ()).throw(RuntimeError("pasteboard rejected")),
    )

    main.recover_pending_recordings()

    assert kept.exists()


def test_the_gate_still_serializes_the_two_clipboard_writers(recovery, monkeypatch, tmp_path):
    """
    Narrowing the gate must not remove it: recovery's write still cannot land
    between a live worker's claim and its paste.
    """
    _wav(tmp_path / "20260827-000015-a.dictate.wav")
    monkeypatch.setattr(
        main.transcriber, "transcribe_dictation_sync", lambda *a, **k: "text "
    )
    observed = []
    monkeypatch.setattr(
        main, "copy_to_clipboard", lambda text: observed.append(main._clipboard_gate.locked())
    )

    main.recover_pending_recordings()

    assert observed == [True]


def test_a_corrupt_wav_is_quarantined_not_transcribed(recovery, monkeypatch, tmp_path):
    junk = tmp_path / "20260827-000016-junk.dictate.wav"
    junk.write_bytes(b"not a wav at all")
    calls = []
    monkeypatch.setattr(
        main.transcriber,
        "transcribe_dictation_sync",
        lambda *a, **k: calls.append(1) or "x ",
    )

    main.recover_pending_recordings()

    assert calls == []
    assert (tmp_path / "quarantine" / junk.name).exists()


def test_watchdog_processing_timeout_preserves_the_recording(monkeypatch, tmp_path):
    """
    The processing timeout resets state and reports it. The WAV must stay on
    disk: the retry loop is what eventually turns it into text.
    """
    overlay = _Overlay()
    wav = _wav(tmp_path / "20260827-000017-a.dictate.wav")
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "state", "processing")
    monkeypatch.setattr(main, "state_ts", -main.PROCESSING_TIMEOUT_SEC * 2)
    monkeypatch.setattr(main, "current_wav_path", wav)
    monkeypatch.setattr(main, "current_job_id", 4)

    main._watchdog_tick()

    assert main.state == "idle"
    assert main.current_job_id == 5
    assert main.current_wav_path is None
    assert wav.exists()
    assert ("error", "Transcription timed out, saved for retry") in overlay.events


def test_translate_mode_survives_recovery(recovery, monkeypatch, tmp_path):
    """Mode is carried in the filename, so recovery must route on it."""
    _wav(tmp_path / "20260827-000018-a.translate.wav")
    monkeypatch.setattr(
        main.transcriber, "transcribe_and_translate_sync", lambda p: "English "
    )
    monkeypatch.setattr(
        main.transcriber,
        "transcribe_dictation_sync",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("dictate path must not be used")
        ),
    )

    main.recover_pending_recordings()

    assert recovery == ["English "]


def test_processing_error_message_carries_the_providers_wait(worker, monkeypatch, tmp_path):
    """End to end: a provider 429 body becomes a number on screen."""
    wav = _wav(tmp_path / "20260827-000020-a.dictate.wav")
    monkeypatch.setattr(
        main.transcriber,
        "transcribe_dictation_sync",
        lambda *a, **k: (_ for _ in ()).throw(
            TranscriptionError(
                "HTTP Error 429: Too Many Requests: Quota exceeded for metric: "
                "mistral_requests, limit: 25. "
                "Please retry in 34.5s."
            )
        ),
    )

    main.process_pending_recording(wav, 7, main.MODE_DICTATE)

    assert ("error", "Rate limited — retrying in 35s") in worker.events
    assert wav.exists()


def test_no_stray_threads_are_left_behind():
    """
    Guards against a test above leaking a live timer into the next module.
    Named threads are the ones this app starts (workers, the watchdog, the
    pending-retry loop); pytest's own machinery is not among them.
    """
    leaked = [
        t.name
        for t in threading.enumerate()
        if t.is_alive()
        and not t.daemon
        and t is not threading.main_thread()
    ]
    assert leaked == [], f"threads still running: {leaked}"


def test_shutdown_waits_for_an_in_flight_save(monkeypatch):
    """
    on_key_up holds _save_gate from the moment the samples leave the recorder
    until the WAV is on disk. A signal arriving in that window used to exit the
    process on top of it, losing audio that existed only in a local variable.
    """
    order = []
    monkeypatch.setattr(main, "state", "processing")
    monkeypatch.setattr(main, "current_mode", None)
    monkeypatch.setattr(main, "os", _ExitRecorder(order))

    main._save_gate.acquire()
    try:
        done = threading.Event()

        def _shutdown():
            main.handle_shutdown(15, None)
            done.set()

        threading.Thread(target=_shutdown, daemon=True).start()
        assert not done.wait(0.3), "shutdown did not wait for the save"
        order.append("saved")
    finally:
        main._save_gate.release()

    assert done.wait(2.0), "shutdown never completed after the save"
    assert order == ["saved", "exit"]


def test_shutdown_is_not_blocked_forever_by_a_wedged_save(monkeypatch):
    order = []
    monkeypatch.setattr(main, "state", "idle")
    monkeypatch.setattr(main, "SHUTDOWN_SAVE_WAIT_SEC", 0.05)
    monkeypatch.setattr(main, "os", _ExitRecorder(order))

    main._save_gate.acquire()
    try:
        main.handle_shutdown(15, None)
    finally:
        main._save_gate.release()

    assert order == ["exit"]


class _ExitRecorder:
    """Stands in for the os module so _exit does not kill the test run."""

    def __init__(self, order):
        self._order = order

    def _exit(self, _code):
        self._order.append("exit")

    def __getattr__(self, name):
        import os as _os
        return getattr(_os, name)
