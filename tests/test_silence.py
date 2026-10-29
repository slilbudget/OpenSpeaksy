"""
Room noise must read as "nothing said": no error pill, no retry, no quarantine.
Speech that comes back empty must still be retried, since that is a provider
failure that would otherwise silently drop what was dictated.
"""
from unittest.mock import patch

import numpy as np

import main
import transcriber
from transcriber import Transcriber, loudest_frame_rms, wav_has_speech, write_wav


class _Response:
    def __init__(self, text):
        self._body = ('{"text": "%s"}' % text).encode()

    def read(self):
        return self._body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _room_noise(seconds=2.0, level=0.0015, seed=0):
    """Like the empty recordings seen live: average above 0.001, no peak."""
    return np.random.default_rng(seed).normal(0, level, int(16000 * seconds)).astype(
        np.float32
    )


def _speech_like(seconds=2.0):
    """Mostly quiet, with one short loud burst, as a phrase with pauses is."""
    audio = _room_noise(seconds)
    audio[8000:9600] += 0.1 * np.sin(np.linspace(0, 200 * np.pi, 1600))
    return audio


def test_room_noise_is_not_speech(tmp_path):
    wav = tmp_path / "noise.wav"
    write_wav(_room_noise(), wav)
    assert not wav_has_speech(wav)


def test_one_loud_moment_is_speech_even_if_the_average_is_low(tmp_path):
    wav = tmp_path / "speech.wav"
    audio = _speech_like()
    write_wav(audio, wav)
    assert float(np.sqrt(np.mean(audio ** 2))) < 0.02
    assert wav_has_speech(wav)


def test_an_empty_transcript_of_room_noise_is_accepted_once(monkeypatch, tmp_path):
    wav = tmp_path / "noise.wav"
    write_wav(_room_noise(), wav)
    monkeypatch.setattr(transcriber, "MISTRAL_API_KEY", "k")

    with patch.object(transcriber, "urlopen", return_value=_Response("")) as urlopen:
        assert Transcriber().transcribe_dictation_sync(wav) == ""

    assert urlopen.call_count == 1


def test_an_empty_transcript_of_speech_is_still_retried(monkeypatch, tmp_path):
    wav = tmp_path / "speech.wav"
    write_wav(_speech_like(), wav)
    monkeypatch.setattr(transcriber, "MISTRAL_API_KEY", "k")
    monkeypatch.setattr(transcriber, "RETRY_DELAYS_SEC", (0, 0))
    responses = [_Response(""), _Response("привет")]

    with patch.object(transcriber, "urlopen", side_effect=responses) as urlopen:
        assert Transcriber().transcribe_wav_sync(wav) == "привет "

    assert urlopen.call_count == 2


def test_a_hallucination_over_room_noise_is_dropped(tmp_path):
    wav = tmp_path / "noise.wav"
    write_wav(_room_noise(), wav)
    with patch.object(Transcriber, "_transcribe_mistral", return_value="Спасибо за просмотр"):
        assert Transcriber().transcribe_wav_sync(wav) == ""


def test_loudest_frame_of_nothing_is_zero():
    assert loudest_frame_rms(np.zeros(0, dtype=np.float32)) == 0.0
    assert loudest_frame_rms(np.full(100, 0.5, dtype=np.float32)) == 0.5


def test_the_recording_limit_always_fits_the_upload_limit():
    """A forgotten hands-free recording must still be transcribable."""
    bytes_per_second = 16000 * 2
    assert main.RECORDING_TIMEOUT_SEC * bytes_per_second < transcriber.MAX_UPLOAD_BYTES


def test_quiet_real_speech_still_counts_as_speech(tmp_path):
    """The quietest real dictation measured on a MacBook mic peaked at 0.0104."""
    wav = tmp_path / "quiet.wav"
    audio = _room_noise(level=0.0005)
    audio[8000:9600] += 0.0104 * np.sqrt(2) * np.sin(np.linspace(0, 200 * np.pi, 1600))
    write_wav(audio, wav)
    assert wav_has_speech(wav)


def test_the_loudest_empty_take_measured_is_still_silence(tmp_path):
    """Takes with nothing said peaked at 0.0053 at most."""
    wav = tmp_path / "empty.wav"
    audio = _room_noise(level=0.0005)
    audio[8000:9600] += 0.0053 * np.sqrt(2) * np.sin(np.linspace(0, 200 * np.pi, 1600))
    write_wav(audio, wav)
    assert not wav_has_speech(wav)


def test_a_long_recording_gets_a_longer_socket_wait(tmp_path, monkeypatch):
    # The server is silent while it transcribes; a 20 min take needs well over
    # the flat 30 s, or every attempt times out and the take never lands.
    monkeypatch.setattr(transcriber, "MISTRAL_API_KEY", "mistral-test-key")
    timeouts = []

    class _Resp:
        def read(self):
            return b'{"text": "ok"}'

    def fake_urlopen(req, timeout):
        timeouts.append(timeout)
        return _Resp()

    for minutes in (0.1, 20):
        wav = str(tmp_path / f"{minutes}.wav")
        transcriber.write_wav(np.full(int(minutes * 60 * 16000), 0.1, np.float32), wav)
        with patch.object(transcriber, "urlopen", side_effect=fake_urlopen):
            transcriber.Transcriber()._transcribe_mistral(wav)

    assert timeouts[0] < 31
    assert timeouts[1] >= 85


def test_no_text_for_loud_audio_ends_as_nothing_recognized(tmp_path, monkeypatch):
    monkeypatch.setattr(transcriber, "MISTRAL_API_KEY", "mistral-test-key")
    monkeypatch.setattr(transcriber, "RETRY_DELAYS_SEC", (0, 0))
    wav = str(tmp_path / "click.wav")
    write_wav(np.full(16000, 0.1, np.float32), wav)

    class _Empty:
        def read(self):
            return b'{"text": ""}'

    with patch.object(transcriber, "urlopen", return_value=_Empty()) as mock:
        try:
            Transcriber().transcribe_dictation_sync(wav)
            raise AssertionError("expected NothingRecognizedError")
        except transcriber.NothingRecognizedError:
            pass
    # The quick in-request retries still happen: a provider blip gets its chance.
    assert mock.call_count == transcriber.REQUEST_MAX_ATTEMPTS
