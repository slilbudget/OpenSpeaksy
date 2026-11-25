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


def test_the_pill_splits_only_for_a_recording_with_takes_behind_it():
    assert o._split_target("recording", 0) == 0.0
    assert o._split_target("recording", 1) == 1.0
    assert o._split_target("loading", 3) == 0.0     # the spinner is the pill
    assert o._split_target("error", 3) == 0.0


def test_the_spinner_counts_from_two():
    assert o._spinner_count("recording", 1) is None
    assert o._spinner_count("recording", 2) == "2"
    assert o._spinner_count("loading", 1) is None
    assert o._spinner_count("loading", 3) == "3"
    assert o._spinner_count("loading", 12) == "9+"
    assert o._spinner_count("notice", 3) is None


def test_the_split_layout_puts_the_spinner_in_the_left_cap():
    w0, spin0, bars0 = o._split_layout(0.0)
    assert w0 == o.W and spin0 == bars0 == o.PANEL_W / 2.0
    w1, spin1, bars1 = o._split_layout(1.0)
    left = (o.PANEL_W - w1) / 2.0
    assert w1 == o.W + o.SPLIT_W
    assert spin1 == left + o.H / 2.0                # concentric with the cap
    # Bars clear the spinner with room to spare, inside the pill.
    assert bars1 - 12 > spin1 + o.SPIN_R + 10
    assert bars1 + 12 < left + w1 - 10


def test_the_glyphs_ease_like_the_pill():
    assert o._ease(0.0) == 0.0 and o._ease(1.0) == 1.0
    values = [o._ease(t / 20) for t in range(21)]
    assert values == sorted(values)
    assert abs(o._ease(0.5) - 0.776) < 0.01         # cubic-bezier(.4,0,.2,1)


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


def test_the_pill_can_show_over_full_screen_apps():
    """A full-screen app is its own Space; only FullScreenAuxiliary lets the
    pill appear there, so recording looked off in exactly those windows."""
    assert o.PANEL_BEHAVIOR & o.NSWindowCollectionBehaviorFullScreenAuxiliary
    assert o.PANEL_BEHAVIOR & o.NSWindowCollectionBehaviorCanJoinAllSpaces


# --- the split pill, frame by frame ------------------------------------------

def _split_rig(monkeypatch):
    """An OverlayView on a fake clock, recording what it draws, not drawing."""
    import types
    from AppKit import NSApplication
    from Foundation import NSMakeRect

    NSApplication.sharedApplication()
    o._init_colors()
    clock = [100.0]
    monkeypatch.setattr(o, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    drawn = []
    monkeypatch.setattr(
        o.OverlayView, "_draw_spinner",
        lambda self, cx, cy, alpha, count, scale=1.0: drawn.append(
            ("spin", round(cx - o.PANEL_W / 2, 1), round(alpha, 2), count)),
    )
    monkeypatch.setattr(
        o.OverlayView, "_draw_bars",
        lambda self, cx, cy, alpha: drawn.append(
            ("bars", round(cx - o.PANEL_W / 2, 1), round(alpha, 2))),
    )
    view = o.OverlayView.alloc().initWithFrame_(NSMakeRect(0, 0, o.PANEL_W, o.PANEL_H))

    def show(mode, queued=0):
        view.setMode_queued_(mode, queued)      # as Overlay._show does

    def run(seconds):
        frames = []
        for _ in range(int(round(seconds * 60))):
            clock[0] += 1 / 60
            view.tick_(None)
            drawn.clear()
            view._draw_overlay()
            frames.append(list(drawn))
        return frames

    return view, show, run


def _spins(frames):
    return [next((d for d in f if d[0] == "spin"), None) for f in frames]


def test_a_take_started_over_a_transcription_slides_the_spinner_aside(monkeypatch):
    view, show, run = _split_rig(monkeypatch)
    show("loading", 1)
    run(0.5)
    show("recording", 1)
    frames = run(0.4)
    xs = [s[1] for s in _spins(frames)]
    assert xs == sorted(xs, reverse=True)           # always moving left
    assert xs[-1] == -o.SPIN_SHIFT                  # settles in the cap
    bars = [next(d for d in f if d[0] == "bars")[2] for f in frames]
    assert bars[0] == 0.0 and bars[-1] == 1.0        # bars wait, then come in


def test_the_first_frame_of_a_slide_keeps_the_spinner(monkeypatch):
    """Drawn the instant the recording starts, before any tick: the spinner
    stays where it was and the bars stay hidden (no one-frame flash)."""
    view, show, run = _split_rig(monkeypatch)
    show("loading", 1)
    run(0.5)
    drawn = []
    monkeypatch.setattr(o.OverlayView, "_draw_spinner",
                        lambda self, cx, cy, alpha, count, scale=1.0: drawn.append(("spin", alpha)))
    monkeypatch.setattr(o.OverlayView, "_draw_bars",
                        lambda self, cx, cy, alpha: drawn.append(("bars", alpha)))
    show("recording", 1)
    view._draw_overlay()
    assert drawn == [("spin", 1.0), ("bars", 0.0)]


def test_the_spinner_turns_at_the_same_speed_whatever_the_frame_rate(monkeypatch):
    """A slow or late frame must not change the turn: it is per second."""
    angles = []
    for fps in (60, 20):
        view, show, run = _split_rig(monkeypatch)
        show("loading", 1)
        clock = o.time
        view.tick_(None)                       # first tick: no step yet
        start = view._spin
        for _ in range(fps // 2):              # half a second
            o.time = type(clock)(monotonic=lambda t=clock.monotonic() + 1 / fps: t)
            clock = o.time
            view.tick_(None)
        angles.append(round((view._spin - start) % 360.0, 6))
    assert angles[0] == angles[1] == round(o.SPIN_DEG_PER_SEC / 2 % 360.0, 6)


def test_a_fading_spinner_shows_the_count_it_last_had(monkeypatch):
    """2 takes behind, then 1, then none: the fade-out shows no number,
    and a later session does not inherit the old one."""
    view, show, run = _split_rig(monkeypatch)
    show("loading", 2)
    show("recording", 2)
    run(0.5)
    show("recording", 1)
    run(0.1)
    show("recording", 0)
    assert all(s[3] is None for s in _spins(run(0.4)) if s)

    view.snapSplit_(0.0)                            # straight from 2 to none
    show("loading", 2)
    show("recording", 2)
    run(0.5)
    show("recording", 0)
    assert [s[3] for s in _spins(run(0.1)) if s][0] == "2"

    view.snapSplit_(0.0)                            # hidden, then a new session
    show("recording", 1)
    run(0.5)
    show("recording", 0)
    assert all(s[3] is None for s in _spins(run(0.4)) if s)


def test_a_recording_ending_mid_fade_out_does_not_pop_the_spinner(monkeypatch):
    """The take behind finished, then the key came up a moment later: the new
    take's spinner starts in the middle, not at full strength in the cap."""
    view, show, run = _split_rig(monkeypatch)
    show("loading", 1)
    show("recording", 1)
    run(0.5)
    show("recording", 0)                            # fading out of the cap
    run(0.25)
    show("loading", 1)
    first = _spins(run(1 / 60))[0]
    assert first[1] == 0.0 and first[2] == 1.0


def test_a_faint_spinner_cut_short_restarts_in_the_middle(monkeypatch):
    """A fade-in into the cap barely begun when the recording ends."""
    view, show, run = _split_rig(monkeypatch)
    show("recording", 0)
    run(0.2)
    show("recording", 1)                            # fades in, in place
    run(0.05)
    show("loading", 1)
    first = _spins(run(1 / 60))[0]
    assert first[1] == 0.0 and first[2] == 1.0


def test_a_slide_that_reverses_fades_out_where_it_stopped(monkeypatch):
    """The take behind finished before the spinner reached the cap."""
    view, show, run = _split_rig(monkeypatch)
    show("loading", 1)
    run(0.5)
    show("recording", 1)
    stopped_at = _spins(run(0.15))[-1][1]
    show("recording", 0)
    frames = run(0.4)
    spins = [s for s in _spins(frames) if s]
    assert all(s[1] == stopped_at for s in spins)   # no ride over the bars
    alphas = [s[2] for s in spins]
    assert alphas == sorted(alphas, reverse=True)
    assert _spins(frames)[-1] is None
    bars = [next(d for d in f if d[0] == "bars")[2] for f in frames]
    assert bars == sorted(bars) and bars[-1] == 1.0


def test_the_spinner_glides_to_the_middle_when_the_recording_ends(monkeypatch):
    view, show, run = _split_rig(monkeypatch)
    show("loading", 2)
    run(0.5)
    show("recording", 2)
    run(0.5)
    show("loading", 2)
    frames = run(0.4)
    xs = [s[1] for s in _spins(frames)]
    assert xs == sorted(xs) and xs[-1] == 0.0
    assert all(s[2] == 1.0 and s[3] == "2" for s in _spins(frames))
    assert not any(d[0] == "bars" for f in frames for d in f)


def test_a_fade_in_cut_short_by_the_end_of_the_recording_does_not_jump(monkeypatch):
    view, show, run = _split_rig(monkeypatch)
    show("recording", 0)
    run(0.3)
    show("recording", 1)                            # fades into the cap
    run(0.12)
    show("loading", 1)
    first = _spins(run(1 / 60))[0]
    assert first[1] <= -o.SPIN_SHIFT + 1 and first[2] == 1.0


def test_a_hidden_pill_comes_back_without_a_ghost_spinner(monkeypatch):
    view, show, run = _split_rig(monkeypatch)
    show("loading", 1)
    run(0.5)
    show("recording", 1)
    run(0.5)
    view.snapSplit_(0.0)                            # Overlay._hide
    show("recording", 0)
    view.snapSplit_(o._split_target("recording", 0))    # Overlay._show, fresh
    frames = run(1 / 60)
    assert _spins(frames) == [None]
    assert next(d for d in frames[0] if d[0] == "bars")[1] == 0.0


def test_a_flash_keeps_the_queue_it_covers(monkeypatch):
    ov, drawn, timers = _overlay(monkeypatch)
    ov.show("recording", queued=2)
    ov.flash_error("No connection · retry in 10s")
    assert drawn[-1][0] == "error" and drawn[-1][3] == 2


def test_a_two_character_count_fits_the_arc():
    assert o._spinner_count("loading", 10) == "9+"
    assert o.COUNT_SIZE_WIDE < o.COUNT_SIZE
