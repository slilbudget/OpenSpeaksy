"""
Unattended disk hygiene for .pending/.

Two independent limits: a file the provider keeps refusing is set aside after
PENDING_MAX_FAILURES consecutive failures, and anything still pending after
PENDING_MAX_AGE_DAYS is deleted. Quarantined audio is never auto-deleted.
"""

import os
import time

import main


class StubTranscriber:
    def __init__(self, *, text=None, error=None):
        self.text = text
        self.error = error
        self.calls = 0

    def transcribe_dictation_sync(self, path, language=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.text


def _valid_wav(path):
    path.write_bytes(
        b"RIFF\x26\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
        b"\x01\x00\x01\x00\x80\x3e\x00\x00\x00\x7d\x00\x00"
        b"\x02\x00\x10\x00data\x02\x00\x00\x00\x00\x00"
    )


def _age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def _backoff_elapsed():
    """
    Pretend every scheduled retry is due. Failures now go on an escalating
    backoff, so a test driving several passes in a row would otherwise be
    skipped rather than retried.
    """
    main._pending_next_attempt.clear()


def _wire(monkeypatch, tmp_path, pending, transcriber, clipboard=None):
    # An empty list is falsy, so bind the caller's list explicitly rather than
    # with `clipboard or []` — that would silently swallow every write.
    if clipboard is None:
        clipboard = []
    monkeypatch.setattr(main, "PENDING_DIR", pending)
    monkeypatch.setattr(main, "QUARANTINE_DIR", pending / "quarantine")
    monkeypatch.setattr(main, "FALLBACK_PENDING_DIR", tmp_path / "fallback")
    monkeypatch.setattr(main, "transcriber", transcriber)
    monkeypatch.setattr(main, "copy_to_clipboard", clipboard.append)
    main._pending_failures.clear()


def test_expired_recording_is_deleted_without_being_transcribed(
    tmp_path, monkeypatch
):
    """An expired file must not cost a request on its way out."""
    pending = tmp_path / ".pending"
    pending.mkdir()
    wav = pending / "20260101-000000-a.dictate.wav"
    _valid_wav(wav)
    _age(wav, main.PENDING_MAX_AGE_DAYS + 1)

    stub = StubTranscriber(text="never reached")
    _wire(monkeypatch, tmp_path, pending, stub)

    main.recover_pending_recordings()

    assert not wav.exists()
    assert stub.calls == 0


def test_recording_inside_the_age_limit_survives(tmp_path, monkeypatch):
    pending = tmp_path / ".pending"
    pending.mkdir()
    wav = pending / "20260101-000000-a.dictate.wav"
    _valid_wav(wav)
    _age(wav, main.PENDING_MAX_AGE_DAYS - 1)
    clipboard = []

    stub = StubTranscriber(text="still good")
    _wire(monkeypatch, tmp_path, pending, stub, clipboard)

    main.recover_pending_recordings()

    assert clipboard == ["still good"]
    assert stub.calls == 1


def test_repeated_failures_quarantine_the_file(tmp_path, monkeypatch):
    """A poison file stops costing a request once it passes the cap."""
    pending = tmp_path / ".pending"
    pending.mkdir()
    wav = pending / "20260101-000000-a.dictate.wav"
    _valid_wav(wav)

    stub = StubTranscriber(error=RuntimeError("empty transcript"))
    _wire(monkeypatch, tmp_path, pending, stub)

    for _ in range(main.PENDING_MAX_FAILURES):
        _backoff_elapsed()
        main.recover_pending_recordings()

    assert not wav.exists()
    assert (pending / "quarantine" / wav.name).exists()
    assert stub.calls == main.PENDING_MAX_FAILURES

    # Quarantined audio is out of the retry loop for good.
    _backoff_elapsed()
    main.recover_pending_recordings()
    assert stub.calls == main.PENDING_MAX_FAILURES


def test_failure_streak_resets_after_a_success(tmp_path, monkeypatch):
    """A transient outage must not accumulate toward the quarantine cap."""
    pending = tmp_path / ".pending"
    pending.mkdir()
    wav = pending / "20260101-000000-a.dictate.wav"
    _valid_wav(wav)

    stub = StubTranscriber(error=RuntimeError("transient"))
    _wire(monkeypatch, tmp_path, pending, stub)

    for _ in range(main.PENDING_MAX_FAILURES - 1):
        _backoff_elapsed()
        main.recover_pending_recordings()
    assert wav.exists()

    assert main._pending_failures.get(wav.name) == main.PENDING_MAX_FAILURES - 1
    main._clear_pending_failures(wav.name)
    assert wav.name not in main._pending_failures


def test_quarantined_audio_is_never_auto_purged(tmp_path, monkeypatch):
    """Age expiry covers .pending/ only; quarantine is a human's call."""
    pending = tmp_path / ".pending"
    quarantine = pending / "quarantine"
    quarantine.mkdir(parents=True)
    kept = quarantine / "20260101-000000-a.dictate.wav"
    _valid_wav(kept)
    _age(kept, main.PENDING_MAX_AGE_DAYS * 10)

    stub = StubTranscriber(text="unused")
    _wire(monkeypatch, tmp_path, pending, stub)

    main.recover_pending_recordings()

    assert kept.exists()
