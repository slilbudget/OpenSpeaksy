"""
Right Command and right Option are interchangeable: hold either to dictate,
press both for hands-free, tap either to stop. The rules that keep it from
firing or stopping by accident are the point of these tests: a shortcut typed
with these modifiers must behave exactly as before.
"""
import numpy as np
import pytest

import main


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Recorder:
    """Returns as many samples as the key was actually recording for."""

    def __init__(self, clock):
        self.clock = clock
        self.started_at = None
        self.stops = []

    def start(self):
        self.started_at = self.clock.now

    def stop(self):
        seconds = self.clock.now - self.started_at
        self.stops.append(seconds)
        return np.zeros(int(seconds * 16000), dtype=np.float32)


class _Overlay:
    def __init__(self):
        self.shown = []

    def show(self, mode, label=None, queued=0, **_):
        self.shown.append((mode, label))

    def hide(self, **_):
        pass

    def flash_error(self, message=None, duration=None, **_):
        self.shown.append(("error", message))


class _Thread:
    def __init__(self, *, target, args=(), daemon):
        pass

    def start(self):
        pass


@pytest.fixture
def rig(monkeypatch, tmp_path):
    clock = _Clock()
    recorder = _Recorder(clock)
    overlay = _Overlay()
    saved = []
    typed = {"at": None}

    monkeypatch.setattr(main.time, "monotonic", clock)
    monkeypatch.setattr(main, "recorder", recorder)
    monkeypatch.setattr(main, "overlay", overlay)
    monkeypatch.setattr(main, "log", lambda message: None)
    monkeypatch.setattr(main, "_microphone_access_is_blocked", lambda: False)
    monkeypatch.setattr(main.threading, "Thread", _Thread)
    monkeypatch.setattr(
        main,
        "save_recording_with_fallback",
        lambda audio, mode: saved.append(len(audio)) or tmp_path / "x.dictate.wav",
    )
    # Stands in for the system idle counters: "a key went down at typed['at']".
    monkeypatch.setattr(
        main,
        "_other_input_since",
        lambda started: typed["at"] is not None and typed["at"] > started,
    )
    for name, value in {
        "state": "idle",
        "state_ts": 0.0,
        "current_hotkey": None,
        "current_mode": None,
        "current_hands_free": False,
        "current_job_id": 0,
        "_hands_free_started_at": None,
        "_hands_free_stop_pressed_at": None,
    }.items():
        monkeypatch.setattr(main, name, value)

    keys = {"cmd": main.HOTKEY_KEYCODE, "opt": main.OPTION_KEYCODE}

    class Rig:
        held = set()

        def press(self, key="cmd", after=0.0):
            clock.now += after
            other = (self.held - {key}) != set()
            self.held.add(key)
            main.on_dictate_key(keys[key], True, other)

        def release(self, key="cmd", after=0.0):
            clock.now += after
            self.held.discard(key)
            main.on_dictate_key(keys[key], False, self.held != set())

        def tap(self, key="cmd", after=0.0, held=0.1):
            self.press(key, after)
            self.release(key, held)

        def combo(self, first="opt", after=0.0, held=0.15):
            """Both keys down, then both up, the second key released first."""
            second = "cmd" if first == "opt" else "opt"
            self.press(first, after)
            self.press(second, 0.03)
            self.release(second, held)
            self.release(first, 0.02)

        def type_key(self, after=0.0):
            clock.now += after
            typed["at"] = clock.now

    rig = Rig()
    rig.held = set()
    rig.saved = saved
    rig.overlay = overlay
    return rig


@pytest.mark.parametrize("key", ["cmd", "opt"])
def test_holding_either_key_dictates(rig, key):
    rig.press(key)
    rig.release(key, after=3.0)

    assert rig.saved == [48000]
    assert main.state == "idle" and len(main._jobs) == 1
    assert ("recording", main.HANDS_FREE_LABEL) not in rig.overlay.shown


@pytest.mark.parametrize("first", ["cmd", "opt"])
def test_both_keys_in_either_order_go_hands_free(rig, first):
    rig.combo(first)

    assert main.state == "recording"
    assert main.current_hands_free is True
    assert rig.overlay.shown[-1] == ("recording", main.HANDS_FREE_LABEL)
    assert rig.saved == []


@pytest.mark.parametrize("held, added", [("cmd", "opt"), ("opt", "cmd")])
def test_adding_the_other_key_mid_dictation_keeps_the_audio(rig, held, added):
    rig.press(held)
    rig.press(added, after=4.0)
    rig.release(held, after=0.1)
    rig.release(added, after=0.05)

    assert main.state == "recording"
    assert rig.overlay.shown[-1] == ("recording", main.HANDS_FREE_LABEL)

    rig.tap(held, after=10.0)
    assert main.state == "idle" and len(main._jobs) == 1
    assert rig.saved[0] >= 14 * 16000


@pytest.mark.parametrize("first, stop", [
    ("opt", "cmd"), ("opt", "opt"), ("cmd", "cmd"), ("cmd", "opt"),
])
def test_a_tap_of_either_key_stops_it(rig, first, stop):
    rig.combo(first)
    rig.tap(stop, after=30.0)

    assert main.state == "idle" and len(main._jobs) == 1
    assert len(rig.saved) == 1
    assert rig.saved[0] >= 30 * 16000


def test_both_keys_again_also_stop_it(rig):
    rig.combo("opt")
    rig.combo("cmd", after=10.0)

    assert main.state == "idle" and len(main._jobs) == 1
    assert len(rig.saved) == 1


def test_an_option_command_shortcut_does_not_start_dictation(rig):
    """Right Option + Command + a letter is a shortcut, not a request."""
    rig.press("opt")
    rig.press("cmd", after=0.03)
    rig.type_key(after=0.05)
    rig.release("cmd", after=0.05)
    rig.release("opt", after=0.02)

    assert main.state == "idle"
    assert rig.saved == []


@pytest.mark.parametrize("key", ["cmd", "opt"])
def test_either_key_used_as_a_shortcut_does_not_stop_it(rig, key):
    rig.combo()

    rig.press(key, after=5.0)
    rig.type_key(after=0.05)
    rig.release(key, after=0.05)

    assert main.state == "recording"
    assert rig.saved == []

    rig.tap(key, after=5.0)
    assert main.state == "idle" and len(main._jobs) == 1
    assert len(rig.saved) == 1


def test_a_plain_hold_after_hands_free_is_not_hands_free(rig):
    rig.combo()
    rig.tap(after=5.0)

    # No waiting for the first take: the next one starts while it transcribes.
    rig.press("opt", after=1.0)
    assert main.state == "recording"
    assert main.current_hands_free is False
    rig.release("opt", after=2.0)
    assert main.state == "idle" and len(main._jobs) == 2


@pytest.mark.parametrize("keycode, flags, expected", [
    (0x36, 0x10, ("down", 0x36, "dictate", False)),
    (0x3D, 0x40, ("down", 0x3D, "dictate", False)),
    (0x36, 0x10 | 0x40, ("down", 0x36, "dictate", True)),
    (0x3D, 0x40 | 0x10, ("down", 0x3D, "dictate", True)),
    (0x3C, 0x04, ("down", 0x3C, "translate", False)),
])
def test_the_keyboard_map(monkeypatch, keycode, flags, expected):
    """Right ⌘ and right ⌥ both dictate; right ⇧ is English."""
    calls = []
    monkeypatch.setattr(main, "CGEventGetIntegerValueField", lambda e, f: keycode)
    monkeypatch.setattr(main, "CGEventGetFlags", lambda e: flags)
    monkeypatch.setattr(main, "_hands_free_recording", lambda: False)
    monkeypatch.setattr(main, "_latch_hands_free", lambda k: False)
    monkeypatch.setattr(
        main,
        "on_key_down",
        lambda k, mode, hands_free=False: calls.append(("down", k, mode, hands_free)),
    )

    main.tap_callback(None, main.kCGEventFlagsChanged, object(), None)

    assert calls == [expected]


def test_a_lost_release_of_the_same_key_does_not_go_hands_free(rig):
    """macOS can drop a key-up; the next press of that key is not a combo."""
    rig.press("cmd")
    rig.held.clear()
    rig.press("cmd", after=2.0)

    assert main.current_hands_free is False
