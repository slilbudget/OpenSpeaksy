import json
import os
from unittest.mock import patch

import numpy as np

import transcriber
from transcriber import Transcriber, Vocabulary, load_vocabulary, write_wav


def _vocab(tmp_path, text):
    path = tmp_path / "vocabulary.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _ok(text):
    class _Resp:
        def read(self):
            return json.dumps({"text": text}).encode()
    return _Resp()


def _speech_wav(tmp_path):
    wav = str(tmp_path / "speech.wav")
    t = np.arange(16000) / 16000
    write_wav((0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), wav)
    return wav


def test_terms_fixes_comments_and_blank_lines(tmp_path):
    path = _vocab(tmp_path, "# media buying\nадсеты\n\nкост-кап\nотсеты => адсеты\n")
    vocabulary = load_vocabulary(path)
    assert vocabulary.terms == ("адсеты", "кост-кап")
    assert vocabulary.fixes == (("отсеты", "адсеты"),)


def test_terms_the_endpoint_would_reject_are_skipped(monkeypatch, tmp_path):
    # A comma is HTTP 400 for the whole request on every model; whitespace is
    # too on Transcribe 2, while Transcribe 3 takes phrases.
    path = _vocab(tmp_path, "вайт  пейдж\nCPA,CPL\nкинг\n")
    monkeypatch.setattr(transcriber, "MISTRAL_MODEL", "voxtral-small-transcribe-3")
    assert load_vocabulary(path)[0] == ("вайт пейдж", "кинг")
    monkeypatch.setattr(transcriber, "MISTRAL_MODEL", "voxtral-mini-2602")
    assert load_vocabulary(path)[0] == ("кинг",)


def test_terms_are_capped_at_the_endpoint_limit(tmp_path):
    words = "\n".join(f"term{i}" for i in range(transcriber.CONTEXT_BIAS_MAX_TERMS + 20))
    assert len(load_vocabulary(_vocab(tmp_path, words)).terms) == transcriber.CONTEXT_BIAS_MAX_TERMS


def test_missing_file_means_no_vocabulary(tmp_path):
    assert load_vocabulary(str(tmp_path / "absent.txt")) == Vocabulary()


def test_an_edit_applies_without_a_restart(tmp_path):
    path = _vocab(tmp_path, "кинг\n")
    assert load_vocabulary(path)[0] == ("кинг",)
    with open(path, "w", encoding="utf-8") as f:
        f.write("кинг\nадсеты\n")
    os.utime(path, ns=(1, 1))  # same-second edits must still count
    assert load_vocabulary(path)[0] == ("кинг", "адсеты")


def test_fixes_match_whole_words_and_keep_a_leading_capital(tmp_path):
    vocabulary = load_vocabulary(_vocab(tmp_path, "отсеты => адсеты\nпост бык => постбэк\n"))
    apply = transcriber._apply_fixes
    assert apply("Отсеты залил, пост  бык ушёл", vocabulary) == "Адсеты залил, постбэк ушёл"
    assert apply("ОТСЕТЫ", vocabulary) == "АДСЕТЫ"
    # Part of a longer word is not the misheard word.
    assert apply("подотсеты", vocabulary) == "подотсеты"


def test_a_fix_with_its_own_casing_keeps_it(tmp_path):
    vocabulary = load_vocabulary(_vocab(tmp_path, "айос => iOS\nраи => ROI\n"))
    assert transcriber._apply_fixes("Айос и АЙОС, раи", vocabulary) == "iOS и iOS, ROI"


def test_fixes_run_in_one_pass_longest_first(tmp_path):
    vocabulary = load_vocabulary(_vocab(tmp_path, "а => б\nб => в\nпост => ПОСТ\nпост бык => постбэк\n"))
    # "а" becomes "б", and that "б" is not rewritten again into "в".
    assert transcriber._apply_fixes("а б, пост бык, пост", vocabulary) == "б в, постбэк, ПОСТ"


def test_glossary_lines_and_a_byte_order_mark(tmp_path):
    path = tmp_path / "vocabulary.txt"
    path.write_text("# comment\nпвашки = PWA apps\nкинг\n", encoding="utf-8-sig")
    vocabulary = load_vocabulary(str(path))
    # The BOM must not turn the comment into a term.
    assert vocabulary.terms == ("кинг",)
    assert vocabulary.glossary == (("пвашки", "PWA apps"),)


def test_unprintable_lines_are_skipped(tmp_path):
    assert load_vocabulary(_vocab(tmp_path, "кинг\nбад\x00\n")).terms == ("кинг",)


def test_terms_go_out_as_context_bias_and_fixes_apply(monkeypatch, tmp_path):
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-test-key")
    monkeypatch.setattr(transcriber, "MISTRAL_API_KEY", "mistral-test-key")
    monkeypatch.setattr(
        transcriber, "VOCABULARY_PATH", _vocab(tmp_path, "адсеты\nкинг\nотсеты => адсеты\n")
    )
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = req.data
        return _ok("Залил отсеты на кинг.")

    with patch.object(transcriber, "urlopen", side_effect=fake_urlopen):
        text = Transcriber().transcribe_wav_sync(_speech_wav(tmp_path), language="ru")

    body = captured["body"]
    assert body.count(b'name="context_bias"') == 2
    assert "\r\nадсеты\r\n".encode() in body
    assert text == "Залил адсеты на кинг. "


def test_no_vocabulary_sends_no_context_bias(monkeypatch, tmp_path):
    monkeypatch.setattr(transcriber, "MISTRAL_API_KEY", "mistral-test-key")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = req.data
        return _ok("привет")

    with patch.object(transcriber, "urlopen", side_effect=fake_urlopen):
        Transcriber().transcribe_wav_sync(_speech_wav(tmp_path))

    assert b"context_bias" not in captured["body"]


def test_command_mood_turns_misheard_first_person_into_imperative():
    commands = transcriber._as_commands
    assert commands("Проанализирую этот код и протестирую.") == "Проанализируй этот код и протестируй."
    assert commands("реализую это, оптимизирую запросы") == "реализуй это, оптимизируй запросы"
    assert commands("Проверю код, напишу тесты и посмотрю логи") == "Проверь код, напиши тесты и посмотри логи"
    assert commands("Сделаю коротко. ПОПРОБУЮ ещё") == "Сделай коротко. ПОПРОБУЙ ещё"
    assert commands("Пофикшу кнопку") == "Пофикси кнопку"


def test_command_mood_keeps_real_first_person_and_other_words():
    commands = transcriber._as_commands
    # "я" earlier in the same sentence means the speaker meant themselves.
    assert commands("Я завтра сам это проанализирую.") == "Я завтра сам это проанализирую."
    # A new sentence is judged on its own.
    assert commands("Я занят. Проанализирую код") == "Я занят. Проанализируй код"
    assert commands("Я сейчас проверю и напишу") == "Я сейчас проверю и напишу"
    # "я" after the verb counts too, and so does a negation or a condition.
    assert commands("Проверю я это завтра") == "Проверю я это завтра"
    assert commands("Не сделаю этого. Проверю код") == "Не сделаю этого. Проверь код"
    assert commands("Если проверю, скажу") == "Если проверю, скажу"
    # A dot inside a version number does not end the sentence.
    assert commands("Я думаю, что v1.2 проверю") == "Я думаю, что v1.2 проверю"
    # Only whole words: "проверюсь" is not "проверю".
    assert commands("проверюсь у врача") == "проверюсь у врача"
    # Adjectives and other -ую words are not verbs of this shape.
    assert commands("Используй другую модель и новую схему") == "Используй другую модель и новую схему"


def test_command_mood_is_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(transcriber, "MISTRAL_API_KEY", "mistral-test-key")
    assert transcriber.COMMAND_MOOD is False

    with patch.object(transcriber, "urlopen", return_value=_ok("Проанализирую код")):
        assert Transcriber().transcribe_wav_sync(_speech_wav(tmp_path)) == "Проанализирую код "

    monkeypatch.setattr(transcriber, "COMMAND_MOOD", True)
    with patch.object(transcriber, "urlopen", return_value=_ok("Проанализирую код")):
        assert Transcriber().transcribe_wav_sync(_speech_wav(tmp_path)) == "Проанализируй код "


def test_a_rejected_term_list_does_not_lose_the_take(monkeypatch, tmp_path):
    monkeypatch.setattr(transcriber, "MISTRAL_API_KEY", "mistral-test-key")
    monkeypatch.setattr(transcriber, "VOCABULARY_PATH", _vocab(tmp_path, "кинг\n"))
    rejected = transcriber.TranscriptionError("bad context_bias")
    rejected.code = 400
    calls = []

    def fake(self, wav_path, language=None, terms=()):
        calls.append(terms)
        if terms:
            raise rejected
        return "текст"

    with patch.object(Transcriber, "_transcribe_mistral", fake):
        assert Transcriber().transcribe_wav_sync(_speech_wav(tmp_path)) == "текст "
    assert calls == [("кинг",), ()]


def test_other_failures_are_not_retried_without_terms(monkeypatch, tmp_path):
    monkeypatch.setattr(transcriber, "VOCABULARY_PATH", _vocab(tmp_path, "кинг\n"))
    busy = transcriber.TranscriptionError("busy")
    busy.code = 429
    with patch.object(Transcriber, "_transcribe_mistral", side_effect=busy) as mock:
        try:
            Transcriber().transcribe_wav_sync(_speech_wav(tmp_path))
        except transcriber.TranscriptionError:
            pass
    assert mock.call_count == 1
