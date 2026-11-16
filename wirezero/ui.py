"""WireZero screens, drawing and key handling."""

import math
import os
import queue
import threading
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from .display import WIDTH, HEIGHT
from .nm import NAME_RE, NMError, check_conf_text, find_conf_files, sanitize_ifname

# ---------------------------------------------------------------- look & feel
BG = (13, 17, 23)
PANEL = (22, 27, 34)
SEL = (31, 42, 58)
LINE = (48, 54, 61)
TEXT = (230, 237, 243)
DIM = (125, 133, 144)
FAINT = (72, 79, 88)
ACCENT = (88, 166, 255)
GREEN = (63, 185, 80)
AMBER = (210, 153, 34)
RED = (248, 81, 73)

HEADER_H = 20
BAR_TOP = 135
ROW_H = 28
ICON_X = (49, 105, 161, 217, 273)   # centred over keys 4..8
ICON_Y = 152
ICON = 22

FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF",
             "/usr/share/fonts/truetype/jetbrains-mono",
             "/usr/share/fonts/truetype/jetbrains-mono-nl")


def _font(names, size):
    for d in FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


class Fonts:
    def __init__(self):
        sans, bold = ["DejaVuSans.ttf"], ["DejaVuSans-Bold.ttf"]
        mono = ["JetBrainsMono-Regular.ttf", "DejaVuSansMono.ttf"]
        self.title = _font(bold, 13)
        self.name = _font(bold, 14)
        self.body = _font(sans, 12)
        self.small = _font(sans, 12)
        self.mono = _font(mono, 12)
        self.big = _font(bold, 16)


def _w(draw, text, font):
    b = draw.textbbox((0, 0), text, font=font)
    return b[2] - b[0]


def ellipsize(draw, text, font, width):
    if _w(draw, text, font) <= width:
        return text
    while text and _w(draw, text + "...", font) > width:
        text = text[:-1]
    return text + "..."


def wrap(draw, text, font, width):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if _w(draw, trial, font) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        while _w(draw, word, font) > width:          # very long tokens (paths)
            cut = len(word)
            while cut > 1 and _w(draw, word[:cut], font) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        cur = word
    if cur:
        lines.append(cur)
    return lines


# ---------------------------------------------------------------- icons
def draw_icon(d, kind, cx, cy, color):
    r = ICON // 2
    x0, y0, x1, y1 = cx - r, cy - r, cx + r, cy + r
    if kind == "power":
        d.arc((x0 + 2, y0 + 3, x1 - 2, y1 - 1), 300, 240, fill=color, width=2)
        d.line((cx, y0, cx, cy + 1), fill=color, width=2)
    elif kind == "import":
        d.line((cx, y0 + 1, cx, cy + 3), fill=color, width=2)
        d.polygon([(cx - 5, cy - 1), (cx + 5, cy - 1), (cx, cy + 5)], fill=color)
        d.line((x0 + 2, cy + 3, x0 + 2, y1 - 2, x1 - 2, y1 - 2, x1 - 2, cy + 3),
               fill=color, width=2)
    elif kind == "info":
        d.ellipse((x0 + 1, y0 + 1, x1 - 1, y1 - 1), outline=color, width=2)
        d.rectangle((cx - 1, cy - 1, cx + 1, cy + 6), fill=color)
        d.rectangle((cx - 1, cy - 6, cx + 1, cy - 4), fill=color)
    elif kind == "auto":
        d.arc((x0 + 2, y0 + 2, x1 - 2, y1 - 2), 30, 330, fill=color, width=2)
        ax = cx + (r - 2) * math.cos(math.radians(330))
        ay = cy + (r - 2) * math.sin(math.radians(330))
        d.polygon([(ax - 4, ay - 2), (ax + 4, ay - 3), (ax + 1, ay + 5)], fill=color)
        d.text((cx - 3, cy - 7), "A", fill=color)
    elif kind == "trash":
        d.line((x0 + 3, y0 + 5, x1 - 3, y0 + 5), fill=color, width=2)
        d.rectangle((cx - 3, y0 + 2, cx + 3, y0 + 4), outline=color)
        d.polygon([(x0 + 5, y0 + 7), (x1 - 5, y0 + 7), (x1 - 6, y1 - 1), (x0 + 6, y1 - 1)],
                  outline=color)
        d.line((x0 + 5, y0 + 7, x1 - 5, y0 + 7, x1 - 6, y1 - 1, x0 + 6, y1 - 1, x0 + 5, y0 + 7),
               fill=color, width=2)
        for dx in (-3, 0, 3):
            d.line((cx + dx, y0 + 10, cx + dx, y1 - 4), fill=color)
    elif kind == "refresh":
        d.arc((x0 + 2, y0 + 2, x1 - 2, y1 - 2), 320, 260, fill=color, width=2)
        ax = cx + (r - 2) * math.cos(math.radians(260))
        ay = cy + (r - 2) * math.sin(math.radians(260))
        d.polygon([(ax - 1, ay - 5), (ax + 6, ay), (ax - 1, ay + 4)], fill=color)
    elif kind == "back":
        d.line((x0 + 3, cy, x1 - 2, cy), fill=color, width=2)
        d.line((x0 + 3, cy, x0 + 10, cy - 7), fill=color, width=2)
        d.line((x0 + 3, cy, x0 + 10, cy + 7), fill=color, width=2)
    elif kind == "camera":
        d.rounded_rectangle((x0 + 1, y0 + 5, x1 - 1, y1 - 2), 3, outline=color, width=2)
        d.rectangle((cx - 4, y0 + 2, cx + 4, y0 + 5), fill=color)
        d.ellipse((cx - 5, cy - 3, cx + 5, cy + 7), outline=color, width=2)
    elif kind == "help":
        d.ellipse((x0 + 1, y0 + 1, x1 - 1, y1 - 1), outline=color, width=2)
        d.text((cx - 3, cy - 7), "?", fill=color)


# ---------------------------------------------------------------- the app
class App:
    REFRESH_EVERY = 2.0

    def __init__(self, backend, display, keys, conf_finder=find_conf_files,
                 scanner_factory=None, camera_check=None):
        if scanner_factory is None or camera_check is None:
            from . import camera
            scanner_factory = scanner_factory or camera.Scanner
            camera_check = camera_check or camera.camera_present
        self.scanner_factory = scanner_factory
        self.has_camera = camera_check()
        self.scanner = None
        self.pending_conf = None      # decoded QR text awaiting a name
        self.name_text = ""
        self.nm = backend
        self.display = display
        self.keys = keys
        self.find_confs = conf_finder
        self.f = Fonts()
        self.screen = "list"          # list | details | import | help | fatal
        self.tunnels = []
        self.sel = 0
        self.top = 0
        self.detail_rows = []
        self.detail_scroll = 0
        self.confs = []
        self.conf_sel = 0
        self.conf_top = 0
        self.confirm = None           # (question, detail, callback)
        self.toast = None             # (text, color, until)
        self.busy = None              # text while a background job runs
        self.fatal = None
        self.auth_problem = False
        self.net = "unknown"
        self.help_from = "list"
        self.error_lines = []
        self.error_from = "list"
        self.running = True
        self.dirty = True
        self._last_refresh = 0.0
        self._jobs = queue.Queue()
        self._spin = 0

    # ---------------------------------------------------------- data
    def refresh(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_refresh < self.REFRESH_EVERY:
            return
        self._last_refresh = now
        try:
            tunnels = self.nm.list_tunnels()
            net = self.nm.net_state()
        except NMError as e:
            self.show_toast(str(e), RED)
            return
        cur = self.current()
        self.tunnels = tunnels
        if cur:
            for i, t in enumerate(tunnels):
                if t.uuid == cur.uuid:
                    self.sel = i
                    break
        self.sel = max(0, min(self.sel, len(tunnels) - 1))
        self.net = net
        if self.screen == "details":
            self.load_details()
        self.dirty = True

    def current(self):
        if 0 <= self.sel < len(self.tunnels):
            return self.tunnels[self.sel]
        return None

    def load_details(self):
        t = self.current()
        if not t:
            self.screen = "list"
            return
        try:
            self.detail_rows = self.nm.details(t)
        except NMError as e:
            self.detail_rows = [("Error", str(e))]

    # ---------------------------------------------------------- jobs
    def run_job(self, label, fn, done_msg=None):
        if self.busy:
            return
        self.busy = label
        self.dirty = True

        def work():
            try:
                res = fn()
                self._jobs.put(("ok", done_msg(res) if callable(done_msg) else done_msg))
            except NMError as e:
                self._jobs.put(("auth" if e.auth else "err", str(e)))
            except Exception as e:  # never let a worker kill the UI
                self._jobs.put(("err", "%s: %s" % (type(e).__name__, e)))

        threading.Thread(target=work, daemon=True).start()

    def poll_jobs(self):
        try:
            kind, msg = self._jobs.get_nowait()
        except queue.Empty:
            return
        self.busy = None
        if kind == "imported":
            self.after_import(*msg)
        elif kind == "ok":
            if msg:
                self.show_toast(msg, GREEN)
        elif kind == "auth":
            self.auth_problem = True
            self.show_toast("Not allowed to change networks - press H", RED, 6)
        else:
            self.show_error(msg)
        self.refresh(force=True)

    def show_error(self, msg):
        """Short errors as a toast; long ones on a full screen so nothing is cut off."""
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        if _w(probe, msg, self.f.body) <= 300:
            self.show_toast(msg, RED, 5)
            return
        self.error_lines = wrap(probe, msg, self.f.body, WIDTH - 20)
        self.error_from = self.screen if self.screen != "error" else "list"
        self.screen = "error"
        self.dirty = True

    def show_toast(self, text, color=TEXT, secs=3.0):
        self.toast = (text, color, time.monotonic() + secs)
        self.dirty = True

    # ---------------------------------------------------------- actions
    def act_toggle(self):
        t = self.current()
        if not t:
            return
        if t.state in ("up", "activating"):
            self.run_job("Disconnecting %s" % t.name, lambda: self.nm.down(t),
                         "%s disconnected" % t.name)
        else:
            self.run_job("Connecting %s" % t.name, lambda: self.nm.up(t),
                         "%s connected" % t.name)

    def act_autoconnect(self):
        t = self.current()
        if not t:
            return
        on = not t.autoconnect
        self.run_job("Updating %s" % t.name, lambda: self.nm.set_autoconnect(t, on),
                     "Auto-connect %s for %s" % ("on" if on else "off", t.name))

    def act_delete(self):
        t = self.current()
        if not t:
            return

        def yes():
            self.screen = "list"
            self.run_job("Deleting %s" % t.name, lambda: self.nm.delete(t),
                         "%s deleted" % t.name)
        self.confirm = ("Delete this tunnel?", t.name, yes, "delete")
        self.dirty = True

    def open_import(self):
        self.screen = "import"
        self.scan_confs()

    def scan_confs(self):
        try:
            self.confs = self.find_confs()
        except Exception:
            self.confs = []
        self.conf_sel = min(self.conf_sel, max(0, len(self.confs) - 1))
        self.conf_top = 0
        self.dirty = True

    def act_import(self):
        if not self.confs:
            return
        path = self.confs[self.conf_sel]
        names = [t.name for t in self.tunnels]
        self.screen = "list"
        self.busy = "Importing %s" % os.path.basename(path)
        self.dirty = True

        def work():
            try:
                name, warns = self.nm.import_conf(path, names)
                self._jobs.put(("imported", (name, warns, path)))
            except NMError as e:
                self._jobs.put(("auth" if e.auth else "err", str(e)))
            except Exception as e:
                self._jobs.put(("err", "%s: %s" % (type(e).__name__, e)))

        threading.Thread(target=work, daemon=True).start()

    def after_import(self, name, warns, path):
        msg = "Imported as '%s'" % name
        if warns:
            msg += " - " + "; ".join(warns)
        self.show_toast(msg, AMBER if warns else GREEN, 6 if warns else 3)
        if path is None:
            return

        def remove():
            try:
                os.remove(path)
                self.show_toast("Original file removed", GREEN)
            except OSError:
                self.show_toast("Can't remove it here - delete %s yourself"
                                % os.path.basename(path), AMBER, 6)
            self.scan_confs()
        # The source file holds the private key in plain text; offer to remove it.
        self.confirm = ("Remove the original file?", "It contains your private key", remove,
                        "remove")

    # -- QR scanning
    def open_scan(self):
        if not self.has_camera:
            self.show_toast("No camera on this model", AMBER)
            return
        self.stop_scan()
        try:
            self.scanner = self.scanner_factory(accept=lambda t: check_conf_text(t) is None)
        except TypeError:
            self.scanner = self.scanner_factory()
        self.screen = "scan"
        self.dirty = True

    def stop_scan(self):
        if self.scanner:
            self.scanner.stop()
            self.scanner = None

    def poll_scan(self):
        sc = self.scanner
        if not sc:
            return
        if sc.error:
            err = sc.error
            self.stop_scan()
            self.screen = "import"
            self.show_toast(err, RED, 5)
            return
        if getattr(sc, "rejected", False):
            sc.rejected = False
            self.show_toast("That QR code isn't a WireGuard config", AMBER)
        text = sc.take_result()
        if text is None:
            return
        if check_conf_text(text):        # scanner without its own filter
            self.show_toast("That QR code isn't a WireGuard config", AMBER)
            return
        self.stop_scan()
        self.pending_conf = text
        taken = [t.name for t in self.tunnels]
        self.name_text = sanitize_ifname("vpn", taken)
        self.screen = "name"
        self.dirty = True

    def cancel_name(self):
        self.pending_conf = None
        self.name_text = ""
        self.screen = "import"

    def act_import_qr(self):
        name = self.name_text
        if not NAME_RE.match(name):
            self.show_toast("Start with a letter or digit", AMBER)
            return
        if name in [t.name for t in self.tunnels]:
            self.show_toast("'%s' already exists" % name, AMBER)
            return
        text, self.pending_conf = self.pending_conf, None
        self.screen = "list"
        self.busy = "Importing %s" % name
        self.dirty = True

        def work():
            try:
                res = self.nm.import_text(text, name)
                self._jobs.put(("imported", (res[0], res[1], None)))
            except NMError as e:
                self._jobs.put(("auth" if e.auth else "err", str(e)))
            except Exception as e:
                self._jobs.put(("err", "%s: %s" % (type(e).__name__, e)))

        threading.Thread(target=work, daemon=True).start()

    def open_help(self):
        self.help_from = self.screen
        self.screen = "help"
        self.dirty = True

    # ---------------------------------------------------------- input
    def save_screenshot(self):
        """fn+J: save the current screen like other CardputerZero apps do."""
        frame = getattr(self, "last_frame", None)
        if frame is None:
            return
        if self.screen == "scan":
            # The camera preview shows the QR code, which contains the private key.
            self.show_toast("No screenshots while scanning (the QR holds your key)", AMBER)
            return
        folder = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
        name = "wirezero_%s.png" % datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        try:
            os.makedirs(folder, exist_ok=True)
            fd = os.open(os.path.join(folder, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                frame.save(fh, "PNG")
            self.show_toast("Screenshot saved to ~/Pictures/Screenshots", GREEN)
        except OSError as e:
            self.show_toast("Screenshot failed: %s" % (e.strerror or e), RED)

    def handle(self, a):
        self.dirty = True
        if a.startswith("ch:") and self.screen != "name":
            return
        if self.screen == "name" and not (a.startswith("ch:") or a in
                                          ("backspace", "enter", "esc", "screenshot")):
            return
        if a == "screenshot":
            self.save_screenshot()
            return
        if self.confirm:
            if a == "enter":
                cb = self.confirm[2]
                self.confirm = None
                cb()
            elif a in ("esc", "left"):
                self.confirm = None
            return
        if self.screen == "fatal":
            if a in ("esc", "enter"):
                self.running = False
            return
        if a == "help":
            if self.screen == "help":
                self.screen = self.help_from
            else:
                self.open_help()
            return
        if self.screen == "help":
            if a in ("esc", "enter", "left"):
                self.screen = self.help_from
            return
        getattr(self, "key_" + self.screen)(a)

    def _move(self, sel, n, a, page=4):
        step = {"up": -1, "down": 1, "pageup": -page, "pagedown": page,
                "home": -10 ** 6, "end": 10 ** 6}.get(a)
        if step is None or n == 0:
            return sel
        return max(0, min(n - 1, sel + step))

    def key_list(self, a):
        if a in ("up", "down", "pageup", "pagedown", "home", "end"):
            self.sel = self._move(self.sel, len(self.tunnels), a)
        elif a == "esc":
            self.running = False
        elif self.busy:
            return
        elif a in ("enter", "k4"):
            self.act_toggle()
        elif a == "k5":
            self.open_import()
        elif a in ("k6", "right") and self.current():
            self.screen = "details"
            self.detail_scroll = 0
            self.load_details()
        elif a == "k7":
            self.act_autoconnect()
        elif a == "k8":
            self.act_delete()
        elif a == "refresh":
            self.refresh(force=True)

    def key_details(self, a):
        if a in ("esc", "left", "k6"):
            self.screen = "list"
        elif a == "up":
            self.detail_scroll = max(0, self.detail_scroll - 1)
        elif a == "down":
            self.detail_scroll = min(max(0, len(self.detail_rows) - 6), self.detail_scroll + 1)
        elif self.busy:
            return
        elif a in ("enter", "k4"):
            self.act_toggle()
        elif a == "k7":
            self.act_autoconnect()
        elif a == "k8":
            self.act_delete()
        elif a in ("k5", "refresh"):
            self.refresh(force=True)

    def key_import(self, a):
        if a in ("esc", "left"):
            self.screen = "list"
        elif a in ("up", "down", "pageup", "pagedown", "home", "end"):
            self.conf_sel = self._move(self.conf_sel, len(self.confs), a)
        elif a in ("k5", "refresh"):
            self.scan_confs()
            self.show_toast("Found %d config file%s" % (len(self.confs),
                            "" if len(self.confs) == 1 else "s"))
        elif a == "k6" and not self.busy:
            self.open_scan()
        elif a in ("enter", "k4") and not self.busy:
            self.act_import()

    def key_error(self, a):
        if a in ("esc", "enter", "left"):
            self.screen = self.error_from

    def key_scan(self, a):
        if a in ("esc", "left"):
            self.stop_scan()
            self.screen = "import"

    def key_name(self, a):
        if a.startswith("ch:"):
            if len(self.name_text) < 15:
                self.name_text += a[3:]
        elif a == "backspace":
            self.name_text = self.name_text[:-1]
        elif a == "enter":
            self.act_import_qr()
        elif a == "esc":
            self.cancel_name()

    # ---------------------------------------------------------- drawing
    def render(self):
        img = Image.new("RGB", (WIDTH, HEIGHT), BG)
        self._img = img
        d = ImageDraw.Draw(img)
        getattr(self, "draw_" + self.screen)(d)
        if self.confirm:
            self.draw_confirm(d)
        if self.busy:
            self.draw_busy(d)
        elif self.toast:
            self.draw_toast(d)
        return img

    def draw_header(self, d, title, right=None, right_color=DIM):
        d.rectangle((0, 0, WIDTH, HEIGHT_HEADER), fill=PANEL)
        d.text((8, 3), title, font=self.f.title, fill=TEXT)
        if right:
            w = _w(d, right, self.f.small)
            d.text((WIDTH - 8 - w, 3), right, font=self.f.small, fill=right_color)

    def draw_bar(self, d, icons):
        d.line((0, BAR_TOP, WIDTH, BAR_TOP), fill=LINE)
        for (kind, enabled, hot), cx in zip(icons, ICON_X):
            if kind is None:
                continue
            color = (GREEN if hot else TEXT) if enabled else FAINT
            draw_icon(d, kind, cx, ICON_Y, color)
            d.line((cx, ICON_Y + ICON // 2 + 3, cx, HEIGHT - 1), fill=FAINT if not enabled else DIM)

    def _net_label(self):
        n = self.net.lower()
        if n.startswith("connected") and "site" not in n and "local" not in n:
            return "Online", GREEN
        if n.startswith("connected"):
            return "Limited", AMBER
        if n.startswith("connecting"):
            return "Connecting", AMBER
        return "Offline", RED

    # -- list
    def draw_list(self, d):
        label, color = self._net_label()
        self.draw_header(d, "WireZero", label, color)
        if not self.tunnels:
            self.draw_empty(d)
        else:
            rows = (BAR_TOP - HEADER_H - 2) // ROW_H
            if self.sel < self.top:
                self.top = self.sel
            elif self.sel >= self.top + rows:
                self.top = self.sel - rows + 1
            y = HEADER_H + 2
            for i in range(self.top, min(len(self.tunnels), self.top + rows)):
                self.draw_row(d, self.tunnels[i], y, i == self.sel)
                y += ROW_H
            if len(self.tunnels) > rows:
                self.draw_scrollbar(d, self.top, rows, len(self.tunnels))
        t = self.current()
        up = bool(t and t.state in ("up", "activating"))
        self.draw_bar(d, [("power", t is not None, up), ("import", True, False),
                          ("info", t is not None, False),
                          ("auto", t is not None, bool(t and t.autoconnect)),
                          ("trash", t is not None, False)])

    def draw_empty(self, d):
        lines = [("No tunnels yet", self.f.name, TEXT),
                 ("Press 5 to import a WireGuard .conf.", self.f.body, DIM),
                 ("Put it in your home folder or on", self.f.body, DIM),
                 ("the SD card's boot partition first.", self.f.body, DIM)]
        y = 38
        for text, font, color in lines:
            w = _w(d, text, font)
            d.text(((WIDTH - w) // 2, y), text, font=font, fill=color)
            y += 22 if font is self.f.name else 17

    def draw_row(self, d, t, y, selected):
        if selected:
            d.rectangle((0, y, WIDTH - 6, y + ROW_H - 2), fill=SEL)
            d.rectangle((0, y, 2, y + ROW_H - 2), fill=ACCENT)
        color = {"up": GREEN, "activating": AMBER, "deactivating": AMBER}.get(t.state, FAINT)
        cy = y + ROW_H // 2 - 1
        d.ellipse((10, cy - 5, 20, cy + 5), fill=color)
        name = ellipsize(d, t.name, self.f.name, 170)
        d.text((28, y + 6), name, font=self.f.name, fill=TEXT)
        if t.state == "up":
            status = self.short_addr(t) or "Connected"
        else:
            status = {"activating": "Connecting...", "deactivating": "Stopping..."}.get(
                t.state, "Off")
        sw = _w(d, status, self.f.mono)
        d.text((WIDTH - 14 - sw, y + 7), status, font=self.f.mono,
               fill=GREEN if t.state == "up" else DIM)
        if t.autoconnect:
            nx = 28 + _w(d, name, self.f.name) + 6
            if nx < WIDTH - 24 - sw - 20:
                d.rounded_rectangle((nx, y + 8, nx + 14, y + 21), 3, outline=ACCENT)
                d.text((nx + 3, y + 7), "A", font=self.f.small, fill=ACCENT)

    def short_addr(self, t):
        cache = getattr(self, "_addr_cache", {})
        hit = cache.get(t.uuid)
        if hit and time.monotonic() - hit[1] < 10:
            return hit[0]
        addr = None
        try:
            for k, v in self.nm.details(t):
                if k == "Address":
                    addr = v.split(",")[0].split("/")[0]
                    break
        except NMError:
            pass
        cache[t.uuid] = (addr, time.monotonic())
        self._addr_cache = cache
        return addr

    def draw_scrollbar(self, d, top, visible, total):
        h = BAR_TOP - HEADER_H - 4
        th = max(10, h * visible // total)
        ty = HEADER_H + 2 + (h - th) * top // max(1, total - visible)
        d.rectangle((WIDTH - 4, ty, WIDTH - 2, ty + th), fill=LINE)

    # -- details
    def draw_details(self, d):
        t = self.current()
        if not t:
            return
        color = {"up": GREEN, "activating": AMBER}.get(t.state, DIM)
        state = {"up": "Connected", "activating": "Connecting"}.get(t.state, "Off")
        self.draw_header(d, ellipsize(d, t.name, self.f.title, 200), state, color)
        y = HEADER_H + 4
        rows = self.detail_rows[self.detail_scroll:self.detail_scroll + 6]
        for k, v in rows:
            d.text((8, y), k, font=self.f.body, fill=DIM)
            d.text((96, y), ellipsize(d, v, self.f.mono, WIDTH - 104), font=self.f.mono,
                   fill=TEXT)
            y += 18
        if len(self.detail_rows) > 6:
            self.draw_scrollbar(d, self.detail_scroll, 6, len(self.detail_rows))
        up = t.state in ("up", "activating")
        self.draw_bar(d, [("power", True, up), ("refresh", True, False), ("back", True, False),
                          ("auto", True, t.autoconnect), ("trash", True, False)])

    # -- import
    def draw_import(self, d):
        self.draw_header(d, "Import tunnel", "%d found" % len(self.confs))
        if not self.confs:
            lines = ["No WireGuard .conf files found.", "",
                     "Press 6 to scan a QR code, or copy",
                     "a .conf to your home folder, a USB",
                     "stick or the SD card's boot partition",
                     "and press 5 to look again."]
            y = HEADER_H + 8
            for line in lines:
                d.text((10, y), line, font=self.f.body, fill=DIM if line else TEXT)
                y += 16
        else:
            rh = 36
            rows = (BAR_TOP - HEADER_H - 2) // rh
            if self.conf_sel < self.conf_top:
                self.conf_top = self.conf_sel
            elif self.conf_sel >= self.conf_top + rows:
                self.conf_top = self.conf_sel - rows + 1
            y = HEADER_H + 2
            home = os.path.expanduser("~")
            for i in range(self.conf_top, min(len(self.confs), self.conf_top + rows)):
                p = self.confs[i]
                if i == self.conf_sel:
                    d.rectangle((0, y, WIDTH - 6, y + rh - 2), fill=SEL)
                    d.rectangle((0, y, 2, y + rh - 2), fill=ACCENT)
                d.text((10, y + 2), ellipsize(d, os.path.basename(p), self.f.name, 290),
                       font=self.f.name, fill=TEXT)
                folder = os.path.dirname(p)
                if folder.startswith(home):
                    folder = "~" + folder[len(home):]
                d.text((10, y + 19), ellipsize(d, folder, self.f.small, 290), font=self.f.small,
                       fill=DIM)
                y += rh
            if len(self.confs) > rows:
                self.draw_scrollbar(d, self.conf_top, rows, len(self.confs))
        self.draw_bar(d, [("import", bool(self.confs), False), ("refresh", True, False),
                          ("camera", self.has_camera, False), (None, False, False),
                          (None, False, False)])

    # -- scan
    def draw_scan(self, d):
        pw = HEIGHT          # square preview, exactly the area that is decoded
        frame = self.scanner.latest() if self.scanner else None
        if frame:
            grey, w, h = frame
            img = Image.frombytes("L", (w, h), grey).resize((pw, pw), Image.BILINEAR)
            self._img.paste(img.convert("RGB"), (0, 0))
        else:
            d.rectangle((0, 0, pw - 1, HEIGHT), fill=(0, 0, 0))
            msg = "Starting camera..."
            d.text(((pw - _w(d, msg, self.f.body)) // 2, HEIGHT // 2 - 7), msg,
                   font=self.f.body, fill=DIM)
        L = 22
        x0, y0, x1, y1 = 3, 3, pw - 4, HEIGHT - 4
        for (x, y, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            d.line((x, y, x + dx * L, y), fill=GREEN, width=3)
            d.line((x, y, x, y + dy * L), fill=GREEN, width=3)
        d.rectangle((pw, 0, WIDTH, HEIGHT), fill=PANEL)
        y = 8
        for text, font, color in (("Scan QR", self.f.name, TEXT), ("", self.f.body, DIM),
                                  ("Fill the green", self.f.body, DIM),
                                  ("corners with the", self.f.body, DIM),
                                  ("QR code. Hold", self.f.body, DIM),
                                  ("still, ~25 cm", self.f.body, DIM),
                                  ("away.", self.f.body, DIM)):
            d.text((pw + 8, y), text, font=font, fill=color)
            y += 20 if font is self.f.name else 16
        d.text((pw + 8, HEIGHT - 22), "Esc: cancel", font=self.f.body, fill=DIM)

    # -- name
    def draw_name(self, d):
        self.draw_header(d, "Name this tunnel", "QR code read", GREEN)
        d.text((10, HEADER_H + 10), "Tunnel name:", font=self.f.body, fill=DIM)
        box = (10, HEADER_H + 30, WIDTH - 10, HEADER_H + 58)
        ok = bool(NAME_RE.match(self.name_text))
        d.rectangle(box, fill=SEL, outline=ACCENT if ok else AMBER, width=2)
        d.text((18, HEADER_H + 35), self.name_text, font=self.f.big, fill=TEXT)
        cx = 18 + _w(d, self.name_text, self.f.big) + 2
        if int(time.monotonic() * 2) % 2 == 0:
            d.rectangle((cx, HEADER_H + 36, cx + 1, HEADER_H + 53), fill=TEXT)
        y = HEADER_H + 68
        for line in ("Letters, digits, - and _ (max 15).",
                     "Enter: import    Esc: discard"):
            d.text((10, y), line, font=self.f.body, fill=DIM)
            y += 17
        d.text((10, y + 4), "The QR code contained your private key.", font=self.f.small, fill=AMBER)
        d.text((10, y + 20), "It is only kept in memory until imported.", font=self.f.small,
               fill=AMBER)

    # -- error
    def draw_error(self, d):
        self.draw_header(d, "Something went wrong", "Esc to close", RED)
        y = HEADER_H + 8
        for line in self.error_lines[:7]:
            d.text((10, y), line, font=self.f.body, fill=TEXT)
            y += 16
        d.text((10, HEIGHT - 20), "fn+J saves a screenshot of this", font=self.f.small, fill=DIM)

    # -- help
    def draw_help(self, d):
        self.draw_header(d, "WireZero help", "Esc to close")
        if self.auth_problem:
            lines = [("Permission problem", RED),
                     ("The system refused the change.", TEXT),
                     ("Reinstall WireZero, or run:", DIM),
                     ("sudo usermod -aG wirezero $USER", ACCENT),
                     ("then reboot the CardputerZero.", DIM)]
            y = HEADER_H + 6
            for text, color in lines:
                font = self.f.mono if color == ACCENT else self.f.body
                d.text((8, y), text, font=font, fill=color)
                y += 17
            d.line((0, y + 2, WIDTH, y + 2), fill=LINE)
            y += 6
        else:
            y = HEADER_H + 4
        keys = [("Enter / 4", "Connect or disconnect"), ("5", "Import (6 there: scan QR)"),
                ("6 / C", "Tunnel details"), ("7", "Toggle auto-connect at boot"),
                ("8", "Delete tunnel"), ("F / X", "Move up / down"), ("Esc", "Back / quit")]
        for k, v in keys:
            if y > HEIGHT - 16:
                break
            d.text((8, y), k, font=self.f.mono, fill=ACCENT)
            d.text((88, y), v, font=self.f.body, fill=TEXT)
            y += 16

    # -- fatal
    def draw_fatal(self, d):
        self.draw_header(d, "WireZero", "Error", RED)
        y = HEADER_H + 12
        for line in self.fatal:
            d.text((10, y), line, font=self.f.body, fill=TEXT if y == HEADER_H + 12 else DIM)
            y += 18
        d.text((10, HEIGHT - 22), "Press Esc to exit", font=self.f.body, fill=DIM)

    # -- overlays
    def draw_confirm(self, d):
        q, detail, _, yes_label = self.confirm
        box = (24, 34, WIDTH - 24, 126)
        d.rectangle(box, fill=PANEL, outline=RED if yes_label == "delete" else AMBER, width=2)
        for text, font, color, y in ((q, self.f.name, TEXT, 44),
                                     (ellipsize(d, detail, self.f.body, 260), self.f.body, AMBER, 66),
                                     ("Enter = %s    Esc = %s" % (yes_label, "keep" if yes_label == "remove" else "cancel"),
                                      self.f.body, DIM, 98)):
            w = _w(d, text, font)
            d.text(((WIDTH - w) // 2, y), text, font=font, fill=color)

    def draw_busy(self, d):
        y0 = BAR_TOP - 26
        d.rectangle((0, y0, WIDTH, BAR_TOP - 1), fill=PANEL)
        d.line((0, y0, WIDTH, y0), fill=ACCENT)
        cx, cy = 16, y0 + 13
        for i in range(8):
            ang = (i + self._spin) * math.pi / 4
            c = tuple(int(v * (0.3 + 0.7 * i / 7)) for v in ACCENT)
            d.ellipse((cx + 6 * math.cos(ang) - 1.5, cy + 6 * math.sin(ang) - 1.5,
                       cx + 6 * math.cos(ang) + 1.5, cy + 6 * math.sin(ang) + 1.5), fill=c)
        d.text((30, y0 + 6), ellipsize(d, self.busy + "...", self.f.body, 280),
               font=self.f.body, fill=TEXT)

    def draw_toast(self, d):
        text, color, until = self.toast
        if time.monotonic() > until:
            self.toast = None
            return
        y0 = BAR_TOP - 26
        d.rectangle((0, y0, WIDTH, BAR_TOP - 1), fill=PANEL)
        d.line((0, y0, WIDTH, y0), fill=color)
        d.text((10, y0 + 6), ellipsize(d, text, self.f.body, 300), font=self.f.body, fill=color)

    # ---------------------------------------------------------- main loop
    def start(self):
        err = self.nm.check()
        if err:
            self.screen = "fatal"
            self.fatal = [err, "WireZero needs NetworkManager", "to manage WireGuard tunnels."]
        else:
            self.refresh(force=True)

    def tick(self, actions):
        for a in actions:
            self.handle(a)
        self.poll_jobs()
        self.poll_scan()
        if self.screen in ("scan", "name"):
            self.dirty = True
        if self.screen not in ("fatal", "scan", "name") and not self.busy:
            self.refresh()
        if self.busy:
            self._spin = (self._spin + 1) % 8
            self.dirty = True
        if self.toast and time.monotonic() > self.toast[2]:
            self.toast = None
            self.dirty = True
        if self.dirty:
            self.last_frame = self.render()
            self.display.show(self.last_frame)
            self.dirty = False

    def run(self):
        try:
            self._run()
        finally:
            self.stop_scan()
            self.pending_conf = None

    def _run(self):
        self.start()
        self.last_frame = self.render()
        self.display.show(self.last_frame)
        while self.running:
            timeout = 0.08 if (self.busy or self.screen == "scan") else (
                0.25 if self.screen == "name" else 0.5)
            self.tick(self.keys.poll(timeout))


HEIGHT_HEADER = HEADER_H
