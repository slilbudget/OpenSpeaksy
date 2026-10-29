import main


class StubTranscriber:
    def __init__(self, *, text=None):
        self.text = text

    def transcribe_dictation_sync(self, path, language=None):
        return self.text


def _valid_wav(path):
    path.write_bytes(
        b"RIFF\x26\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
        b"\x01\x00\x01\x00\x80\x3e\x00\x00\x00\x7d\x00\x00"
        b"\x02\x00\x10\x00data\x02\x00\x00\x00\x00\x00"
    )


def test_recovery_stops_after_provider_failure_to_unblock_hotkeys(
    tmp_path, monkeypatch
):
    pending = tmp_path / ".pending"
    pending.mkdir()
    first = pending / "20260721-000000-a.dictate.wav"
    second = pending / "20260721-000001-b.dictate.wav"
    _valid_wav(first)
    _valid_wav(second)
    calls = []

    class FailingTranscriber:
        def transcribe_dictation_sync(self, path, language=None):
            calls.append(path)
            raise main.ProviderUnavailableError("nodename nor servname")

    monkeypatch.setattr(main, "PENDING_DIR", pending)
    monkeypatch.setattr(main, "QUARANTINE_DIR", pending / "quarantine")
    monkeypatch.setattr(main, "FALLBACK_PENDING_DIR", tmp_path / "fallback")
    monkeypatch.setattr(main, "transcriber", FailingTranscriber())

    main.recover_pending_recordings()

    assert calls == [first]
    assert first.exists() and second.exists()
def test_recovery_processes_fallback_storage(tmp_path, monkeypatch):
    primary = tmp_path / ".pending"
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    wav = fallback / "20260721-000000-a.dictate.wav"
    _valid_wav(wav)
    clipboard = []

    monkeypatch.setattr(main, "PENDING_DIR", primary)
    monkeypatch.setattr(main, "QUARANTINE_DIR", primary / "quarantine")
    monkeypatch.setattr(main, "FALLBACK_PENDING_DIR", fallback)
    monkeypatch.setattr(main, "transcriber", StubTranscriber(text="recovered "))
    monkeypatch.setattr(main, "copy_to_clipboard", clipboard.append)

    main.recover_pending_recordings()

    assert clipboard == ["recovered "]
    assert not wav.exists()
