import fcntl
import logging
import os
import re
import signal
import time
import threading
import uuid
import wave
from logging.handlers import RotatingFileHandler
from pathlib import Path

import objc
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory, NSPasteboard
from Foundation import NSBundle
from Quartz import (
    CGEventTapCreate, CGEventTapEnable,
    CGEventGetIntegerValueField, CGEventGetFlags,
    CGEventCreateKeyboardEvent, CGEventSetFlags, CGEventPost,
    CGPreflightListenEventAccess, CGPreflightPostEventAccess,
    CGEventMaskBit, CFMachPortCreateRunLoopSource,
    kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionListenOnly,
    kCGEventFlagsChanged, kCGKeyboardEventKeycode,
    kCGEventFlagMaskCommand, kCGHIDEventTap,
    kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput,
    CGEventSourceSecondsSinceLastEventType, kCGEventSourceStateHIDSystemState,
    kCGEventKeyDown, kCGEventLeftMouseDown, kCGEventRightMouseDown,
    CGEventSourceFlagsState, kCGEventFlagMaskAlternate, kCGEventFlagMaskShift,
    kCGEventFlagMaskControl,
)
from CoreFoundation import (
    CFRunLoopAddSource, CFRunLoopGetCurrent, CFRunLoopRun, kCFRunLoopDefaultMode,
)
from PyObjCTools import AppHelper

from recorder import Recorder
from transcriber import (
    is_capacity_shortage,
    loudest_frame_rms,
    DICTATE_LANGUAGE,
    MISTRAL_API_KEY,
    MISTRAL_FALLBACK_MODEL,
    MISTRAL_MODEL,
    MISTRAL_TRANSLATION_MODEL,
    Transcriber,
    TranscriptionError,
    ProviderUnavailableError,
    NothingRecognizedError,
    RequestRejectedError,
    write_wav,
)
from overlay import Overlay

# Hotkeys: right Command and right Option dictate, right Shift translates. Each
# needs its keycode and its device-dependent flag (right and left share the
# generic modifier flag).
HOTKEY_KEYCODE   = 0x36   # right Command
HOTKEY_FLAG      = 0x10   # NX_DEVICERCMDKEYMASK — distinguishes right Cmd from left
TRANSLATE_KEYCODE = 0x3C  # right Shift — dictate Russian, paste English
TRANSLATE_FLAG    = 0x04  # NX_DEVICERSHIFTKEYMASK — distinguishes right Shift from left
OPTION_KEYCODE = 0x3D     # right Option — dictates exactly like right Command
OPTION_FLAG    = 0x40     # NX_DEVICERALTKEYMASK
# The two interchangeable dictation keys: keycode -> (own flag, other key's flag).
DICTATION_KEYS = {
    HOTKEY_KEYCODE: (HOTKEY_FLAG, OPTION_FLAG),
    OPTION_KEYCODE: (OPTION_FLAG, HOTKEY_FLAG),
}
# Cancel. A modifier on purpose: the event tap subscribes to
# kCGEventFlagsChanged only, so an ordinary key like Escape or Space would mean
# subscribing to every keystroke the user types. Space would also fire
# constantly — a retry can run for minutes while the user is typing normally,
# and every space would cancel it.
# Caps Lock, chosen by elimination on a MacBook keyboard: there is no right
# Control, every LEFT modifier is part of everyday shortcuts (binding one would
# make Cmd+C cancel a transcription), the other right-hand modifiers are the
# dictation hotkeys, and fn already switches this user's input source.
# Verified on the real keyboard rather than from a header: keycode 0x39, flags
# 0x00010100. Caps Lock still toggles capitals — set it to "No Action" in
# System Settings > Keyboard > Modifier Keys to avoid that.
# A toggle: one event per press, so every event is one tap (see tap_callback).
CANCEL_KEYCODE = 0x39    # Caps Lock
# Two taps inside this window mean "try again now" rather than a second cancel.
CANCEL_DOUBLE_TAP_SEC = 0.6
# How long a transcription must have been running before a tap can call it off.
# A healthy one finishes in a second or two, so a tap before this is the user
# typing a capital letter, not asking to cancel — and silently killing a
# working transcription would be far worse than ignoring the key.
CANCEL_GRACE_SEC = 10.0
# Hands-free dictation: both dictation keys pressed together, in either order,
# record without either being held, and a tap of either one stops it. Holding
# a key blocks ordinary clicks, since macOS reads them as Cmd+click, and a long
# dictation is tiring to hold. Pressing the second key during a held
# dictation switches it to hands-free without losing what was already said.
HANDS_FREE_LABEL = "Hands-free"
MODE_DICTATE   = "dictate"
MODE_TRANSLATE = "translate"
# Overlay label per mode; dictate has none.
MODE_LABELS = {MODE_TRANSLATE: "English"}
V_KEY = 0x09
# Ignore accidental taps shorter than 0.8 seconds.
MIN_AUDIO_SAMPLES = 12800
PB_TYPE = "public.utf8-plain-text"
PROJECT_ROOT = Path(__file__).resolve().parent
PENDING_DIR = PROJECT_ROOT / ".pending"
QUARANTINE_DIR = PENDING_DIR / "quarantine"

# Watchdog: an independent poll thread resets stuck states. Triggers when a
# key-up is lost (Secure Input app, tap glitch, mid-recording crash) and the
# state machine would otherwise sit forever with audio buffering in memory.
# The hard limit stops a hands-free recording someone forgot about. Reaching
# it finalizes and transcribes the audio instead of discarding it. 20 minutes
# is about 38 MB at 16 kHz, well inside transcriber.MAX_UPLOAD_BYTES. Do not poll
# CGEventSourceKeyState for modifier ownership here: macOS can report a held
# right-side modifier as released, which would cut off valid dictation.
RECORDING_TIMEOUT_SEC = 20 * 60
# Translation makes two provider calls (STT, translate), each with bounded
# retries. Keep the watchdog above that legitimate retry budget so it never
# invalidates a worker still making progress.
PROCESSING_TIMEOUT_SEC = 360
WATCHDOG_POLL_SEC = 5
# How long a shutdown waits for an in-flight recording to reach disk. Only a
# wedged save should ever come close to it.
SHUTDOWN_SAVE_WAIT_SEC = 2.0

# Long-term observability
PENDING_AGE_WARN_DAYS = 7

# Unattended disk hygiene. A recording that no longer transcribes is not worth
# keeping forever: it is retried every PENDING_RETRY_POLL_SEC, and each attempt
# spends a rate-limit slot that live dictation needs. Two independent limits,
# because the two failure shapes differ:
#   - a file the provider keeps refusing gets set aside after N consecutive
#     recovery failures, so a single poison recording stops costing requests;
#   - anything still pending after N days is deleted outright, which is the
#     backstop for a long provider outage that ends with the audio too old to
#     be worth pasting anyway.
# Quarantined files are NEVER auto-deleted — a human decides, since that
# directory is the only copy of audio the app could not transcribe.
PENDING_MAX_FAILURES = 5
PENDING_MAX_AGE_DAYS = 14
# A recording whose transcription failed is retried on this escalating
# schedule rather than on a flat poll. The first step is short because the
# common case is a brief network drop and the user is still sitting there;
# the later ones stretch out so a long outage costs a handful of requests
# instead of one every few seconds. The last value repeats until the file is
# quarantined, purged, or finally goes through.
PENDING_RETRY_BACKOFF_SEC = (10, 30, 60, 120, 300)


# Bounded log file: 2 MB × 3 files = 6 MB max ever on disk
LOG_DIR = Path.home() / "Library/Logs/com.openspeaksy"
FALLBACK_PENDING_DIR = (
    Path.home() / "Library/Application Support/OpenSpeaksy/pending"
)
INSTANCE_LOCK_PATH = LOG_DIR / "instance.lock"
_logger = logging.getLogger("openspeaksy")
_logger.setLevel(logging.INFO)
_logger.propagate = False
_instance_lock_file = None

MICROPHONE_AUTH_NOT_DETERMINED = 0
MICROPHONE_AUTH_RESTRICTED = 1
MICROPHONE_AUTH_DENIED = 2
MICROPHONE_AUTH_AUTHORIZED = 3
MICROPHONE_MEDIA_TYPE = "soun"


def _install_file_handler():
    """
    Attach the rotating file handler. Called from main() — NOT at import —
    so that pytest (which imports main for state-machine tests) can't
    pollute the live agent's log file via the same handler.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(LOG_DIR, 0o700)
    log_path = LOG_DIR / "main.log"
    handler = RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    os.chmod(log_path, 0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(handler)


def log(msg):
    _logger.info(msg)


def _open_instance_lock(lock_path):
    """
    Return an exclusively locked file handle, or None if another OpenSpeaksy
    process already owns the lock. The caller must keep the handle alive for
    the lifetime of the process.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock_path.parent, 0o700)
    lock_file = lock_path.open("a+", encoding="ascii")
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def _acquire_instance_lock():
    global _instance_lock_file
    _instance_lock_file = _open_instance_lock(INSTANCE_LOCK_PATH)
    return _instance_lock_file is not None


def microphone_authorization_status():
    """
    Query macOS microphone authorization without adding the large
    pyobjc-framework-AVFoundation package. AVFoundation is part of macOS and
    can be loaded through the Objective-C runtime already used by the app.
    Returns None if the status cannot be queried.
    """
    try:
        bundle = NSBundle.bundleWithPath_(
            "/System/Library/Frameworks/AVFoundation.framework"
        )
        if bundle is None or not bundle.load():
            return None
        capture_device = objc.lookUpClass("AVCaptureDevice")
        return int(
            capture_device.authorizationStatusForMediaType_(
                MICROPHONE_MEDIA_TYPE
            )
        )
    except Exception as e:
        log(f"microphone authorization preflight unavailable: {e}")
        return None


def _microphone_access_is_blocked():
    status = microphone_authorization_status()
    return status in {
        MICROPHONE_AUTH_RESTRICTED,
        MICROPHONE_AUTH_DENIED,
    }


def handle_shutdown(signum, _frame):
    global state, state_ts, current_hotkey, current_mode

    # A key-up already in flight holds the samples in a local variable, a few
    # milliseconds from having them on disk — and this handler exits the process
    # outright. Wait for that save rather than landing on top of it. launchd
    # allows seconds before SIGKILL, so a bounded wait is free; the timeout only
    # exists so a wedged save cannot block shutdown forever.
    saving = _save_gate.acquire(timeout=SHUTDOWN_SAVE_WAIT_SEC)
    try:
        with state_lock:
            was_recording = state == "recording"
            mode = current_mode or MODE_DICTATE
            state = "idle"
            state_ts = time.monotonic()
            # Every queued take is already on disk; voiding the jobs keeps a
            # worker from pasting while the process goes down.
            _jobs.clear()
            current_hotkey = None
            current_mode = None
    finally:
        if saving:
            _save_gate.release()

    if was_recording:
        try:
            audio = recorder.stop()
            if len(audio) >= MIN_AUDIO_SAMPLES:
                path = save_recording_with_fallback(audio, mode)
                log(f"shutdown preserved recording: {path.name}")
            else:
                log(f"shutdown: recording held only {len(audio)} samples, nothing to keep")
        except Exception as e:
            log(f"shutdown recording preservation error: {e}")

    log(f"received signal {signum}, exiting")
    os._exit(128 + signum)


recorder = Recorder()
transcriber = Transcriber()
overlay = Overlay()

# The recording slot: "idle" or "recording". Only one take records at a time;
# any number of finished takes can be transcribing in _jobs meanwhile.
state = "idle"
state_ts = time.monotonic()
state_lock = threading.Lock()
# Serializes clipboard mutations between live-worker pastes and the
# background pending-recovery pass, so recovered text can never race a fresh
# dictation paste into the same clipboard. Also what keeps pastes in order.
_clipboard_gate = threading.Lock()
# Held by on_key_up from the moment the samples leave the recorder until the WAV
# is on disk. handle_shutdown waits on it, closing the window where a signal
# would exit the process while the only copy of the audio was a local variable.
_save_gate = threading.Lock()
# Id of the last take handed to transcription. Ids only grow, so they are also
# the order the takes were spoken in.
current_job_id = 0


class Job:
    """One finished take on its way from the recorder to the paste."""

    __slots__ = ("id", "mode", "path", "started", "done", "text", "notice", "rejected")

    def __init__(self, job_id, mode):
        self.id = job_id
        self.mode = mode
        self.path = None
        self.started = time.monotonic()
        self.done = False
        self.text = None
        self.notice = None
        self.rejected = False


# Takes being transcribed or waiting for their turn to paste, in the order they
# were spoken (dicts keep insertion order, and ids are inserted ascending).
# Guarded by state_lock. A worker whose job is no longer here was voided by a
# cancel, the watchdog or shutdown: it drops its result, the audio stays queued.
_jobs = {}
# Which hotkey owns the in-flight cycle. Set in on_key_down, consumed in on_key_up.
# A key-up event whose keycode doesn't match current_hotkey is ignored, so tapping
# the OTHER hotkey mid-record can't end the cycle. Watchdog also clears it on reset.
current_hotkey = None
current_mode = None
# Whether the recording cycle is hands-free. Written by
# _begin_recording for every cycle (and flipped by _latch_hands_free), read only while state is "recording",
# so the resets that leave "recording" never have to clear it.
current_hands_free = False
tap_ref = None
source_ref = None
SHUTDOWN_SIGNALS = {signal.SIGTERM, signal.SIGINT}
shutdown_read_fd = None
shutdown_write_fd = None


def begin_processing():
    """
    End the recording and queue its job in one critical section: the slot is
    free for the next take at once, and the job holds its place in the paste
    order from the moment the take ends, before its audio is even saved.
    Returns (job_id, mode), or (None, None) if nothing was recording.
    """
    global state, state_ts, current_job_id, current_hotkey, current_mode
    with state_lock:
        if state != "recording":
            return None, None
        state = "idle"
        state_ts = time.monotonic()
        current_job_id += 1
        mode = current_mode
        current_hotkey = None
        current_mode = None
        _jobs[current_job_id] = Job(current_job_id, mode)
        return current_job_id, mode


def _drop_job(job_id):
    """Take a job out of the queue; False if something already voided it."""
    with state_lock:
        return _jobs.pop(job_id, None) is not None


def shutdown_signal_loop():
    """
    Read signal numbers from Python's wakeup fd in a dedicated thread.

    The low-level signal handler writes to this pipe immediately even while the
    main thread is inside AppKit. The worker can therefore preserve an active
    recording without waiting for the main thread to execute Python bytecode.
    """
    while True:
        data = os.read(shutdown_read_fd, 1)
        if not data:
            continue
        signum = data[0]
        handle_shutdown(signum, None)


def _install_shutdown_handling():
    global shutdown_read_fd, shutdown_write_fd

    shutdown_read_fd, shutdown_write_fd = os.pipe()
    os.set_blocking(shutdown_write_fd, False)
    signal.set_wakeup_fd(shutdown_write_fd)
    for signum in SHUTDOWN_SIGNALS:
        # Installing any Python handler activates the low-level wakeup-fd
        # write. The Python callback itself is intentionally a no-op; the
        # dedicated reader performs the real shutdown work.
        signal.signal(signum, lambda _signum, _frame: None)
    threading.Thread(
        target=shutdown_signal_loop,
        name="openspeaksy-signal-reader",
        daemon=True,
    ).start()


def _watchdog_tick():
    """
    Single watchdog pass. The hard recording limit is routed through the normal
    on_key_up path, which stops, atomically saves, and processes every captured
    sample. A take stuck in transcription is dropped from the queue, so the
    takes behind it can paste; its audio stays in .pending for the retry loop.
    """
    finish_keycode = None
    now = time.monotonic()

    with state_lock:
        elapsed = now - state_ts
        if state == "recording" and elapsed > RECORDING_TIMEOUT_SEC:
            finish_keycode = current_hotkey
        stuck = [
            job for job in _jobs.values()
            if not job.done and now - job.started > PROCESSING_TIMEOUT_SEC
        ]
        for job in stuck:
            del _jobs[job.id]

    for job in stuck:
        log(
            f"watchdog: take {job.id} stuck in transcription for "
            f"{now - job.started:.0f}s, dropped from the queue; "
            + (f"recording kept in .pending: {job.path.name}" if job.path else "no file yet")
        )
    if stuck:
        overlay.flash_error("Timed out · saved")
        _schedule_drain()

    if finish_keycode is not None:
        log(f"watchdog: hard recording limit after {elapsed:.0f}s; finalizing captured audio")
        # on_key_up owns the normal stop → atomic save → worker path. If the
        # real key-up raced this call, its ownership check makes this a no-op.
        on_key_up(finish_keycode)


def watchdog_loop():
    while True:
        time.sleep(WATCHDOG_POLL_SEC)
        try:
            _watchdog_tick()
        except Exception as e:
            log(f"watchdog loop error: {e}")


# The loop only wakes up to check what is due — PENDING_RETRY_BACKOFF_SEC is
# what paces the actual requests.
PENDING_RETRY_POLL_SEC = 5


def pending_retry_loop():
    """
    Re-transcribe recordings that failed mid-session (dead network, provider
    outage) so they no longer wait for the next app restart. Mirrors startup
    recovery semantics: combined text goes to the clipboard only, never an
    unprompted paste.

    The clipboard gate is taken inside recover_pending_recordings, around the
    clipboard write alone. Holding it across the transcription calls would
    block a live dictation's paste for as long as the provider takes on every
    queued file — long enough for the watchdog to void that dictation.
    """
    while True:
        time.sleep(PENDING_RETRY_POLL_SEC)
        try:
            with state_lock:
                busy = state != "idle" or bool(_jobs)
            if busy:
                continue
            recover_pending_recordings()
        except Exception as e:
            log(f"pending retry loop error: {e}")


def copy_to_clipboard(text):
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    if not pb.setString_forType_(text, PB_TYPE):
        raise RuntimeError("pasteboard rejected transcription")


# A target app reads the clipboard when it handles ⌘V, which can be after the
# next paste has already replaced it. Back-to-back pastes wait this long apart.
PASTE_SETTLE_SEC = 0.3
_last_paste_at = 0.0


def _settle_clipboard():
    """Wait until the last paste has had PASTE_SETTLE_SEC to be read."""
    wait = PASTE_SETTLE_SEC - (time.monotonic() - _last_paste_at)
    if wait > 0:
        time.sleep(wait)


def paste_text(text):
    """
    Copy, then ⌘V. False when the ⌘V was not posted; the text is on the
    clipboard by then unless the copy itself raised.
    """
    global _last_paste_at
    try:
        _settle_clipboard()
        copy_to_clipboard(text)
        if not CGPreflightPostEventAccess():
            log("paste blocked: Accessibility permission is not trusted")
            return False
        time.sleep(0.05)
        # The drain checked the modifiers before it got here; a key pressed
        # since (the next take, a stop tap) would turn this into ⌥⌘V.
        if _modifiers_held():
            log("paste skipped: a modifier went down just before it")
            return False

        for press in (True, False):
            e = CGEventCreateKeyboardEvent(None, V_KEY, press)
            CGEventSetFlags(e, kCGEventFlagMaskCommand)
            CGEventPost(kCGHIDEventTap, e)

        _last_paste_at = time.monotonic()
        return True
    except Exception as e:
        log(f"paste error: {e}")
        return False


def _ensure_pending_dir(pending_dir):
    pending_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(pending_dir, 0o700)
    except OSError as e:
        log(f"chmod pending dir error: {e}")


def save_pending_recording(audio, mode, pending_dir=None):
    """
    Encode mode in the filename so a crash between save and worker spawn doesn't
    lose the language/translate intent. Filename: ...-<uuid>.<mode>.wav. Legacy
    pre-upgrade files without the mode segment are treated as dictate by
    parse_pending_mode().
    """
    pending_dir = PENDING_DIR if pending_dir is None else Path(pending_dir)
    _ensure_pending_dir(pending_dir)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}.{mode}.wav"
    final = pending_dir / name
    tmp = pending_dir / (name + ".tmp")
    write_wav(audio, tmp)
    try:
        os.chmod(tmp, 0o600)
    except OSError as e:
        log(f"chmod pending file error: {e}")
    os.replace(tmp, final)  # atomic — recovery never sees a half-written WAV
    return final


def save_recording_with_fallback(audio, mode):
    """
    Persist to the project pending directory first, then to a private directory
    under ~/Library/Application Support if the project directory is
    unavailable. Callers must still surface an error if both locations fail.
    """
    try:
        return save_pending_recording(audio, mode)
    except Exception as primary_error:
        log(f"primary pending save failed: {primary_error}; trying fallback")
        try:
            path = save_pending_recording(
                audio, mode, pending_dir=FALLBACK_PENDING_DIR
            )
        except Exception as fallback_error:
            raise RuntimeError(
                "could not preserve recording in primary or fallback storage"
            ) from fallback_error
        log(f"recording preserved in fallback storage: {path.name}")
        return path


def parse_pending_mode(path):
    """
    Filename format: <timestamp>-<uuid>.<mode>.wav. Returns the mode if present,
    or MODE_DICTATE for legacy files (pre-upgrade) with no mode segment.
    """
    stem = path.stem  # strips final .wav
    for mode in (MODE_TRANSLATE, MODE_DICTATE):
        if stem.endswith(f".{mode}"):
            return mode
    return MODE_DICTATE


def delete_pending_recording(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"delete pending recording error {path.name}: {e}")


# Consecutive recovery failures per pending file, keyed by name. In memory
# only: a restart re-arms every file, which is deliberate — a fresh process may
# be running against a fixed config, so it deserves a clean set of attempts.
_pending_failures = {}
_pending_failures_lock = threading.Lock()


# When each pending file may next be attempted, keyed by name (monotonic
# clock). In memory only, like _pending_failures: a restart means a fresh
# process that may have working network, so everything is due immediately.
_pending_next_attempt = {}
# Retries so far, per file — paces the backoff only. Separate from
# _pending_failures, which gates quarantine and must not count an outage.
_pending_attempt_count = {}
# Files the user cancelled. The audio is kept — only the automatic retries
# stop, so a stuck transcription can be called off without losing what was
# said. Cleared by a double tap, which puts everything back in the queue.
_pending_cancelled = set()


def _schedule_pending_retry(path, error=None):
    """
    Put a failed recording on the backoff schedule and return the delay used,
    so the caller can tell the user when it will be tried again.

    Paced by its OWN attempt count, deliberately separate from
    _pending_failures: that one decides quarantine and must ignore network
    outages, but the backoff has to keep stretching during one or a long
    outage would mean a request every PENDING_RETRY_BACKOFF_SEC[0] seconds
    for as long as it lasts.

    A provider that stated its own wait wins whenever it asks for longer:
    retrying before it said earns the same refusal again.
    """
    with _pending_failures_lock:
        attempt = _pending_attempt_count.get(path.name, 0) + 1
        _pending_attempt_count[path.name] = attempt
    step = PENDING_RETRY_BACKOFF_SEC[
        min(attempt - 1, len(PENDING_RETRY_BACKOFF_SEC) - 1)
    ]
    if error is not None:
        hint = provider_wait_hint(error)
        if hint is not None:
            step = max(step, hint)
    with _pending_failures_lock:
        _pending_next_attempt[path.name] = time.monotonic() + step
    return step


def _due_pending(paths):
    """The subset whose next-attempt time has arrived and is not cancelled."""
    now = time.monotonic()
    with _pending_failures_lock:
        return [
            p
            for p in paths
            if p.name not in _pending_cancelled
            and _pending_next_attempt.get(p.name, 0.0) <= now
        ]


def _note_pending_failure(path):
    """Count one failed recovery attempt; True once the file is past the cap."""
    with _pending_failures_lock:
        count = _pending_failures.get(path.name, 0) + 1
        _pending_failures[path.name] = count
    return count >= PENDING_MAX_FAILURES


def _clear_pending_failures(name):
    with _pending_failures_lock:
        _pending_failures.pop(name, None)
        _pending_next_attempt.pop(name, None)
        _pending_attempt_count.pop(name, None)
        _pending_cancelled.discard(name)


def purge_expired_pending(paths):
    """
    Delete pending recordings past PENDING_MAX_AGE_DAYS.

    The audio is the only copy, so this is the one place the app discards a
    recording the user never saw. It runs before transcription so an expired
    file is not first retried and then deleted in the same pass.
    Returns the paths that survived.
    """
    cutoff = time.time() - PENDING_MAX_AGE_DAYS * 86400
    kept = []
    expired = 0
    for path in paths:
        try:
            too_old = path.stat().st_mtime < cutoff
        except OSError:
            kept.append(path)
            continue
        if not too_old:
            kept.append(path)
            continue
        try:
            path.unlink()
            _clear_pending_failures(path.name)
            expired += 1
        except FileNotFoundError:
            expired += 1
        except OSError as e:
            log(f"purge error {path.name}: {e}")
            kept.append(path)
    if expired:
        log(
            f"purged {expired} pending recording(s) older than "
            f"{PENDING_MAX_AGE_DAYS}d"
        )
    return kept


def quarantine_path(path, reason):
    quarantine_dir = (
        QUARANTINE_DIR
        if path.parent == PENDING_DIR
        else path.parent / "quarantine"
    )
    quarantine_dir.mkdir(exist_ok=True, mode=0o700)
    target = quarantine_dir / path.name
    try:
        path.rename(target)
        log(f"quarantined {path.name}: {reason}")
    except OSError as e:
        log(f"quarantine error {path.name}: {e}")


def is_valid_wav(path):
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() > 0
    except Exception:
        return False


def _with_retry(message, retry_in):
    """Append the scheduled retry, when there is one."""
    if retry_in is None:
        return message
    return f"{message} · retry in {retry_in}s"


def provider_wait_hint(error):
    """
    How long the provider itself asked us to wait, in seconds, or None.

    Its own number beats any schedule we invent: retrying sooner than it said
    just earns the same refusal again.
    """
    match = re.search(
        r"(?:frees up in|retry in)\s*([\d.]+)\s*s", str(error), re.IGNORECASE
    )
    if not match:
        return None
    try:
        return int(float(match.group(1)) + 0.5)
    except ValueError:
        return None


def error_notice(error, retry_in=None):
    """
    A short, human notice for the overlay pill. Provider errors are written for
    logs ("HTTP Error 429: Too Many Requests"), which says nothing useful to
    someone who just spoke into their laptop.

    retry_in turns the message from a verdict into a status: the audio is
    saved and already scheduled, so the pill says when it will be tried again.
    Without that, a passing network blip reads as a lost dictation and the
    user re-records something the app is about to deliver anyway.
    """
    if isinstance(error, NothingRecognizedError):
        return "No speech"
    text = str(error).strip()
    if not text:
        return _with_retry("Failed", retry_in)

    hint = provider_wait_hint(error)
    if hint is not None:
        # The schedule already folded this hint in, so the countdown below
        # states it once rather than quoting two different numbers.
        return _with_retry("Rate limited", retry_in) if retry_in else (
            f"Rate limited · {hint}s"
        )
    # Mistral reports a capacity shortage and a spent quota with the same
    # status, so the body decides the wording. They call for opposite
    # reactions: a capacity refusal clears on its own and the retry ladder is
    # already working on it, while a spent quota means waiting for a reset.
    if is_capacity_shortage(error):
        return _with_retry("Mistral busy", retry_in)
    if "429" in text:
        return _with_retry("Rate limited", retry_in)
    if isinstance(error, ProviderUnavailableError):
        return _with_retry("No connection", retry_in)
    if "too large" in text.lower():
        return _with_retry("Too long", retry_in)
    if "api key" in text.lower():
        return _with_retry("API key rejected", retry_in)
    if "microphone" in text.lower():
        return _with_retry("Mic blocked", retry_in)

    # Unrecognized: show the provider's own words rather than swallowing them.
    # The overlay collapses whitespace and truncates, so a long one is safe.
    return _with_retry(text, retry_in)


def process_pending_recording(path, job_id, mode):
    """
    Live worker spawned by on_key_up, one per take; several run at once. It
    only transcribes and files the result on its job: pasting is
    _drain_ready_jobs's, which keeps the takes in the order they were spoken.
    Recovery uses recover_pending_recordings instead — it has different rules
    around the clipboard.
    """
    text = None
    notice = None
    rejected = False
    try:
        if mode == MODE_TRANSLATE:
            text = transcriber.transcribe_and_translate_sync(path)
        else:
            text = transcriber.transcribe_dictation_sync(path, language=DICTATE_LANGUAGE)
    except RequestRejectedError as e:
        # Unacceptable to the provider as-is, so a retry is guaranteed to fail
        # the same way. Keeping it pending would burn a quota slot every
        # 5 minutes for the rest of the session.
        log(f"transcription rejected {path.name}: {e}")
        notice = error_notice(e)
        rejected = True
    except TranscriptionError as e:
        log(f"transcription error {path.name}: {e}")
        notice = error_notice(e, retry_in=_schedule_pending_retry(path, e))
    except Exception as e:
        log(f"processing error {path.name}: {e}")
        notice = error_notice(e, retry_in=_schedule_pending_retry(path, e))

    with state_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.done = True
            job.text = text
            job.notice = notice
            job.rejected = rejected
    if job is None:
        # Voided by a cancel, the watchdog or shutdown. The audio stays in
        # .pending: the user called off the wait, not the words.
        log(f"stale worker abandoned: {path.name}")
        return
    _drain_ready_jobs()


DRAIN_RECHECK_SEC = 0.15
# A modifier that reads as down this long after the take is ready, with no
# recording going on, is taken to be stuck (a remapper, remote input, a key-up
# lost across a screen lock). The text goes to the clipboard instead of
# waiting behind it forever, and the queue behind it moves on.
MODIFIER_HOLD_MAX_SEC = 4.0
# Caps Lock is left out on purpose: it latches, and ⌘V works with it on.
_HELD_MODIFIERS = (
    kCGEventFlagMaskCommand | kCGEventFlagMaskAlternate
    | kCGEventFlagMaskShift | kCGEventFlagMaskControl
)
# One recheck timer at a time, however many drains see the key held.
_drain_recheck_armed = False
# When a drain first found a done take held back by a modifier with nothing
# recording; None otherwise. MODIFIER_HOLD_MAX_SEC counts from here, not from
# when the take finished: a take that waited behind a long held recording
# must still paste when that key comes up.
_held_since = None


def _modifiers_held():
    """
    True while any modifier is physically down. ⌘V posted then would merge
    with it: right Option makes it ⌥⌘V, right Shift ⇧⌘V, and both happen
    whenever a take finishes while the next one is held, hands-free is being
    switched on, or a stop tap is in progress. The tap drains the queue as
    soon as the last modifier comes up.
    """
    return bool(
        CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState) & _HELD_MODIFIERS
    )


def _drain_recheck():
    global _drain_recheck_armed
    with state_lock:
        _drain_recheck_armed = False
    _drain_ready_jobs()


def _copy_only(text):
    try:
        copy_to_clipboard(text)
        return True
    except Exception as e:
        log(f"clipboard error: {e}")
        return False


def _drain_ready_jobs():
    """
    Deliver every finished take at the head of the queue, in spoken order. A
    take that finished early waits for the ones before it. Consecutive texts
    go in as one paste, so the target app never sees two ⌘V in a row.

    Everything from taking the jobs out of the queue to deleting their files
    happens under _clipboard_gate, which is also what recovery takes before it
    looks at the disk: it can never see a file whose job has just left _jobs.
    """
    global _drain_recheck_armed, _held_since
    with _clipboard_gate:
        ready = []
        # Settle before looking at the keys, so the look is as close to the
        # ⌘V as it can be.
        _settle_clipboard()
        held = _modifiers_held()
        rearm = False
        overdue = False
        with state_lock:
            head = next(iter(_jobs.values()), None)
            waiting = head is not None and head.done
            if held and waiting and state != "recording":
                now = time.monotonic()
                if _held_since is None:
                    _held_since = now
                overdue = now - _held_since > MODIFIER_HOLD_MAX_SEC
            else:
                _held_since = None
            if not held or overdue:
                _held_since = None
                for job in list(_jobs.values()):
                    if not job.done:
                        break
                    del _jobs[job.id]
                    ready.append(job)
            elif waiting and not _drain_recheck_armed:
                # Look again shortly: the key-up that should release this
                # paste can arrive before the system's own modifier state
                # catches up, and a stuck key has to be noticed.
                _drain_recheck_armed = rearm = True
        if rearm:
            try:
                timer = threading.Timer(DRAIN_RECHECK_SEC, _drain_recheck)
                timer.daemon = True
                timer.start()
            except Exception as e:
                with state_lock:
                    _drain_recheck_armed = False
                log(f"drain recheck start error: {e}")

        texts = [job for job in ready if job.text and not job.notice]
        if texts:
            combined = "".join(job.text for job in texts)
            if not overdue and paste_text(combined):
                log(f"pasted {len(combined)} chars from {len(texts)} take(s)")
                delivered = True
            elif _copy_only(combined):
                # No ⌘V (a stuck modifier, or the app or the permission
                # refused it), but the words are where ⌘V will find them.
                log(f"copied {len(combined)} chars from {len(texts)} take(s); not pasted")
                overlay.flash_notice("Copied · ⌘V")
                delivered = True
            else:
                # The files stay in .pending; the retry loop puts the text
                # in the clipboard later.
                overlay.flash_error("Paste failed · saved")
                delivered = False
            if delivered:
                for job in texts:
                    delete_pending_recording(job.path)

        for job in ready:
            if job.notice:
                overlay.flash_error(job.notice)
                if job.rejected:
                    quarantine_path(job.path, "rejected by the provider")
                # otherwise the file stays for the retry loop
            elif not job.text:
                log(f"no speech detected in {job.path.name}")
                delete_pending_recording(job.path)

        _refresh_overlay()


def _schedule_drain():
    """Drain off the calling thread: the event tap must never wait on a paste."""
    try:
        threading.Thread(target=_drain_ready_jobs, daemon=True).start()
    except Exception as e:
        log(f"drain start error: {e}")


_overlay_lock = threading.Lock()


def _refresh_overlay():
    """
    Show what is going on right now: the recording, else the transcription
    queue, else nothing. Read and posted under one lock, so two threads can
    never leave the pill showing the older of two snapshots.
    """
    with _overlay_lock:
        with state_lock:
            recording = state == "recording"
            label = HANDS_FREE_LABEL if current_hands_free else MODE_LABELS.get(current_mode)
            queued = len(_jobs)
            head = next(iter(_jobs.values()), None)
        if recording:
            overlay.show("recording", label=label, queued=queued)
        elif head is not None:
            overlay.show("loading", label=MODE_LABELS.get(head.mode), queued=queued)
        else:
            overlay.hide()


RECOVERY_SEPARATOR = "\n\n---\n\n"
# Text recovery already paid for but could not hand over because a take was
# live, by file name. The next pass delivers it without a second request.
_recovered_text = {}


def recover_pending_recordings():
    """
    Startup recovery. Transcribes every pending WAV, joins them with a separator,
    and writes the combined text to the clipboard once at the end. Per-file
    overwrite would lose all but the last transcript. Never auto-pastes — focus
    at login is unrelated to the dictation context.

    Runs synchronously BEFORE the event tap activates so a fresh dictation
    can never race the recovery clipboard write.
    """
    pending_dirs = []
    for pending_dir in (PENDING_DIR, FALLBACK_PENDING_DIR):
        if pending_dir not in pending_dirs:
            pending_dirs.append(pending_dir)

    existing_dirs = [
        pending_dir for pending_dir in pending_dirs if pending_dir.is_dir()
    ]
    if not existing_dirs:
        return

    # Clean up partial writes from a previous crash mid-save
    for pending_dir in existing_dirs:
        for tmp in pending_dir.glob("*.tmp"):
            try:
                tmp.unlink()
                log(f"removed partial write: {tmp.name}")
            except OSError as e:
                log(f"remove partial write error {tmp.name}: {e}")

    # A live take's WAV is its worker's. A take can end in the gap between
    # this pass deciding to run and the glob, and both transcribing the same
    # file would spend the quota twice and paste the text twice. So: glob
    # first, then drop whatever a job owns. The drain holds _clipboard_gate
    # from taking a job out of _jobs until its file is gone, so under the gate
    # a file is never between owners; and a take still being saved has no
    # path yet, so its file could be in the glob unclaimed: wait for it.
    with _clipboard_gate:
        found = [
            path
            for pending_dir in existing_dirs
            for path in pending_dir.glob("*.wav")
        ]
        with state_lock:
            saving = any(job.path is None for job in _jobs.values())
            live = {job.path for job in _jobs.values()}
    if saving:
        return
    paths = sorted(path for path in found if path not in live)
    if not paths:
        return

    paths = purge_expired_pending(paths)
    if not paths:
        return

    # Only files whose backoff has elapsed. The loop wakes every few seconds;
    # without this it would hammer a provider that just refused, and fill the
    # log with a "found N pending" line each time.
    paths = _due_pending(paths)
    if not paths:
        return

    cutoff = time.time() - PENDING_AGE_WARN_DAYS * 86400
    stale = sum(1 for p in paths if p.stat().st_mtime < cutoff)
    if stale:
        log(
            f"WARNING: {stale} pending recording(s) older than "
            f"{PENDING_AGE_WARN_DAYS}d — transcription API may be unreachable"
        )

    log(f"found {len(paths)} pending recording(s)")
    recovered = []  # (path, text); text may be empty for hallucination/silence
    skipped = 0
    for index, path in enumerate(paths):
        if not is_valid_wav(path):
            quarantine_path(path, "corrupt WAV header")
            continue
        mode = parse_pending_mode(path)
        if path.name in _recovered_text:
            recovered.append((path, _recovered_text[path.name]))
            continue
        try:
            if mode == MODE_TRANSLATE:
                text = transcriber.transcribe_and_translate_sync(path)
            else:
                text = transcriber.transcribe_dictation_sync(path, language=DICTATE_LANGUAGE)
        except ProviderUnavailableError as e:
            # The network is out, not this file's fault, so it must NOT count
            # toward quarantine. Put the rest of the queue on the backoff and
            # stop the pass — hammering a dead link helps nobody.
            for remaining in paths[index:]:
                _schedule_pending_retry(remaining, e)
            log(f"recovery paused: provider unreachable ({e}); "
                f"{len(paths) - index} recording(s) pending")
            break
        except RequestRejectedError as e:
            # The provider will never accept this payload — too long for the
            # backend, or malformed. Leaving it pending means retrying it every
            # 5 minutes forever, and each attempt spends a key's quota slot
            # that live dictation needs. Set it aside instead of losing it.
            quarantine_path(path, f"rejected by the provider: {e}")
            skipped += 1
            continue
        except Exception as e:
            # A file-specific problem (e.g. the provider persistently returns
            # an empty transcript for this audio). Skip it so one poison file
            # can't block recovery of every other recording. After
            # PENDING_MAX_FAILURES consecutive failures, stop paying for it:
            # set it aside so it no longer spends a request every poll.
            if _note_pending_failure(path):
                quarantine_path(
                    path, f"{PENDING_MAX_FAILURES} consecutive failures: {e}"
                )
                _clear_pending_failures(path.name)
            else:
                wait = _schedule_pending_retry(path, e)
                log(f"recovery retrying {path.name} in {wait}s: {e}")
            skipped += 1
            continue
        _clear_pending_failures(path.name)
        recovered.append((path, text))

    non_empty = [(p, t) for p, t in recovered if t]

    if skipped:
        log(
            f"recovery skipped {skipped} recording(s) with file-specific "
            "errors; each is either scheduled for a retry or quarantined"
        )

    if non_empty:
        combined = RECOVERY_SEPARATOR.join(t for _, t in non_empty)
        try:
            # The gate covers the clipboard write only. The drain holds it
            # across its own paste, so this can never land in the middle of
            # one — and it never blocks that paste behind the network.
            with _clipboard_gate:
                with state_lock:
                    live = state != "idle" or bool(_jobs)
                if not live:
                    _settle_clipboard()
                    copy_to_clipboard(combined)
            if live:
                # A take started while this pass was transcribing. Its paste
                # would replace the recovered text a moment after it landed,
                # and the files would be gone: keep both, deliver next pass.
                for path, text in recovered:
                    _recovered_text[path.name] = text
                log(f"recovery deferred: a take is live; {len(recovered)} kept for the next pass")
                return
            log(f"recovered {len(non_empty)} dictation(s) ({len(combined)} chars total) to clipboard")
        except Exception as e:
            log(f"recovery clipboard error: {e}")
            return  # leave all files in pending so a future startup can retry

        # The clipboard just changed under the user, who may be mid-task in
        # another window. Saying so is the difference between a recovered
        # dictation and a confusing paste later on. Kept out of the block
        # above: the text is already safely on the clipboard, so a pill that
        # fails to draw must not strand the files it came from.
        try:
            overlay.flash_notice(
                "Copied · ⌘V" if len(non_empty) == 1
                else f"{len(non_empty)} copied · ⌘V"
            )
        except Exception as e:
            log(f"recovery notice error: {e}")

    # Delete files only after a successful clipboard write (or on filtered-empty results)
    for path, _ in recovered:
        _recovered_text.pop(path.name, None)
        delete_pending_recording(path)


def _all_pending_names(min_age_sec=0.0):
    """
    Every recording currently queued on disk, across both directories.

    min_age_sec keeps a cancel from touching one that has only just been
    written: the same grace the in-flight job gets, so the rule the user sees
    is one rule — a tap acts on what has been stuck for a while, never on
    something that is still perfectly on track.
    """
    cutoff = time.time() - min_age_sec
    names = []
    for pending_dir in (PENDING_DIR, FALLBACK_PENDING_DIR):
        if not pending_dir.is_dir():
            continue
        for path in pending_dir.glob("*.wav"):
            try:
                if min_age_sec and path.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            names.append(path.name)
    return names


def cancel_everything(min_age=0.0):
    """
    Stop whatever is in flight and hold the queue, without losing audio.

    Voids every queued job at least min_age old, so a late worker drops its
    result instead of pasting into whatever the user is doing next. Younger
    takes carry on and paste: the cancel tap passes CANCEL_GRACE_SEC, and a
    take voided before its file is old enough to be held would be picked up
    by the retry loop while its worker still runs. A recording still being
    captured is finalized to disk rather than dropped — the user asked to stop
    waiting, not to discard what they said.

    Returns (what_was_stopped, recordings_held, newly_held). The last one is
    what decides whether there is anything to tell the user: a tap that held
    nothing new changed nothing, and this key is also how capitals get typed.
    """
    stopped = None
    finish_keycode = None
    now = time.monotonic()
    with state_lock:
        newest = current_job_id
        if state == "recording":
            stopped = "recording"
            finish_keycode = current_hotkey
        # A take that is already done is only waiting for the stuck one
        # ahead of it: it stays, and pastes once that one is out of the way.
        stale = [
            job for job in _jobs.values()
            if not job.done and now - job.started >= min_age
        ]
        for job in stale:
            del _jobs[job.id]
        if stale and stopped is None:
            stopped = "processing"
        # Held by name: a file written a moment after its job started can be
        # younger than the grace while the job is past it, and an unheld file
        # would go straight back to the retry loop.
        voided = [job.path.name for job in stale if job.path is not None]

    if finish_keycode is not None:
        # Outside the lock, like the watchdog's hard-limit branch: on_key_up
        # owns stop → atomic save → worker. Run it so the audio reaches disk,
        # then void its job — the recording belongs to the user, the
        # transcription is what they asked to call off.
        on_key_up(finish_keycode)
        with state_lock:
            for job_id in [i for i in _jobs if i > newest]:
                del _jobs[job_id]

    if stopped is not None:
        _refresh_overlay()
    if stale:
        # Done takes that waited behind a voided one can go now.
        _schedule_drain()
    held = sorted(set(_all_pending_names(min_age_sec=CANCEL_GRACE_SEC)) | set(voided))
    with _pending_failures_lock:
        newly_held = [n for n in held if n not in _pending_cancelled]
        _pending_cancelled.update(held)
    return stopped, len(held), len(newly_held)


def resume_everything():
    """
    Undo a cancel: clear the hold and make every queued recording due now.
    Returns how many were released.
    """
    with _pending_failures_lock:
        _pending_cancelled.clear()
        for name in _all_pending_names():
            _pending_next_attempt[name] = 0.0
        return len(_all_pending_names())


def _begin_recording(keycode, mode, hands_free=False):
    """
    Atomic idle→recording transition that also latches the hotkey and mode
    in a single critical section. Splitting the state flip and the
    hotkey/mode write into two locks would leave a window in which a key-up
    can see the new "recording" state but the wrong (stale) hotkey/mode.
    Returns True on success.
    """
    global state, state_ts, current_hotkey, current_mode, current_hands_free
    with state_lock:
        # Only the recording slot matters: earlier takes may still be
        # transcribing, and they paste in order behind this one.
        if state != "idle":
            return False
        state = "recording"
        state_ts = time.monotonic()
        current_hotkey = keycode
        current_mode = mode
        current_hands_free = hands_free
        return True


def _abandon_recording_cycle():
    """
    Drop a cycle that failed to launch (recorder.start error). Resets state to
    idle AND clears the hotkey/mode in a single critical section — separate
    set_state + clear would leave a window in which another keypress could
    start a new cycle that the second mutation then clobbers.
    """
    global state, state_ts, current_hotkey, current_mode
    with state_lock:
        if state == "recording":
            state = "idle"
            state_ts = time.monotonic()
        current_hotkey = None
        current_mode = None
    # A take that finished while this key was held waited for it.
    _schedule_drain()


def on_key_down(keycode, mode, hands_free=False):
    if not _begin_recording(keycode, mode, hands_free):
        return
    if _microphone_access_is_blocked():
        log(
            "recording blocked: Microphone permission is denied or restricted"
        )
        _abandon_recording_cycle()
        overlay.flash_error("Mic blocked")
        return
    try:
        recorder.start()
        log(f"recording started: mode={mode}{'; hands-free' if hands_free else ''}")
        _refresh_overlay()
    except Exception as e:
        log(f"recorder.start error: {e}")
        _abandon_recording_cycle()
        overlay.flash_error("Mic unavailable")


_last_cancel_tap = 0.0
_cancel_tap_lock = threading.Lock()


def on_cancel_tap():
    """
    Caps Lock. One tap stops everything stuck and holds it; a second tap
    inside CANCEL_DOUBLE_TAP_SEC releases it and tries again straight away, so
    the same key both calls off a stuck transcription and restarts it.
    """
    global _last_cancel_tap
    now = time.monotonic()

    with state_lock:
        recording = state == "recording"
        oldest = min((job.started for job in _jobs.values()), default=None)

    # This key is also how people type capitals, so every path that has
    # nothing to do returns in silence — no pill, no log line. Only a tap that
    # actually changes something is allowed to say so.
    if recording:
        return
    if oldest is not None and now - oldest < CANCEL_GRACE_SEC:
        return

    with _pending_failures_lock:
        anything_held = bool(_pending_cancelled)

    # A second tap only means "retry now" when the first one held something;
    # otherwise two stray capitals in a row would release the queue.
    with _cancel_tap_lock:
        double = anything_held and (now - _last_cancel_tap) <= CANCEL_DOUBLE_TAP_SEC
        _last_cancel_tap = 0.0 if double else now

    if double:
        released = resume_everything()
        log(f"resume requested: {released} recording(s) queued now")
        overlay.flash_notice(f"Retrying {released}", sound=False)
        return

    stopped, held, newly_held = cancel_everything(min_age=CANCEL_GRACE_SEC)
    if stopped is None and newly_held == 0:
        return
    log(f"cancel requested: stopped={stopped or 'nothing'}; {held} held")
    overlay.flash_notice(f"Cancelled · {held} kept" if held else "Cancelled", sound=False)


def on_key_up(keycode):
    # Ignore key-up for a hotkey that did NOT start the current cycle.
    # Without this, tapping the other hotkey mid-record would end the cycle.
    with state_lock:
        if current_hotkey != keycode:
            return

    with _save_gate:
        # Stop the recorder BEFORE the slot is freed. Once it is free a
        # key-down can start the next take, and recorder.start() resets the
        # buffer this stop is about to return: the watchdog stopping a long
        # take would get the new, empty one. The gate makes a second stopper
        # (watchdog vs. real key-up) wait, then find nothing to stop.
        with state_lock:
            if state != "recording" or current_hotkey != keycode:
                return
        try:
            audio, stop_error = recorder.stop(), None
        except Exception as e:
            audio, stop_error = None, e

        # Free the recording slot and queue the job in one step: the next
        # take can start right away, and this one already holds its place in
        # the paste order.
        job_id, mode = begin_processing()
        if job_id is None:
            return

        if stop_error is not None:
            log(f"recorder.stop error: {stop_error}")
            if _drop_job(job_id):
                overlay.flash_error("Recording failed")
            _schedule_drain()
            return
        log(
            f"recording stopped: {len(audio)} samples; mode={mode}; "
            f"loudest frame {loudest_frame_rms(audio):.4f}"
        )

        if len(audio) < MIN_AUDIO_SAMPLES:
            log(
                f"recording ignored: {len(audio)} samples is below "
                f"{MIN_AUDIO_SAMPLES}-sample minimum"
            )
            _drop_job(job_id)
            _schedule_drain()
            return

        try:
            wav_path = save_recording_with_fallback(audio, mode)
        except Exception as e:
            log(f"save pending recording error: {e}")
            if _drop_job(job_id):
                overlay.flash_error("Not saved")
            _schedule_drain()
            return

        with state_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.path = wav_path
    if job is None:
        # Voided while saving (cancel, shutdown). The file is on disk, so the
        # retry loop owns it now.
        return

    try:
        threading.Thread(
            target=process_pending_recording,
            args=(wav_path, job_id, mode),
            daemon=True,
        ).start()
    except Exception as e:
        log(f"processing worker start error {wav_path.name}: {e}")
        if _drop_job(job_id):
            overlay.flash_error("Will retry")
    # Takes that finished while this key was held were waiting for it.
    _schedule_drain()


def _other_input_since(started):
    """
    Whether any ordinary key or mouse button went down after `started`
    (a time.monotonic() value). Reads the system's per-type idle counters, so
    the event tap never has to see the keystrokes themselves.
    """
    elapsed = time.monotonic() - started
    return any(
        CGEventSourceSecondsSinceLastEventType(
            kCGEventSourceStateHIDSystemState, kind
        ) < elapsed
        for kind in (kCGEventKeyDown, kCGEventLeftMouseDown, kCGEventRightMouseDown)
    )


# Touched only from the event-tap thread.
_hands_free_started_at = None
_hands_free_stop_pressed_at = None


def _hands_free_recording():
    with state_lock:
        return (
            state == "recording"
            and current_hands_free
            and current_hotkey in DICTATION_KEYS
        )


def _latch_hands_free(keycode):
    """Turn a held dictation hands-free when the other dictation key goes down."""
    global current_hands_free
    with state_lock:
        if (
            state == "recording"
            and current_hotkey in DICTATION_KEYS
            and current_hotkey != keycode
            and not current_hands_free
        ):
            current_hands_free = True
            return True
        return False


def _finish_hands_free():
    """End the cycle through its owner, whichever key the stop tap was on."""
    with state_lock:
        owner = current_hotkey
    if owner in DICTATION_KEYS:
        on_key_up(owner)


def on_dictate_key(keycode, pressed, other_held):
    """
    Right Command or right Option: hold either to dictate. Pressing the other
    as well, in either order, makes the recording hands-free, and a tap of
    either key then stops it. The stop tap counts on release, and only if
    nothing was typed or clicked while it was down, so both keys still work as
    shortcut modifiers during a hands-free recording.

    The other key's state comes from this event's own flags rather than from
    remembered key-downs: a lost release would otherwise turn every later
    dictation into a hands-free one.
    """
    global _hands_free_started_at, _hands_free_stop_pressed_at
    now = time.monotonic()

    if _hands_free_recording():
        if pressed:
            # Pressing both keys to stop starts the tap at the first of them.
            if _hands_free_stop_pressed_at is None:
                _hands_free_stop_pressed_at = now
            return
        stop_pressed_at = _hands_free_stop_pressed_at
        started_at = _hands_free_started_at
        _hands_free_stop_pressed_at = None
        _hands_free_started_at = None
        if stop_pressed_at is not None:
            if not _other_input_since(stop_pressed_at):
                _finish_hands_free()
        elif started_at is not None and _other_input_since(started_at):
            # A key went down while the combination was held, so it was an
            # Option + Command shortcut, not a request to dictate.
            _finish_hands_free()
        return

    if pressed and _latch_hands_free(keycode):
        _hands_free_started_at = now
        _hands_free_stop_pressed_at = None
        log("recording switched to hands-free")
        _refresh_overlay()
        return

    if not pressed:
        on_key_up(keycode)
        return

    _hands_free_stop_pressed_at = None
    _hands_free_started_at = now if other_held else None
    on_key_down(keycode, MODE_DICTATE, hands_free=other_held)


def tap_callback(proxy, event_type, event, refcon):
    # Wrap entire body — Python exceptions from here propagate into the
    # CGEventTap C callback and can take down the run loop
    try:
        if event_type == kCGEventTapDisabledByTimeout or event_type == kCGEventTapDisabledByUserInput:
            CGEventTapEnable(tap_ref, True)
            log(f"event tap re-enabled (reason: {event_type})")
            return event

        keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
        # Device-dependent flag distinguishes left vs right modifier —
        # the shared mask (e.g. kCGEventFlagMaskCommand) catches both
        if keycode in DICTATION_KEYS:
            own, other = DICTATION_KEYS[keycode]
            flags = CGEventGetFlags(event)
            on_dictate_key(keycode, bool(flags & own), bool(flags & other))
        elif keycode == TRANSLATE_KEYCODE:
            pressed = bool(CGEventGetFlags(event) & TRANSLATE_FLAG)
            if pressed:
                on_key_down(keycode, MODE_TRANSLATE)
            else:
                on_key_up(keycode)
        elif keycode == CANCEL_KEYCODE:
            # Caps Lock is a TOGGLE, not a held modifier: one press emits one
            # event, carrying the new state, so its flag is set on the press
            # that turns capitals on and clear on the press that turns them
            # off. Gating on the flag being set would therefore act on every
            # SECOND press and make the double tap unreachable. One event is
            # already one press, so fire on all of them.
            on_cancel_tap()

        # Any modifier, ours or not, can hold back a paste (_modifiers_held).
        # The event that lifts the last one releases it.
        if not CGEventGetFlags(event) & _HELD_MODIFIERS:
            with state_lock:
                head = next(iter(_jobs.values()), None)
                waiting = head is not None and head.done
            if waiting:
                _schedule_drain()
    except Exception as e:
        log(f"tap_callback error: {e}")

    return event


def run_event_tap():
    global tap_ref, source_ref

    log(
        f"input monitoring trusted: {bool(CGPreflightListenEventAccess())}; "
        f"post events trusted: {bool(CGPreflightPostEventAccess())}"
    )
    tap_ref = CGEventTapCreate(
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionListenOnly,
        CGEventMaskBit(kCGEventFlagsChanged),
        tap_callback,
        None,
    )
    if tap_ref is None:
        log("Failed to create event tap")
        log("Grant Input Monitoring: System Settings > Privacy & Security > Input Monitoring")
        os._exit(1)

    source_ref = CFMachPortCreateRunLoopSource(None, tap_ref, 0)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), source_ref, kCFRunLoopDefaultMode)
    CGEventTapEnable(tap_ref, True)
    log("event tap active")
    CFRunLoopRun()


PLIST_HINT = (
    "~/Library/LaunchAgents/com.openspeaksy.plist (EnvironmentVariables) "
    "and reload."
)


def configuration_error():
    """
    Return a message describing the first fatal misconfiguration, or None when
    the config is usable. Pure so the rules can be tested without booting the
    event tap: main() only logs whatever this returns and exits.
    """
    if not MISTRAL_API_KEY:
        return (
            "no Mistral API key configured; both transcription and the "
            f"translate hotkeys require it. Set MISTRAL_API_KEY in {PLIST_HINT}"
        )
    return None


def main():
    # Private-by-default for logs, pending recordings, and any future files.
    os.umask(0o077)
    _install_file_handler()
    if not _acquire_instance_lock():
        log("another OpenSpeaksy instance is already running; exiting")
        return
    _install_shutdown_handling()

    fatal = configuration_error()
    if fatal:
        # Exit CLEANLY, despite being a fatal error. Only editing the plist can
        # fix this, and that needs a reload anyway, so a nonzero exit under
        # KeepAlive={SuccessfulExit: false} would just relaunch every
        # ThrottleInterval forever. A missing permission still exits nonzero,
        # because there a relaunch is exactly what picks the grant up.
        log(f"FATAL: {fatal}")
        return

    translator = f"Mistral {MISTRAL_TRANSLATION_MODEL}"
    stt = f"Mistral {MISTRAL_MODEL}" + (
        f", fallback {MISTRAL_FALLBACK_MODEL}"
        if MISTRAL_FALLBACK_MODEL and MISTRAL_FALLBACK_MODEL != MISTRAL_MODEL else ""
    )
    log(
        f"OpenSpeaksy starting — primary STT: {stt}; "
        f"dictate language: {DICTATE_LANGUAGE or 'auto'}; "
        f"translation backend: {translator}"
    )
    microphone_status = microphone_authorization_status()
    if microphone_status in {
        MICROPHONE_AUTH_RESTRICTED,
        MICROPHONE_AUTH_DENIED,
    }:
        log(
            "ERROR: Microphone permission is denied or restricted; enable it "
            "in System Settings > Privacy & Security > Microphone"
        )
    elif microphone_status == MICROPHONE_AUTH_NOT_DETERMINED:
        log("microphone permission has not been requested yet")
    elif microphone_status == MICROPHONE_AUTH_AUTHORIZED:
        log("microphone permission trusted: True")

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    # Recovery runs synchronously BEFORE the tap activates so a fresh dictation
    # cannot race the recovery clipboard write.
    recover_pending_recordings()

    threading.Thread(target=watchdog_loop, daemon=True).start()
    threading.Thread(target=pending_retry_loop, daemon=True).start()
    threading.Thread(target=run_event_tap, daemon=True).start()
    time.sleep(0.1)

    log(
        "OpenSpeaksy running — hold right Command or right Option (dictate), "
        "press both (hands-free), or hold right Shift (Russian→English)"
    )
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
