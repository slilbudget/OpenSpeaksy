import sys
from pathlib import Path

import pytest

# Make the project root importable so tests can `from transcriber import ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _no_personal_vocabulary(monkeypatch, tmp_path):
    """
    The user's own vocabulary.txt sits in the project root on a dev machine.
    Tests that reload transcriber would pick it up and send extra form fields,
    so point every test at a file that does not exist. The env var survives
    importlib.reload, a patched module constant would not.
    """
    import transcriber

    missing = str(tmp_path / "no-vocabulary.txt")
    monkeypatch.setenv("OPENSPEAKSY_VOCABULARY", missing)
    monkeypatch.setattr(transcriber, "VOCABULARY_PATH", missing)
    # Tests count requests to one model; the fallback tests opt back in.
    monkeypatch.setenv("MISTRAL_FALLBACK_MODEL", "")
    monkeypatch.setattr(transcriber, "MISTRAL_FALLBACK_MODEL", "")
    monkeypatch.setattr(transcriber, "_primary_stt_skip_until", 0.0)


@pytest.fixture(autouse=True)
def _no_held_keys(monkeypatch):
    """The real check reads this Mac's keyboard; tests decide it themselves."""
    import main

    monkeypatch.setattr(main, "_modifiers_held", lambda: False)
    monkeypatch.setattr(main, "PASTE_SETTLE_SEC", 0.0)


@pytest.fixture(autouse=True)
def _reset_pending_retry_state():
    """
    The pending failure counts and the backoff schedule are module globals, so
    one test's failed file would otherwise still be "not due yet" in the next
    and silently skip its recovery pass.
    """
    import main

    _clear(main)
    yield
    _clear(main)


def _clear(main):
    main._pending_failures.clear()
    main._pending_next_attempt.clear()
    main._pending_attempt_count.clear()
    main._pending_cancelled.clear()
    main._last_cancel_tap = 0.0
    main._jobs.clear()
    main._last_paste_at = 0.0
    main._drain_recheck_armed = False
    main._held_since = None
    main._recovered_text.clear()
