"""
Capacity refusals vs spent quota.

Mistral reports both as HTTP 429. Code 3505 ("backend_out_of_capacity") is the
provider being momentarily full and is worth waiting out; code 1300
("rate_limited") is this account's own allowance and is not.
"""

import io
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

import transcriber as t


def _http_error(body, code=429):
    return HTTPError(
        "https://api.mistral.ai/v1/audio/transcriptions",
        code,
        "Too Many Requests",
        {},
        io.BytesIO(json.dumps(body).encode()),
    )


CAPACITY_BODY = {
    "message": "Not enough capacity available for this request, please retry later.",
    "type": "backend_out_of_capacity",
    "code": "3505",
}
QUOTA_BODY = {
    "message": "Rate limit exceeded",
    "type": "rate_limited",
    "code": "1300",
}


def test_capacity_refusal_is_distinguished_from_spent_quota():
    assert t.is_capacity_shortage(
        "HTTP Error 429: " + json.dumps(CAPACITY_BODY)
    )
    assert not t.is_capacity_shortage(
        "HTTP Error 429: " + json.dumps(QUOTA_BODY)
    )


def test_capacity_refusal_waits_longer_than_the_default_ladder():
    """The default ladder gives up in ~2s; a capacity burst outlasts that."""
    err = _http_error(CAPACITY_BODY)
    err_text = t._http_error_text(err)
    err.args = (err_text,)

    capacity_total = sum(t.CAPACITY_RETRY_DELAYS_SEC[: t.CAPACITY_MAX_ATTEMPTS - 1])
    default_total = sum(t.RETRY_DELAYS_SEC)
    assert capacity_total > default_total
    # Measured bursts ran tens of seconds, so the ladder must too.
    assert capacity_total >= 30


def test_capacity_refusal_retries_and_eventually_succeeds():
    """A burst that clears mid-ladder must yield a transcript, not an error."""
    attempts = {"n": 0}

    def fake_urlopen(req, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _http_error(CAPACITY_BODY)
        return io.BytesIO(json.dumps({"text": "recovered"}).encode())

    with patch.object(t, "urlopen", side_effect=fake_urlopen), patch.object(
        t.time, "sleep"
    ):
        result = t._request_json(object(), "Mistral transcription")

    assert result == {"text": "recovered"}
    assert attempts["n"] == 3


def test_capacity_refusal_still_waits_when_throttling_retry_is_off():
    """
    retry_throttling=False means "this key is spent, go elsewhere" — but a
    capacity refusal has nowhere else to go, so it must still be retried.
    """
    attempts = {"n": 0}

    def fake_urlopen(req, timeout):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _http_error(CAPACITY_BODY)
        return io.BytesIO(json.dumps({"text": "ok"}).encode())

    with patch.object(t, "urlopen", side_effect=fake_urlopen), patch.object(
        t.time, "sleep"
    ):
        result = t._request_json(
            object(), "Mistral translate", retry_throttling=False
        )

    assert result == {"text": "ok"}
    assert attempts["n"] == 2


def test_spent_quota_still_fails_fast_when_throttling_retry_is_off():
    """The 1300 fast-fail path must survive the capacity change."""
    attempts = {"n": 0}

    def fake_urlopen(req, timeout):
        attempts["n"] += 1
        raise _http_error(QUOTA_BODY)

    with patch.object(t, "urlopen", side_effect=fake_urlopen), patch.object(
        t.time, "sleep"
    ):
        with pytest.raises(t.TranscriptionError):
            t._request_json(
                object(), "Mistral translate", retry_throttling=False
            )

    assert attempts["n"] == 1


def test_overlay_wording_separates_capacity_from_quota():
    """
    The pill must not call a capacity shortage "rate limited": the two ask the
    user for opposite reactions, and the retry ladder is already working on the
    capacity one.
    """
    import main

    quota = "HTTP Error 429: Too Many Requests: " + json.dumps(QUOTA_BODY)
    capacity = "HTTP Error 429: Too Many Requests: " + json.dumps(CAPACITY_BODY)

    assert main.error_notice(Exception(quota)) == "Rate limited"
    assert main.error_notice(Exception(capacity)) == "Mistral busy"


def test_oversized_recording_is_rejected_before_the_upload(tmp_path):
    """
    A payload this large comes back as the same 3505 the retry ladder waits
    out, so it must be refused locally — otherwise it is retried forever.
    """
    wav = tmp_path / "huge.wav"
    wav.write_bytes(b"\0" * (t.MAX_UPLOAD_BYTES + 1))
    called = []

    with patch.object(t, "MISTRAL_API_KEY", "test-key"), patch.object(
        t, "urlopen", side_effect=lambda *a, **k: called.append(1)
    ):
        with pytest.raises(t.RequestRejectedError):
            t.Transcriber()._transcribe_mistral(wav)

    assert called == []


def test_a_numeric_capacity_code_still_reads_as_capacity():
    assert t.is_capacity_shortage(_http_error({"message": "full", "code": 3505}))
    assert not t.is_capacity_shortage(_http_error({"message": "x", "code": 1300}))


def test_a_spent_quota_is_not_retried_on_a_transcription(monkeypatch, tmp_path):
    """Retrying a spent allowance within seconds only spends more of it."""
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(t, "MISTRAL_API_KEY", "test-key")
    monkeypatch.setattr(t.time, "sleep", lambda s: None)
    with patch.object(t, "urlopen", side_effect=lambda *a, **k: (_ for _ in ()).throw(
        _http_error(QUOTA_BODY)
    )) as urlopen:
        with pytest.raises(t.TranscriptionError):
            t.Transcriber()._transcribe_mistral(wav)
    assert urlopen.call_count == 1


def test_an_error_body_never_carries_the_request_back():
    """A validation error quoting the dictation must not reach the log or pill."""
    error = _http_error(
        {
            "object": "error",
            "message": {"detail": [{"type": "string_too_long", "input": "мой текст"}]},
            "type": "invalid_request_error",
            "code": "3051",
        },
        code=422,
    )
    text = t._http_error_text(error)
    assert "мой текст" not in text
    assert "invalid_request_error" in text and "3051" in text


def test_a_null_text_block_does_not_break_a_translation():
    reply = {"choices": [{"message": {"content": [
        {"type": "thinking", "thinking": []},
        {"type": "text", "text": None},
        {"type": "text", "text": "Hello"},
    ]}}]}
    assert t._chat_text(reply) == "Hello"
