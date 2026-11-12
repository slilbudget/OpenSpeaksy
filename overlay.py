import math
import threading
import time
import objc
from AppKit import (
    NSView, NSPanel, NSColor, NSBezierPath, NSScreen, NSFont,
    NSFontAttributeName, NSForegroundColorAttributeName, NSKernAttributeName,
    NSFontWeightLight, NSFontWeightRegular,
    NSFontDescriptorSystemDesignRounded, NSLineCapStyleRound,
    NSMutableParagraphStyle, NSParagraphStyleAttributeName,
    NSLineBreakByWordWrapping, NSTextAlignmentCenter,
    NSStringDrawingUsesLineFragmentOrigin,
    NSWindowStyleMaskBorderless, NSBackingStoreBuffered,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSAnimationContext,
    NSInsetRect, NSViewLayerContentsRedrawDuringViewResize,
    NSSound,
)
from Foundation import NSMakeRect, NSMakePoint, NSMakeSize, NSAttributedString, NSTimer
from Quartz import CAMediaTimingFunction
from PyObjCTools import AppHelper

W, H = 70, 33
PAD = 16        # side / bottom margin around the pill
LABEL_PAD = 18  # extra top margin for the mode label
MARGIN = 145
FPS = 1.0 / 60.0

# An error pill carries a sentence, so it grows past the glyph-sized W. The
# panel is built once at the widest size any state can need and never resized;
# only the pill inside it animates, which keeps the window position stable.
ERROR_TEXT_SIZE = 11.5
ERROR_TEXT_PAD_X = 14      # breathing room inside the pill, per side
ERROR_MAX_W = 420          # wrap beyond this; keeps the pill off screen edges
ERROR_LINE_GAP = 3.0
ERROR_MIN_H = H
ERROR_MAX_CHARS = 90       # Beyond this it stops being a glanceable notice
ERROR_FLASH_SEC = 1.2      # Bare "!" flash, and the floor for a message
ERROR_CHARS_PER_SEC = 22.0 # Rough comfortable reading pace
ERROR_MESSAGE_MAX_SEC = 6.0
# Longest a message may cover a recording: the user has to see it records.
RECORDING_FLASH_MAX_SEC = 2.0

# The panel must fit the TALLEST state, not the common one: a wrapped error
# message is several lines high, and a panel sized for the glyph would clip it.
# ERROR_MAX_CHARS at ERROR_MAX_W wraps to at most ERROR_MAX_LINES.
ERROR_MAX_LINES = 4
ERROR_PILL_MAX_H = (
    math.ceil(ERROR_TEXT_SIZE * 1.25) * ERROR_MAX_LINES
    + ERROR_LINE_GAP * (ERROR_MAX_LINES - 1)
    + 16
)
PANEL_W = ERROR_MAX_W + 2 * PAD
PANEL_H = max(H + PAD + LABEL_PAD, math.ceil(ERROR_PILL_MAX_H) + 2 * PAD)

# Animation durations (seconds). Calm ease, no overshoot.
EXPAND_IN = 0.32
EXPAND_OUT = 0.22
ALPHA_IN = 0.18

_EASE = None

# One component, one look. All modes are the SAME dark pill; translate and
# hands-free only add a thin text label above it. The fill is a
# flat, opaque dark color (static — no blur, no adaptation), so the
# light content always reads on it. Error keeps the dark fill and a coral glyph.
FILL_RGBA = (36 / 255, 36 / 255, 36 / 255, 1.0)    # Dark pill fill — #242424 neutral gray-black
EDGE_RGBA = (1.0, 1.0, 1.0, 0.14)      # Soft light hairline rim
BAR_RGBA = (190 / 255, 190 / 255, 190 / 255, 1.0)    # Bars / spinner — #bebebe
BORDER_W = 1.0

# Queue dots: one per take the main glyph is not already showing — under a
# recording, every take still transcribing; under the spinner, the ones
# waiting behind it. They sit on a small capsule of the pill's own fill in the
# margin under the pill, so they read on a white page as well as a dark one.
DOT_D = 3.0
DOT_GAP = 3.5
DOT_MAX = 6
DOT_RGBA = (190 / 255, 190 / 255, 190 / 255, 0.95)
DOT_PAD_X = 4.0            # capsule padding around the row of dots
DOT_PAD_Y = 2.5

# Mode label ("English" / "Hands-free") — small soft rounded type.
LABEL_SIZE = 9.5
LABEL_TRACKING = 0.2       # Slight letter spacing for an airy, minimal look
LABEL_RGBA = (1.0, 1.0, 1.0, 0.60)     # Mode label — medium gray

# Error message type — same rounded family as the mode label, sized to read at
# a glance without dominating the screen.
ERROR_TEXT_RGBA = (1.0, 1.0, 1.0, 0.92)
# Modes whose pill is a block of text rather than a glyph. "notice" is drawn
# exactly like "error" — same dark pill, same type — because the difference
# that matters to the user is the words, not a color they would have to learn.
_MESSAGE_MODES = ("error", "notice")
# A notice means the clipboard changed without a paste (recovery, or a ⌘V
# that could not be posted), which is easy to miss while working in another
# window. The sound is what makes it noticeable.
NOTICE_SOUND = "Glass"

FILL = None
EDGE = None
DOT_COLOR = None
BAR_COLOR = None           # Recording bars / loading arc
ERROR_BAR = None           # Error "!"
LABEL_ATTRS = None
ERROR_ATTRS = None
CLEAR = None


def _init_colors():
    global FILL, EDGE, BAR_COLOR, ERROR_BAR, LABEL_ATTRS, ERROR_ATTRS, CLEAR, _EASE
    global DOT_COLOR
    if BAR_COLOR is None:
        c = NSColor.colorWithRed_green_blue_alpha_

        FILL = c(*FILL_RGBA)
        DOT_COLOR = c(*DOT_RGBA)
        EDGE = c(*EDGE_RGBA)
        BAR_COLOR = c(*BAR_RGBA)
        ERROR_BAR = c(236 / 255, 112 / 255, 102 / 255, 1.0)

        base = NSFont.systemFontOfSize_weight_(LABEL_SIZE, NSFontWeightLight)
        rounded = base.fontDescriptor().fontDescriptorWithDesign_(NSFontDescriptorSystemDesignRounded)
        label_font = NSFont.fontWithDescriptor_size_(rounded, LABEL_SIZE) or base
        err_base = NSFont.systemFontOfSize_weight_(
            ERROR_TEXT_SIZE, NSFontWeightRegular
        )
        err_rounded = err_base.fontDescriptor().fontDescriptorWithDesign_(
            NSFontDescriptorSystemDesignRounded
        )
        err_font = NSFont.fontWithDescriptor_size_(err_rounded, ERROR_TEXT_SIZE) or err_base
        wrap = NSMutableParagraphStyle.alloc().init()
        wrap.setLineBreakMode_(NSLineBreakByWordWrapping)
        wrap.setAlignment_(NSTextAlignmentCenter)
        wrap.setLineSpacing_(ERROR_LINE_GAP)
        ERROR_ATTRS = {
            NSFontAttributeName: err_font,
            NSForegroundColorAttributeName: c(*ERROR_TEXT_RGBA),
            NSParagraphStyleAttributeName: wrap,
        }
        LABEL_ATTRS = {
            NSFontAttributeName: label_font,
            NSForegroundColorAttributeName: c(*LABEL_RGBA),
            NSKernAttributeName: LABEL_TRACKING,
        }

        CLEAR = NSColor.clearColor()
        _EASE = CAMediaTimingFunction.functionWithControlPoints____(0.4, 0.0, 0.2, 1.0)


def _clean_message(message):
    """
    Collapse a raw provider error into one tidy line of prose. Log strings are
    written for logs, not for a pill floating over the user's work.
    """
    if not message:
        return None
    text = " ".join(str(message).split())
    if not text:
        return None
    if len(text) > ERROR_MAX_CHARS:
        text = text[: ERROR_MAX_CHARS - 1].rstrip(" ,.;:") + "…"
    return text


def _read_time(message):
    """Keep the pill up long enough to actually read it, within bounds."""
    seconds = ERROR_FLASH_SEC + len(message) / ERROR_CHARS_PER_SEC
    return max(ERROR_FLASH_SEC, min(seconds, ERROR_MESSAGE_MAX_SEC))


def _measure_error(message):
    """
    Text box size for an error message, wrapped at ERROR_MAX_W. Returns
    (text_w, text_h) in points; the caller pads it into a pill.
    """
    body = NSAttributedString.alloc().initWithString_attributes_(message, ERROR_ATTRS)
    limit = NSMakeSize(ERROR_MAX_W - 2 * ERROR_TEXT_PAD_X, 10000)
    # usesLineFragmentOrigin is required for multi-line measurement; without it
    # AppKit reports a single line's height no matter how the text wraps.
    box = body.boundingRectWithSize_options_(limit, NSStringDrawingUsesLineFragmentOrigin)
    return math.ceil(box.size.width), math.ceil(box.size.height)


def _error_frame(message):
    """Pill frame sized to hold `message`, centered in the fixed-width panel."""
    tw, th = _measure_error(message)
    w = min(ERROR_MAX_W, tw + 2 * ERROR_TEXT_PAD_X)
    # Clamped to the panel: a taller pill would draw its text outside the
    # window, where it is simply invisible.
    h = max(ERROR_MIN_H, min(th + 16, PANEL_H - 2 * PAD))
    return NSMakeRect((PANEL_W - w) / 2.0, PAD, w, h)


def _queue_dots(mode, queued):
    """How many queue dots to draw under the pill in this mode."""
    if mode == "recording":
        return min(queued, DOT_MAX)
    if mode == "loading":
        return min(max(queued - 1, 0), DOT_MAX)
    return 0


def _full_frame():
    return NSMakeRect((PANEL_W - W) / 2.0, PAD, W, H)


def _circle_frame():
    return NSMakeRect((PANEL_W - H) / 2.0, PAD, H, H)


class PillView(NSView):
    """
    The pill body: a flat dark fill plus a thin light hairline rim, drawn
    together. Animates its frame with the expand/collapse, redrawing on resize.
    Fully static — no blur or backdrop adaptation.
    """

    def drawRect_(self, rect):
        b = self.bounds()
        r = b.size.height / 2.0
        FILL.setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(b, r, r).fill()

        # Stroke centered on a path inset by half the line width, so the rim's
        # outer edge lands on the pill edge.
        half = BORDER_W / 2.0
        inset = NSInsetRect(b, half, half)
        ir = inset.size.height / 2.0
        rim = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(inset, ir, ir)
        rim.setLineWidth_(BORDER_W)
        EDGE.setStroke()
        rim.stroke()


class GlyphView(NSView):
    """
    The animated glyph (bars / spinner / error) and the mode label, in a
    transparent sibling spanning the whole panel above the pill. The 60fps
    timer drives it via setNeedsDisplay.
    """

    def drawRect_(self, rect):
        self._owner._draw_overlay()


class OverlayView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(OverlayView, self).initWithFrame_(frame)
        if self is not None:
            self._mode = None
            self._label = None
            self._message = None
            self._queued = 0
            self._phase = 0.0
            _init_colors()
            self.setWantsLayer_(True)

            surface = PillView.alloc().initWithFrame_(_circle_frame())
            surface.setWantsLayer_(True)
            surface.setLayerContentsRedrawPolicy_(NSViewLayerContentsRedrawDuringViewResize)
            self.addSubview_(surface)
            self._surface = surface

            # Glyph + label sibling — full panel, animated by the timer.
            glyph = GlyphView.alloc().initWithFrame_(self.bounds())
            glyph._owner = self
            glyph.setWantsLayer_(True)
            self.addSubview_(glyph)
            self._glyph = glyph

            # Render layers at the screen's backing scale so small text stays
            # crisp (a layer at 1x looks pixelated on a Retina display).
            scale = NSScreen.mainScreen().backingScaleFactor()
            for v in (surface, glyph):
                if v.layer() is not None:
                    v.layer().setContentsScale_(scale)
        return self

    def setMode_(self, mode):
        if self._mode != mode:
            self._mode = mode
            self._phase = 0.0
            self._glyph.setNeedsDisplay_(True)

    def setLabel_(self, label):
        if self._label != label:
            self._label = label
            self._glyph.setNeedsDisplay_(True)

    def setQueued_(self, queued):
        if self._queued != queued:
            self._queued = queued
            self._glyph.setNeedsDisplay_(True)

    def setMessage_(self, message):
        if self._message != message:
            self._message = message
            self._glyph.setNeedsDisplay_(True)

    def _animate_surface(self, frame, duration):
        # Calm ease, no overshoot — the pill settles statically.
        NSAnimationContext.beginGrouping()
        ctx = NSAnimationContext.currentContext()
        ctx.setDuration_(duration)
        ctx.setTimingFunction_(_EASE)
        ctx.setAllowsImplicitAnimation_(True)
        self._surface.animator().setFrame_(frame)
        NSAnimationContext.endGrouping()

    def expandToMessage_(self, message):
        """Grow the pill to fit an error message instead of the glyph size."""
        self._animate_surface(_error_frame(message), EXPAND_IN)

    def expand(self):
        self._animate_surface(_full_frame(), EXPAND_IN)

    def collapse(self):
        self._animate_surface(_circle_frame(), EXPAND_OUT)

    def tick_(self, timer):
        if self._mode == "recording":
            self._phase += 0.1
            self._glyph.setNeedsDisplay_(True)
        elif self._mode == "loading":
            self._phase += 4.0
            self._glyph.setNeedsDisplay_(True)

    def _draw_overlay(self):
        # The glyph layer spans the whole panel; the pill occupies _full_frame,
        # so the glyph is drawn in pill-local coordinates and the label sits in
        # the margin above it.
        if self._label and self._mode not in _MESSAGE_MODES:
            s = NSAttributedString.alloc().initWithString_attributes_(self._label, LABEL_ATTRS)
            sz = s.size()
            x = (PANEL_W - sz.width) / 2.0
            y = PAD + H + (LABEL_PAD - sz.height) / 2.0
            s.drawAtPoint_(NSMakePoint(x, y))

        cx, cy = PANEL_W / 2.0, PAD + H / 2.0

        dots = _queue_dots(self._mode, self._queued)
        if dots:
            row = dots * DOT_D + (dots - 1) * DOT_GAP
            x0 = cx - row / 2.0
            y = (PAD - DOT_D) / 2.0
            capsule = NSMakeRect(
                x0 - DOT_PAD_X, y - DOT_PAD_Y,
                row + 2 * DOT_PAD_X, DOT_D + 2 * DOT_PAD_Y,
            )
            r = capsule.size.height / 2.0
            FILL.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(capsule, r, r).fill()
            DOT_COLOR.setFill()
            for i in range(dots):
                NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(x0 + i * (DOT_D + DOT_GAP), y, DOT_D, DOT_D)
                ).fill()

        if self._mode in _MESSAGE_MODES:
            if self._message:
                # Center the wrapped text on the pill the surface animated to,
                # so text and pill can never disagree about where they are.
                pill = self._surface.frame()
                tw, th = _measure_error(self._message)
                body = NSAttributedString.alloc().initWithString_attributes_(
                    self._message, ERROR_ATTRS
                )
                box = NSMakeRect(
                    pill.origin.x + (pill.size.width - tw) / 2.0,
                    pill.origin.y + (pill.size.height - th) / 2.0,
                    tw,
                    th,
                )
                body.drawWithRect_options_(box, NSStringDrawingUsesLineFragmentOrigin)
            else:
                # "!" — vertical bar + dot
                ERROR_BAR.setFill()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(cx - 1, cy - 4, 2, 10), 1, 1
                ).fill()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(cx - 1, cy - 8, 2, 2), 1, 1
                ).fill()

        elif self._mode == "recording":
            # Center-weighted envelope reads like a real voice meter.
            env = (0.55, 0.8, 1.0, 0.8, 0.55)
            barW, gap = 2.0, 3.5
            total = 5 * barW + 4 * gap
            x0 = cx - total / 2.0
            BAR_COLOR.setFill()
            for i in range(5):
                t = math.sin(self._phase + i * 1.2) * 0.5 + 0.5
                h = 5 + t * 11 * env[i]
                y = cy - h / 2.0
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(x0 + i * (barW + gap), y, barW, h), 1, 1
                ).fill()

        elif self._mode == "loading":
            # A single clean arc, round caps, no background track — a calm,
            # minimal spinner.
            BAR_COLOR.setStroke()
            arc = NSBezierPath.bezierPath()
            arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                NSMakePoint(cx, cy), 7.5, self._phase, self._phase + 270, False
            )
            arc.setLineWidth_(2.0)
            arc.setLineCapStyle_(NSLineCapStyleRound)
            arc.stroke()


class Overlay:
    """
    The pill. Callers describe the current state with show()/hide() — the
    "base" — and flash a message over it with flash_error()/flash_notice().
    A flash never ends the base: when it times out, the pill goes back to
    whatever the base is by then, so an error from one take cannot cover or
    take down the recording of the next.
    """

    def __init__(self):
        self._panel = None
        self._view = None
        self._timer = None
        self._hiding = False
        # Bumped on every _show. A flash's timer captures the value, so only
        # the most recent flash can end the flashing.
        self._gen = 0
        self._base = None          # (mode, label, queued) or None for hidden
        self._flashing = False
        self._flash_started = 0.0

    def show(self, mode, label=None, queued=0):
        AppHelper.callAfter(self._set_base, (mode, label, queued))

    def hide(self):
        AppHelper.callAfter(self._set_base, None)

    def flash_error(self, message=None):
        """
        Show the error pill. With a message the pill widens to fit it and stays
        up long enough to read; without one it is the compact "!" flash.
        """
        message = _clean_message(message)
        duration = _read_time(message) if message else ERROR_FLASH_SEC
        AppHelper.callAfter(self._flash, "error", message, duration, False)

    def flash_notice(self, message, sound=True):
        """
        A neutral message pill — e.g. text landing in the clipboard instead of
        being pasted, which the user needs to know about even mid-dictation.
        """
        message = _clean_message(message)
        duration = _read_time(message)
        AppHelper.callAfter(self._flash, "notice", message, duration, sound)

    def _set_base(self, base):
        recording = base is not None and base[0] == "recording"
        was_recording = self._base is not None and self._base[0] == "recording"
        self._base = base
        if not self._flashing:
            self._render_base()
        elif recording and not was_recording:
            # A take just started under a long message (an earlier take's
            # error). The user must see it is recording: cut the message
            # short, once it has been up long enough to be glanced at.
            left = RECORDING_FLASH_MAX_SEC - (time.monotonic() - self._flash_started)
            self._end_flash_after(max(left, 0.0), self._gen)

    def _render_base(self):
        if self._base is None:
            self._hide()
        else:
            mode, label, queued = self._base
            self._show(mode, label, None, queued)

    def _flash(self, mode, message, duration, sound):
        if sound:
            try:
                tone = NSSound.soundNamed_(NOTICE_SOUND)
                if tone is not None:
                    tone.play()
            except Exception:
                # A missing system sound must never cost the user the notice.
                pass
        if self._base is not None and self._base[0] == "recording":
            duration = min(duration, RECORDING_FLASH_MAX_SEC)
        self._flashing = True
        self._flash_started = time.monotonic()
        try:
            self._show(mode, None, message)
        finally:
            # Always armed: a flash whose drawing failed must still end, or
            # every later show()/hide() would wait behind it forever.
            self._end_flash_after(duration, self._gen)

    def _end_flash_after(self, delay, gen):
        # daemon, so a pending timer never holds the process open on shutdown:
        # the pill is going away with the app anyway.
        timer = threading.Timer(
            delay, lambda: AppHelper.callAfter(self._end_flash, gen)
        )
        timer.daemon = True
        timer.start()

    def _end_flash(self, gen):
        if gen != self._gen:
            return  # a later flash owns the pill now
        self._flashing = False
        self._render_base()

    def _center(self):
        # Bottom-left of the glyph-sized pill on screen. The panel is a fixed
        # wide box centered on the same point, so a wider error pill grows
        # symmetrically without moving the window.
        sf = NSScreen.mainScreen().frame()
        x = sf.origin.x + (sf.size.width - W) / 2
        y = sf.origin.y + MARGIN - H
        return x, y

    def _panel_origin(self):
        x, y = self._center()
        return NSMakePoint(x - (PANEL_W - W) / 2.0, y - PAD)

    def _ensure_panel(self):
        # Build the panel once and keep it alive (hidden at alpha 0) between
        # cycles — no per-cycle teardown/recreate.
        if self._panel is not None:
            return
        origin = self._panel_origin()
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(origin.x, origin.y, PANEL_W, PANEL_H),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(1000)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
        )
        panel.setOpaque_(False)
        panel.setBackgroundColor_(CLEAR)
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setFloatingPanel_(True)
        panel.setAlphaValue_(0.0)

        view = OverlayView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, PANEL_H))
        panel.setContentView_(view)
        panel.orderFrontRegardless()

        self._panel = panel
        self._view = view

    def _show(self, mode, label, message=None, queued=0):
        _init_colors()
        self._ensure_panel()
        self._hiding = False
        self._gen += 1

        self._panel.setFrameOrigin_(self._panel_origin())
        self._view.setLabel_(label)
        self._view.setMessage_(message)
        self._view.setQueued_(queued)
        self._view.setMode_(mode)

        # The redraw timer runs only while the pill is visible.
        if self._timer is None:
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                FPS, self._view, b"tick:", None, True
            )

        # Fade the window in (re-targeted through the animator so an in-flight
        # fade-out can't keep driving it toward 0); expand the pill.
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(ALPHA_IN)
        self._panel.animator().setAlphaValue_(1.0)
        NSAnimationContext.endGrouping()

        if message:
            self._view.expandToMessage_(message)
        else:
            self._view.expand()

    def _hide(self):
        if not self._panel or self._hiding:
            return
        self._hiding = True

        self._view.collapse()

        # Fade out over the same span the pill collapses. The panel is kept
        # alive (not torn down).
        NSAnimationContext.beginGrouping()
        ctx = NSAnimationContext.currentContext()
        ctx.setDuration_(EXPAND_OUT)
        ctx.setCompletionHandler_(self._on_hide_done)
        self._panel.animator().setAlphaValue_(0.0)
        NSAnimationContext.endGrouping()

    def _on_hide_done(self):
        if self._hiding and self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        self._hiding = False
