"""Keyboard input via evdev, mapped to the CardputerZero key conventions."""

import errno
import select

# Linux input event codes (linux/input-event-codes.h) so this file needs no
# evdev import just to describe the mapping.
KEY_ESC, KEY_1, KEY_4, KEY_5, KEY_6, KEY_7, KEY_8 = 1, 2, 5, 6, 7, 8, 9
KEY_ENTER, KEY_KPENTER = 28, 96
KEY_F, KEY_H, KEY_R, KEY_X, KEY_Z, KEY_C = 33, 35, 19, 45, 44, 46
KEY_SLASH, KEY_SPACE = 53, 57
KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 103, 108, 105, 106
KEY_PAGEUP, KEY_PAGEDOWN, KEY_HOME, KEY_END = 104, 109, 102, 107
KEY_HELP, KEY_QUESTION, KEY_F1 = 138, 214, 59
KEY_LEFTSHIFT, KEY_RIGHTSHIFT = 42, 54
KEY_SYSRQ, KEY_PRINT = 99, 210   # fn+J sends PrintScreen (SYSRQ)

# Not in a text field anywhere in this app, so F/Z/X/C act as arrows directly
# (as the CardputerZero key guidelines recommend).
KEYMAP = {
    KEY_UP: "up", KEY_F: "up",
    KEY_DOWN: "down", KEY_X: "down",
    KEY_LEFT: "left", KEY_Z: "left",
    KEY_RIGHT: "right", KEY_C: "right",
    KEY_PAGEUP: "pageup", KEY_PAGEDOWN: "pagedown",
    KEY_HOME: "home", KEY_END: "end",
    KEY_ENTER: "enter", KEY_KPENTER: "enter",
    KEY_ESC: "esc",
    KEY_4: "k4", KEY_5: "k5", KEY_6: "k6", KEY_7: "k7", KEY_8: "k8",
    KEY_H: "help", KEY_HELP: "help", KEY_QUESTION: "help", KEY_F1: "help",
    KEY_R: "refresh",
    KEY_SYSRQ: "screenshot", KEY_PRINT: "screenshot",
}
REPEATABLE = {"up", "down", "pageup", "pagedown", "backspace"}
KEY_BACKSPACE, KEY_MINUS = 14, 12
KEYMAP[KEY_BACKSPACE] = "backspace"

# Characters allowed in a tunnel name: letters, digits, '-' and '_'.
_ROWS = {16: "qwertyuiop", 30: "asdfghjkl", 44: "zxcvbnm"}
CHARS = {2 + i: c for i, c in enumerate("1234567890")}
for _start, _row in _ROWS.items():
    CHARS.update({_start + i: c for i, c in enumerate(_row)})


def char_for(code, value, shift=False):
    """Text typed by a key press (name entry only), or None."""
    if value not in (1, 2):
        return None
    if code == KEY_MINUS:
        return "_" if shift else "-"
    c = CHARS.get(code)
    if c and shift and c.isalpha():
        return c.upper()
    return c


def translate(code, value, shift=False):
    """evdev (code, value) -> action name or None. value: 1 press, 2 repeat."""
    if value not in (1, 2):
        return None
    if code == KEY_SLASH and shift:
        return "help"
    action = KEYMAP.get(code)
    if action is None or (value == 2 and action not in REPEATABLE):
        return None
    return action


class EvdevKeys:
    def __init__(self):
        from evdev import InputDevice, ecodes, list_devices
        self._ecodes = ecodes
        self.devices = []
        for path in list_devices():
            try:
                d = InputDevice(path)
            except (OSError, PermissionError):
                continue
            keys = d.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_ENTER in keys or ecodes.KEY_ESC in keys:
                self.devices.append(d)
            else:
                d.close()
        self.shift = False

    def poll(self, timeout):
        """Wait up to `timeout` seconds; return a list of actions."""
        if not self.devices:
            select.select([], [], [], timeout)
            return []
        ready, _, _ = select.select([d.fd for d in self.devices], [], [], timeout)
        actions = []
        for d in list(self.devices):
            if d.fd not in ready:
                continue
            try:
                events = list(d.read())
            except BlockingIOError:
                continue
            except OSError as e:
                if e.errno in (errno.ENODEV, errno.EIO):   # keyboard went away
                    self.devices.remove(d)
                    try:
                        d.close()
                    except OSError:
                        pass
                continue
            if True:
                for ev in events:
                    if ev.type != self._ecodes.EV_KEY:
                        continue
                    if ev.code in (KEY_LEFTSHIFT, KEY_RIGHTSHIFT):
                        self.shift = ev.value != 0
                        continue
                    ch = char_for(ev.code, ev.value, self.shift)
                    if ch:
                        actions.append("ch:" + ch)
                    a = translate(ev.code, ev.value, self.shift)
                    if a:
                        actions.append(a)
        return actions

    def close(self):
        for d in self.devices:
            try:
                d.close()
            except OSError:
                pass
