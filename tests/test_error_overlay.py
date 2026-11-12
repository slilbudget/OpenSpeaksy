"""
Error pill: turning a provider error into a readable notice, and sizing the
pill to whatever text it has to carry.
"""
import pytest

import main
import overlay as o
from transcriber import ProviderUnavailableError, TranscriptionError


@pytest.fixture(autouse=True)
def colors():
    # ERROR_ATTRS is built lazily on the AppKit side; measurement needs it.
    o._init_colors()


# --- turning an error into a notice ----------------------------------------

def test_a_wait_hint_becomes_the_headline():
    """The seconds left is the single most actionable thing to show."""
    notice = main.error_notice(
        TranscriptionError(
            "Mistral is rate-limited; next one frees up in 39s"
        )
    )
    assert notice == "Rate limited · 39s"


def test_a_retry_in_hint_is_also_read():
    notice = main.error_notice(TranscriptionError("Please retry in 12.4s"))
    assert notice == "Rate limited · 12s"


def test_throttling_without_a_hint_still_reads_as_throttling():
    assert main.error_notice(
        TranscriptionError("HTTP Error 429: Too Many Requests")
    ) == "Rate limited"


def test_a_dead_network_says_so():
    assert "No connection" == main.error_notice(
        ProviderUnavailableError("[Errno 8] nodename nor servname provided")
    )


@pytest.mark.parametrize(
    "raw, expected_fragment",
    [
        ("recording is too large for Mistral upload: 99 bytes", "Too long"),
        ("Mistral API key is not configured", "API key"),
        ("Microphone permission is denied", "Mic blocked"),
    ],
)
def test_known_failures_get_plain_language(raw, expected_fragment):
    assert expected_fragment in main.error_notice(TranscriptionError(raw))


def test_an_unrecognized_error_is_shown_verbatim_not_swallowed():
    """Hiding an unknown failure behind 'something went wrong' loses the clue."""
    assert main.error_notice(
        TranscriptionError("weird provider explosion")
    ) == "weird provider explosion"


def test_an_empty_error_still_says_something():
    assert main.error_notice(TranscriptionError("")) == "Failed"


# --- shaping the message ----------------------------------------------------

def test_log_style_whitespace_is_collapsed():
    assert o._clean_message("line one\n  line   two\t") == "line one line two"


def test_a_runaway_message_is_truncated_with_an_ellipsis():
    msg = o._clean_message("x" * 500)
    assert len(msg) <= o.ERROR_MAX_CHARS
    assert msg.endswith("…")


def test_no_message_stays_the_bare_flash():
    assert o._clean_message(None) is None
    assert o._clean_message("   ") is None


# --- sizing -----------------------------------------------------------------

def test_the_pill_widens_with_the_text():
    short = o._error_frame("Rate limited")
    longer = o._error_frame("No connection to the transcription service")
    assert longer.size.width > short.size.width


def test_the_pill_wraps_and_grows_taller_instead_of_running_off_screen():
    frame = o._error_frame(o._clean_message("A very long provider message " * 6))
    assert frame.size.width <= o.ERROR_MAX_W
    assert frame.size.height > o.ERROR_MIN_H


def test_the_pill_never_exceeds_the_panel():
    """The panel is built once at a fixed size; the pill must fit inside it."""
    for text in ("short", "x" * o.ERROR_MAX_CHARS, "слово " * 40):
        frame = o._error_frame(o._clean_message(text))
        assert frame.size.width <= o.PANEL_W - 2 * o.PAD
        assert frame.origin.x >= 0
        assert frame.origin.y + frame.size.height <= o.PANEL_H


def test_a_short_message_keeps_the_pill_height():
    assert o._error_frame("Rate limited").size.height == o.ERROR_MIN_H


def test_the_pill_stays_centered_in_the_panel():
    for text in ("short", "a much longer error message than that one"):
        frame = o._error_frame(text)
        center = frame.origin.x + frame.size.width / 2.0
        assert abs(center - o.PANEL_W / 2.0) < 0.51


# --- how long it stays up ---------------------------------------------------

def test_longer_text_stays_up_longer():
    assert o._read_time("x" * 100) > o._read_time("x" * 10)


def test_read_time_is_bounded():
    assert o._read_time("x") >= o.ERROR_FLASH_SEC
    assert o._read_time("x" * o.ERROR_MAX_CHARS) <= o.ERROR_MESSAGE_MAX_SEC


# --- what the pill returns to -----------------------------------------------

def _overlay(monkeypatch):
    """An Overlay whose drawing is recorded instead of done, with AppKit's
    main-thread hop run inline and the flash timer captured."""
    ov = o.Overlay()
    drawn = []
    timers = []
    monkeypatch.setattr(o.AppHelper, "callAfter", lambda fn, *a: fn(*a))
    monkeypatch.setattr(ov, "_show", lambda mode, label, message=None, queued=0: (
        setattr(ov, "_gen", ov._gen + 1), drawn.append((mode, label, message, queued))))
    monkeypatch.setattr(ov, "_hide", lambda: drawn.append("hidden"))

    class _Timer:
        def __init__(self, delay, fn):
            self.fn = fn
            timers.append(self)
            self.daemon = True

        def start(self):
            pass

    monkeypatch.setattr(o.threading, "Timer", _Timer)
    return ov, drawn, timers


def test_an_error_during_a_recording_returns_to_the_recording(monkeypatch):
    """
    With a queue, an earlier take can fail while the next one is recording.
    The error is shown, and then the recording pill comes back: the flash used
    to take the pill down, mid-sentence.
    """
    ov, drawn, timers = _overlay(monkeypatch)
    ov.show("recording", label=None, queued=1)
    ov.flash_error("No connection · retry in 10s")
    timers[-1].fn()
    assert drawn[-2][0] == "error"
    assert drawn[-1] == ("recording", None, None, 1)


def test_state_changes_during_a_flash_wait_for_it(monkeypatch):
    ov, drawn, timers = _overlay(monkeypatch)
    ov.flash_notice("Copied · ⌘V", sound=False)
    ov.show("loading", queued=2)
    assert drawn[-1][0] == "notice"     # the notice stays readable
    timers[-1].fn()
    assert drawn[-1] == ("loading", None, None, 2)


def test_only_the_latest_flash_ends_the_flashing(monkeypatch):
    ov, drawn, timers = _overlay(monkeypatch)
    ov.flash_error("First")
    ov.flash_error("Second")
    timers[0].fn()                      # the first one's timer fires late
    assert drawn[-1][2] == "Second"
    timers[1].fn()
    assert drawn[-1] == "hidden"


def test_queue_dots():
    assert o._queue_dots("recording", 0) == 0
    assert o._queue_dots("recording", 1) == 1    # one take behind the recording
    assert o._queue_dots("recording", 40) == o.DOT_MAX
    assert o._queue_dots("loading", 1) == 0      # the spinner is that one
    assert o._queue_dots("loading", 2) == 1      # one waits behind it
    assert o._queue_dots("loading", 3) == 2
    assert o._queue_dots("loading", 40) == o.DOT_MAX
    assert o._queue_dots("error", 3) == 0


def test_a_flash_does_not_freeze_the_pill(monkeypatch):
    ov, drawn, timers = _overlay(monkeypatch)
    ov.show("recording")
    ov.flash_error("x")
    timers[-1].fn()
    ov.show("loading", queued=2)
    assert drawn[-1] == ("loading", None, None, 2)
    ov.hide()
    assert drawn[-1] == "hidden"


def test_a_new_recording_cuts_a_long_message_short(monkeypatch):
    """Under a 6 s error the user could not see that the next take records."""
    ov, drawn, timers = _overlay(monkeypatch)
    ov.show("loading", queued=1)
    ov.flash_error("No connection · retry in 10s")
    ov.show("recording", queued=1)
    assert drawn[-1][0] == "error"          # still readable for a moment
    assert len(timers) == 2                 # but now ends early
    timers[-1].fn()
    assert drawn[-1] == ("recording", None, None, 1)
    # The flash's own, later timer is a no-op by then.
    timers[0].fn()
    assert drawn[-1] == ("recording", None, None, 1)


def test_a_flash_that_fails_to_draw_still_ends(monkeypatch):
    ov, drawn, timers = _overlay(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("AppKit said no")

    monkeypatch.setattr(ov, "_show", boom)
    try:
        ov.flash_error("x")
    except RuntimeError:
        pass
    assert timers, "the flash must end even if it never drew"


def test_a_message_over_a_recording_is_kept_short(monkeypatch):
    ov, drawn, timers = _overlay(monkeypatch)
    delays = []
    monkeypatch.setattr(ov, "_end_flash_after", lambda d, gen: delays.append(d))
    ov.show("recording")
    ov.flash_error("No connection · retry in 10s, and a long tail of words here")
    assert delays == [o.RECORDING_FLASH_MAX_SEC]
