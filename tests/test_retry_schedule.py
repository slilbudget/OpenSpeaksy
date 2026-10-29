"""
A failed recording is retried on an escalating schedule instead of waiting for
a flat poll, and the user is told both that it will be retried and when a
recovered transcript lands in the clipboard.

The failure this guards against: a network blip drops one dictation, nothing
visible happens for five minutes, and the user re-records something the app
was about to deliver anyway.
"""

import struct

import pytest

import main
from transcriber import ProviderUnavailableError


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


def _wav(path):
    samplerate = 16000
    data_size = samplerate * 2
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, samplerate, samplerate * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(b"\x10\x20" * samplerate)
    return path


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    overlay = _Overlay()
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "PENDING_DIR", tmp_path)
    monkeypatch.setattr(main, "FALLBACK_PENDING_DIR", tmp_path / "fallback")
    monkeypatch.setattr(main, "QUARANTINE_DIR", tmp_path / "quarantine")
    monkeypatch.setattr(main, "state", "idle")
    monkeypatch.setattr(main, "current_wav_path", None)
    monkeypatch.setattr(main, "copy_to_clipboard", lambda text: None)
    return overlay


def test_backoff_escalates_and_then_holds():
    wav = main.Path("a.wav")
    steps = [
        main._schedule_pending_retry(wav)
        for _ in range(len(main.PENDING_RETRY_BACKOFF_SEC) + 2)
    ]
    assert steps[: len(main.PENDING_RETRY_BACKOFF_SEC)] == list(
        main.PENDING_RETRY_BACKOFF_SEC
    )
    # Past the end the last value repeats rather than growing without bound.
    assert steps[-1] == main.PENDING_RETRY_BACKOFF_SEC[-1]
    assert steps == sorted(steps)


def test_a_provider_wait_hint_beats_a_shorter_backoff():
    """Retrying before the provider said earns the same refusal again."""
    early = main._schedule_pending_retry(main.Path("b.wav"))
    assert early == main.PENDING_RETRY_BACKOFF_SEC[0]

    hinted = main._schedule_pending_retry(
        main.Path("other.wav"), Exception("next one frees up in 42.4s")
    )
    assert hinted == 42


def test_a_shorter_hint_does_not_shrink_the_backoff():
    wav = main.Path("c.wav")
    for _ in range(len(main.PENDING_RETRY_BACKOFF_SEC) - 1):
        main._schedule_pending_retry(wav)
    later = main._schedule_pending_retry(wav, Exception("retry in 1s"))
    assert later == main.PENDING_RETRY_BACKOFF_SEC[-1]


def test_a_scheduled_file_is_skipped_until_it_comes_due(recovery, monkeypatch, tmp_path):
    """A 5s poll must not re-send a recording the schedule just deferred."""
    wav = _wav(tmp_path / "20260922-000001-a.dictate.wav")
    calls = []

    def _transcribe(path, language=None):
        calls.append(path)
        raise ProviderUnavailableError("[Errno 32] Broken pipe")

    monkeypatch.setattr(main.transcriber, "transcribe_dictation_sync", _transcribe)

    main.recover_pending_recordings()
    assert calls == [wav]

    # Immediately again: still inside the backoff, so no second request.
    main.recover_pending_recordings()
    assert calls == [wav]

    # Once the wait has elapsed it is tried again.
    main._pending_next_attempt.clear()
    main.recover_pending_recordings()
    assert calls == [wav, wav]


def test_a_network_outage_never_counts_toward_quarantine(
    recovery, monkeypatch, tmp_path
):
    """
    An outage is not the recording's fault. Counting it would quarantine
    perfectly good audio after PENDING_MAX_FAILURES blips.
    """
    wav = _wav(tmp_path / "20260922-000002-a.dictate.wav")
    monkeypatch.setattr(
        main.transcriber,
        "transcribe_dictation_sync",
        lambda *a, **k: (_ for _ in ()).throw(
            ProviderUnavailableError("[Errno 32] Broken pipe")
        ),
    )

    for _ in range(main.PENDING_MAX_FAILURES + 2):
        main._pending_next_attempt.clear()
        main.recover_pending_recordings()

    assert wav.exists()
    assert not (tmp_path / "quarantine" / wav.name).exists()
    assert main._pending_failures.get(wav.name) is None


def test_a_recovered_transcript_announces_itself(recovery, monkeypatch, tmp_path):
    """
    Recovery replaces the clipboard while the user may be working elsewhere.
    Silence there means a stale paste later with no explanation.
    """
    _wav(tmp_path / "20260922-000003-a.dictate.wav")
    monkeypatch.setattr(
        main.transcriber, "transcribe_dictation_sync", lambda *a, **k: "recovered "
    )

    main.recover_pending_recordings()

    notices = [m for kind, m in recovery.events if kind == "notice"]
    assert len(notices) == 1
    assert "clipboard" in notices[0]


def test_a_failed_notice_still_lets_the_audio_be_released(
    recovery, monkeypatch, tmp_path
):
    """The text is already on the clipboard; a pill that cannot draw must not
    strand the file it came from."""
    wav = _wav(tmp_path / "20260922-000004-a.dictate.wav")
    monkeypatch.setattr(
        main.transcriber, "transcribe_dictation_sync", lambda *a, **k: "recovered "
    )

    def _boom(*a, **k):
        raise RuntimeError("overlay is gone")

    monkeypatch.setattr(recovery, "flash_notice", _boom)

    main.recover_pending_recordings()

    assert not wav.exists()
