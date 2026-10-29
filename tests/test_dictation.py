"""Dictation path: the language reaches the request, and the paste style."""
import json
from unittest.mock import patch

import numpy as np
import pytest

import transcriber
from transcriber import Transcriber, write_wav


def _ok_transcribe(text):
    class _Resp:
        def read(self):
            return json.dumps({"text": text}).encode()
    return _Resp()


@pytest.fixture
def speech_wav(monkeypatch, tmp_path):
    monkeypatch.setattr(transcriber, "MISTRAL_API_KEY", "mistral-test-key")
    wav = str(tmp_path / "speech.wav")
    t = np.arange(16000) / 16000
    write_wav((0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), wav)
    return wav


def test_language_is_passed_through_to_transcription(speech_wav):
    with patch.object(transcriber, "urlopen", return_value=_ok_transcribe("привет")) as mock:
        Transcriber().transcribe_dictation_sync(speech_wav, language="ru")
    assert b'name="language"\r\n\r\nru' in mock.call_args_list[0].args[0].data


@pytest.mark.parametrize("raw, pasted", [
    ("Всё готово.", "Всё готово "),
    ("Первое. Второе.", "Первое. Второе "),
    ("Готово?", "Готово? "),
    ("Ну и вот...", "Ну и вот... "),
    ("Это — важно.", "Это — важно "),
])
def test_dictation_drops_only_the_closing_full_stop(speech_wav, raw, pasted):
    with patch.object(transcriber, "urlopen", return_value=_ok_transcribe(raw)):
        assert Transcriber().transcribe_dictation_sync(speech_wav) == pasted
