"""
Russian→English translate path: language passthrough on transcription,
Mistral chat-completion shape, provider errors, and empty-transcript short-circuit.
"""
import io
import json
import struct
from unittest.mock import patch
from urllib.error import HTTPError

import pytest


def _http_error(code, body=b""):
    return HTTPError("https://api.mistral.ai/", code, "test", {}, io.BytesIO(body))


def _ok_transcribe(text):
    class _Resp:
        def read(self):
            return json.dumps({"text": text}).encode()
    return _Resp()


def _ok_chat(content):
    class _Resp:
        def read(self):
            return json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    return _Resp()


@pytest.fixture
def transcriber_module(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-test-key")
    import importlib
    import transcriber as t
    importlib.reload(t)
    monkeypatch.setattr(t, "RETRY_DELAYS_SEC", (0, 0))
    yield t


def _write_silent_wav(tmp_path):
    p = tmp_path / "silent.wav"
    samplerate = 16000
    num_samples = samplerate
    data_size = num_samples * 2
    with open(p, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, samplerate, samplerate * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(b"\x00" * data_size)
    return p


def test_language_code_in_mistral_multipart(transcriber_module, tmp_path):
    t = transcriber_module
    wav = _write_silent_wav(tmp_path)
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = req.data
        return _ok_transcribe("привет")

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        t.Transcriber()._transcribe_mistral(wav, language="ru")

    assert b'name="language"' in captured["body"]
    assert b"\r\nru\r\n" in captured["body"]


def test_language_omitted_by_default(transcriber_module, tmp_path):
    t = transcriber_module
    wav = _write_silent_wav(tmp_path)
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = req.data
        return _ok_transcribe("hello")

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        t.Transcriber()._transcribe_mistral(wav)

    assert b'name="language"' not in captured["body"]


def test_translate_mistral_posts_chat_completions(transcriber_module):
    t = transcriber_module
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["ct"] = req.headers.get("Content-type")
        captured["authorization"] = req.headers.get("Authorization")
        return _ok_chat("Hello world")

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        result = t.Transcriber()._translate_mistral("Привет мир")

    assert result == "Hello world"
    assert captured["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert captured["ct"] == "application/json"
    assert captured["authorization"] == "Bearer mistral-test-key"
    assert captured["body"]["model"] == t.MISTRAL_TRANSLATION_MODEL
    # Bumped from 0.0 for more natural phrasing on conversational speech.
    assert captured["body"]["temperature"] == 0.2
    msgs = captured["body"]["messages"]
    assert msgs[0]["role"] == "system"
    assert "Russian" in msgs[0]["content"] and "English" in msgs[0]["content"]
    # The dictation is fenced off so a question or command is translated, not answered.
    assert msgs[1]["role"] == "user"
    assert "<dictation>\nПривет мир\n</dictation>" in msgs[1]["content"]
    assert captured["body"]["reasoning_effort"] == "low"


def test_reasoning_effort_goes_only_to_reasoning_models(transcriber_module):
    t = transcriber_module
    bodies = []

    def fake_urlopen(req, timeout):
        bodies.append(json.loads(req.data.decode()))
        return _ok_chat("ok")

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        t.Transcriber()._chat_completion("sys", "x", label="t", model="ministral-8b-latest")
        t.Transcriber()._chat_completion("sys", "x", label="t", model="mistral-large-4")
    assert "reasoning_effort" not in bodies[0]
    assert bodies[1]["reasoning_effort"] == "low"


def test_reasoning_reply_blocks_are_read_as_text(transcriber_module):
    t = transcriber_module
    reply = {"choices": [{"message": {"content": [
        {"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}]},
        {"type": "text", "text": "Hello"},
    ]}}]}
    assert t._chat_text(reply) == "Hello"


def test_a_failed_translation_falls_back_once(transcriber_module):
    t = transcriber_module
    models = []

    def fake_urlopen(req, timeout):
        model = json.loads(req.data.decode())["model"]
        models.append(model)
        if model == t.MISTRAL_TRANSLATION_MODEL:
            raise _http_error(429)
        return _ok_chat("Hello")

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        assert t.Transcriber()._translate_mistral("Привет") == "Hello"
    assert models == [t.MISTRAL_TRANSLATION_MODEL, t.MISTRAL_TRANSLATION_FALLBACK_MODEL]


def test_no_fallback_without_a_network(transcriber_module):
    t = transcriber_module
    with patch.object(
        t.Transcriber, "_chat_completion", side_effect=t.ProviderUnavailableError("offline")
    ) as chat:
        with pytest.raises(t.ProviderUnavailableError):
            t.Transcriber()._translate_mistral("Привет")
    assert chat.call_count == 1


def test_the_glossary_reaches_the_translator(transcriber_module, tmp_path, monkeypatch):
    t = transcriber_module
    vocabulary = tmp_path / "vocabulary.txt"
    vocabulary.write_text("пвашки = PWA apps\nкинг\n", encoding="utf-8")
    monkeypatch.setattr(t, "VOCABULARY_PATH", str(vocabulary))
    captured = {}

    def fake_urlopen(req, timeout):
        captured["system"] = json.loads(req.data.decode())["messages"][0]["content"]
        return _ok_chat("ok")

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        t.Transcriber()._translate_mistral("пвашки")
    assert "пвашки = PWA apps" in captured["system"]
    assert "кинг" not in captured["system"]


def test_translate_429_propagates(transcriber_module):
    t = transcriber_module

    def fake_urlopen(req, timeout):
        raise _http_error(429)

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        with pytest.raises(t.TranscriptionError):
            t.Transcriber()._translate_mistral("текст")


def test_translate_500_propagates(transcriber_module):
    t = transcriber_module

    def fake_urlopen(req, timeout):
        raise _http_error(500)

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        with pytest.raises(t.TranscriptionError):
            t.Transcriber()._translate_mistral("текст")


def test_transcribe_and_translate_skips_llm_on_empty(transcriber_module, tmp_path):
    """If the Russian transcript is empty (hallucination/silence), the LLM
    must NOT be called — both to save quota and to avoid the LLM inventing
    output from an empty input."""
    t = transcriber_module
    wav = _write_silent_wav(tmp_path)
    chat_calls = []

    def fake_urlopen(req, timeout):
        if "audio/transcriptions" in req.full_url:
            return _ok_transcribe("Спасибо за просмотр")  # known hallucination
        chat_calls.append(True)
        return _ok_chat("should not happen")

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        result = t.Transcriber().transcribe_and_translate_sync(wav)

    assert result == ""
    assert chat_calls == []


def test_transcribe_and_translate_happy_path(transcriber_module, tmp_path):
    t = transcriber_module
    wav = _write_silent_wav(tmp_path)
    captured_msgs = {}

    def fake_urlopen(req, timeout):
        if "audio/transcriptions" in req.full_url:
            return _ok_transcribe("Как дела?")
        body = json.loads(req.data.decode())
        captured_msgs["user"] = body["messages"][1]["content"]
        return _ok_chat("How are you?")

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        result = t.Transcriber().transcribe_and_translate_sync(wav)

    # Translator must see the clean Russian (no trailing space from transcribe_wav_sync)
    assert "<dictation>\nКак дела?\n</dictation>" in captured_msgs["user"]
    # Final output has the trailing space matching dictate-path convention
    assert result == "How are you? "


def test_translate_sets_russian_while_dictate_keeps_auto_detection(transcriber_module, tmp_path):
    t = transcriber_module
    wav = _write_silent_wav(tmp_path)
    captured_bodies = []

    def fake_urlopen(req, timeout):
        if "audio/transcriptions" in req.full_url:
            captured_bodies.append(req.data)
            return _ok_transcribe("")  # empty -> skips LLM
        return _ok_chat("")

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        t.Transcriber().transcribe_and_translate_sync(wav)
        t.Transcriber().transcribe_wav_sync(wav)  # dictate path

    assert b'name="language"' in captured_bodies[0]
    assert b"\r\nru\r\n" in captured_bodies[0]
    assert b'name="language"' not in captured_bodies[1]


def test_translate_makes_exactly_one_llm_call(transcriber_module, tmp_path):
    """
    The polishing pass was removed: a long translation must still cost one
    STT call plus one translate call, never a third round-trip.
    """
    t = transcriber_module
    wav = _write_silent_wav(tmp_path)
    long_enough = (
        "We need to rewrite the key rotation because it keeps failing with 429 "
        "errors, and the current retry budget is nowhere near enough."
    )

    def fake_urlopen(req, timeout):
        if "audio/transcriptions" in req.full_url:
            return _ok_transcribe("Надо переписать ротацию ключей, потому что падает 429.")
        return _ok_chat(long_enough)

    with patch.object(t, "urlopen", side_effect=fake_urlopen) as mock:
        result = t.Transcriber().transcribe_and_translate_sync(wav)

    assert result == long_enough[:-1] + " "
    assert mock.call_count == 2



@pytest.mark.parametrize("raw, pasted", [
    ("Wait — that's not it.", "Wait - that's not it"),
    ("Wait—that's not it", "Wait - that's not it"),
    ("Pages 10–20 and 30 – 40", "Pages 10-20 and 30-40"),
    ("— Hi.\n— Hello.", "- Hi.\n- Hello"),
    ("First one. Second one.", "First one. Second one"),
    ("Really?", "Really?"),
    ("Stop!", "Stop!"),
    ("And then...", "And then..."),
    ("Already-hyphenated word", "Already-hyphenated word"),
])
def test_finish_translation_style(transcriber_module, raw, pasted):
    assert transcriber_module._finish_translation(raw) == pasted


def test_translation_applies_the_paste_style(transcriber_module, tmp_path):
    t = transcriber_module
    wav = _write_silent_wav(tmp_path)

    def fake_urlopen(req, timeout):
        if "audio/transcriptions" in req.full_url:
            return _ok_transcribe("Ну вот, всё готово.")
        return _ok_chat("Well — it's done.")

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        result = t.Transcriber().transcribe_and_translate_sync(wav)

    assert result == "Well - it's done "


def test_raw_transcript_keeps_its_full_stop_for_the_translator(
    transcriber_module, tmp_path
):
    """The stop is dropped at paste time, not from what the translator reads."""
    t = transcriber_module
    wav = _write_silent_wav(tmp_path)
    with patch.object(t, "urlopen", return_value=_ok_transcribe("Всё готово.")):
        assert t.Transcriber().transcribe_wav_sync(wav) == "Всё готово. "


def test_a_translation_of_only_a_full_stop_pastes_nothing(
    transcriber_module, tmp_path
):
    t = transcriber_module
    wav = _write_silent_wav(tmp_path)

    def fake_urlopen(req, timeout):
        if "audio/transcriptions" in req.full_url:
            return _ok_transcribe("Ну.")
        return _ok_chat(".")

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        assert t.Transcriber().transcribe_and_translate_sync(wav) == ""


def test_a_long_text_gets_a_longer_wait(transcriber_module):
    t = transcriber_module
    timeouts = []

    def fake_urlopen(req, timeout):
        timeouts.append(timeout)
        return _ok_chat("ok")

    with patch.object(t, "urlopen", side_effect=fake_urlopen):
        t.Transcriber()._chat_completion("sys", "x", label="t")
        t.Transcriber()._chat_completion("sys", "x" * 20000, label="t")
    assert timeouts[0] < 31
    assert timeouts[1] >= 100


def test_a_busy_primary_hands_over_at_once(transcriber_module):
    t = transcriber_module
    models = []

    def fake_urlopen(req, timeout):
        model = json.loads(req.data.decode())["model"]
        models.append(model)
        if model == t.MISTRAL_TRANSLATION_MODEL:
            raise _http_error(429, b'{"code": "3505", "type": "backend_out_of_capacity"}')
        return _ok_chat("Hello")

    with patch.object(t, "urlopen", side_effect=fake_urlopen), patch.object(t.time, "sleep") as sleep:
        assert t.Transcriber()._translate_mistral("Привет") == "Hello"
    assert models == [t.MISTRAL_TRANSLATION_MODEL, t.MISTRAL_TRANSLATION_FALLBACK_MODEL]
    sleep.assert_not_called()


def test_a_rejected_key_does_not_try_the_fallback(transcriber_module):
    t = transcriber_module
    with patch.object(t, "urlopen", side_effect=_http_error(401)) as mock:
        with pytest.raises(t.TranscriptionError):
            t.Transcriber()._translate_mistral("Привет")
    assert mock.call_count == 1


def test_an_echoed_fence_is_not_pasted(transcriber_module):
    assert transcriber_module._finish_translation("<dictation>\nHello.\n</dictation>") == "Hello"
