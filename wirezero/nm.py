"""NetworkManager backend for WireZero.

Status is read with `nmcli` as the normal user. Anything that changes a
tunnel (connect, disconnect, import, delete, auto-connect) goes through
wirezero-helper via sudo, which only ever acts on WireGuard profiles.
"""

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass

NMCLI = "nmcli"
SUDO = "/usr/bin/sudo"
HELPER = "/usr/lib/wirezero/wirezero-helper"
ASKPASS = "/usr/lib/wirezero/wirezero-askpass"
PASSWORD_ENV = "WIREZERO_SUDO_PASSWORD"
IFNAME_MAX = 15
MAX_CONF_BYTES = 64 * 1024
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,14}")   # use .fullmatch()
SCRIPT_KEYS = ("PreUp", "PostUp", "PreDown", "PostDown", "SaveConfig")


class NMError(Exception):
    def __init__(self, message, auth=False, need_password=False, bad_password=False):
        super().__init__(message)
        self.auth = auth
        self.need_password = need_password
        self.bad_password = bad_password


@dataclass
class Tunnel:
    name: str
    uuid: str
    autoconnect: bool
    state: str          # "down", "activating", "up", "deactivating"
    device: str = ""


def split_terse(line):
    """Split one `nmcli -t` line on unescaped ':' and unescape '\\:' / '\\\\'."""
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def parse_connections(out):
    """Parse `nmcli -t -f NAME,UUID,TYPE,AUTOCONNECT,ACTIVE,DEVICE connection show`."""
    tunnels = []
    for line in out.splitlines():
        if not line.strip():
            continue
        f = split_terse(line)
        if len(f) < 6 or f[2] != "wireguard":
            continue
        tunnels.append(Tunnel(
            name=f[0], uuid=f[1], autoconnect=(f[3] == "yes"),
            state="up" if f[4] == "yes" else "down",
            device=f[5] if f[5] not in ("", "--") else ""))
    return tunnels


def parse_active_states(out):
    """Parse `nmcli -t -f UUID,STATE connection show --active` -> {uuid: state}."""
    states = {}
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) >= 2 and f[0]:
            states[f[0]] = f[1]
    return states


STATE_MAP = {
    "activating": "activating",
    "activated": "up",
    "deactivating": "deactivating",
    "deactivated": "down",
}


def parse_show(out):
    """Parse `nmcli -t connection show <id>` -> {key: [values]} (keeps order)."""
    props = {}
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) < 2:
            continue
        key = re.sub(r"\[\d+\]$", "", f[0])
        val = ":".join(f[1:]).strip()
        if val in ("", "--"):
            continue
        props.setdefault(key, []).append(val)
    return props


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def sanitize_ifname(name, taken=()):
    """Turn a file name into a valid, unused Linux interface name."""
    base = re.sub(r"[^A-Za-z0-9_-]", "", name).lstrip("-_")[:IFNAME_MAX] or "wg"
    cand, n = base, 1
    taken = set(taken)
    while cand in taken:
        suffix = str(n)
        cand = base[:IFNAME_MAX - len(suffix)] + suffix
        n += 1
    return cand


def check_conf_text(text):
    """Sanity-check WireGuard config text. Returns an error string or None."""
    if "\x00" in text:
        return "Config contains binary data"
    if not re.search(r"^\s*\[Interface\]", text, re.M | re.I):
        return "Not a WireGuard config ([Interface] missing)"
    if not re.search(r"^\s*PrivateKey\s*=", text, re.M | re.I):
        return "Config has no PrivateKey"
    if not re.search(r"^\s*\[Peer\]", text, re.M | re.I):
        return "Config has no [Peer] section"
    return None


_KEY_LINE = re.compile(r"^(\s*)(PrivateKey|PublicKey|PresharedKey)(\s*=\s*)(.*)$", re.I)


def _key_ok(value):
    import base64
    import binascii
    if len(value) != 44 or not value.endswith("="):
        return False
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


def normalize_keys(text):
    """Clean up key lines (stray spaces, invisible/non-ASCII characters, CRs).

    Returns (text, problems). `problems` describes keys that are still invalid
    without ever revealing the key itself.
    """
    out, problems = [], []
    for n, line in enumerate(text.splitlines(), 1):
        m = _KEY_LINE.match(line)
        if not m:
            out.append(line)
            continue
        raw = m.group(4).split("#", 1)[0]          # allow "Key = abc= # comment"
        cleaned = re.sub(r"[^A-Za-z0-9+/=]", "", raw)
        if not _key_ok(cleaned) and len(cleaned) == 43 and not cleaned.endswith("="):
            cleaned += "="                      # padding lost somewhere
        if not _key_ok(cleaned):
            odd = sorted({c for c in raw if not (c.isalnum() or c in "+/= ")})
            desc = "%d characters" % len(raw.strip())
            if odd:
                desc += ", unexpected: %s" % " ".join(
                    repr(c) if c.isprintable() else "U+%04X" % ord(c) for c in odd[:4])
            if not raw.strip():
                if m.group(2).lower() == "privatekey":
                    problems.append("PrivateKey (line %d) is empty: this config doesn't include "
                                    "the device's private key. Regenerate this client's keys on "
                                    "your VPN server and import the new config." % n)
                    out.append(line)
                    continue
                desc = "empty"
            elif re.search(r"[<>*]|hidden|redacted|replace", raw, re.I):
                desc += ", looks like a placeholder"
            problems.append("%s (line %d): %s; a WireGuard key is 44 characters ending in '='"
                            % (m.group(2), n, desc))
            out.append(line)
        else:
            out.append(m.group(1) + m.group(2) + m.group(3) + cleaned)
    return "\n".join(out) + "\n", problems


def _values(text, key):
    pat = re.compile(r"^\s*%s\s*=(.*)$" % key, re.I)
    vals = []
    for line in text.splitlines():
        m = pat.match(line)
        if m:
            vals += [v.strip() for v in m.group(1).split("#", 1)[0].split(",") if v.strip()]
    return vals


def is_full_tunnel(text):
    """True if AllowedIPs sends all IPv4 traffic through the tunnel."""
    allowed = set(_values(text, "AllowedIPs"))
    return "0.0.0.0/0" in allowed or {"0.0.0.0/1", "128.0.0.0/1"} <= allowed


def covers_ipv6(text):
    allowed = set(_values(text, "AllowedIPs"))
    return "::/0" in allowed or {"::/1", "8000::/1"} <= allowed


def policy_problems(text):
    """Settings WireZero refuses because they could hijack system routing."""
    probs = []
    for v in _values(text, "Table"):
        if v.lower() not in ("auto", "off"):
            probs.append("Table = %s (only auto/off are allowed)" % v)
    if _values(text, "FwMark"):
        probs.append("FwMark is not allowed")
    for v in _values(text, "ListenPort"):
        if not v.isdigit() or (int(v) != 0 and not 1024 <= int(v) <= 65535):
            probs.append("ListenPort = %s (must be 0 or 1024-65535)" % v)
    return probs


def script_lines(text):
    """wg-quick hook lines (PostUp etc). NetworkManager never runs them."""
    pat = r"^\s*(%s)\s*=" % "|".join(SCRIPT_KEYS)
    return [m.group(1) for m in re.finditer(pat, text, re.M | re.I)]


def clean_conf_text(text):
    """Drop wg-quick hook lines so no shell command ever reaches the system."""
    pat = re.compile(r"^\s*(%s)\s*=.*$" % "|".join(SCRIPT_KEYS), re.I)
    return "\n".join(l for l in text.splitlines() if not pat.match(l)) + "\n"


_KEY_RE = re.compile(r"[A-Za-z0-9+/]{42,43}=")


def redact(text):
    """Hide anything that looks like a WireGuard key in a message."""
    return _KEY_RE.sub("[key hidden]", text)


def read_conf(path):
    """Read a config the user picked: regular file, not a symlink, bounded size."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        raise NMError("Can't open file: %s" % (e.strerror or e))
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise NMError("Not a regular file")
        data = fh.read(MAX_CONF_BYTES + 1)
    if len(data) > MAX_CONF_BYTES:
        raise NMError("Config file is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise NMError("Config is not valid text")


def check_conf(path):
    """Error string or None for a config file on disk."""
    try:
        return check_conf_text(read_conf(path))
    except NMError as e:
        return str(e)


def _first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return re.sub(r"^Error:\s*", "", line)
    return "Unknown error"


AUTH_PATTERNS = ("not authorized", "insufficient privileges", "permission denied",
                 "not allowed", "a password is required", "not in the sudoers",
                 "may not run sudo", "is not allowed to execute")


class NMBackend:
    def __init__(self, runner=None):
        self._run_impl = runner or self._subprocess_run
        self.password = None        # set by the UI for a short while, never stored

    @staticmethod
    def _subprocess_run(args, timeout, stdin_text=None, extra_env=None):
        env = dict(os.environ, LC_ALL="C")
        env.pop(PASSWORD_ENV, None)
        if extra_env:
            env.update(extra_env)
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           input=stdin_text,
                           stdin=None if stdin_text is not None else subprocess.DEVNULL,
                           env=env)
        return p.returncode, p.stdout, p.stderr

    def _exec(self, argv, timeout, stdin_text=None, missing="NetworkManager (nmcli) is not installed",
              extra_env=None):
        try:
            if extra_env is not None:
                code, out, err = self._run_impl(argv, timeout, stdin_text, extra_env)
            elif stdin_text is None:
                code, out, err = self._run_impl(argv, timeout)
            else:
                code, out, err = self._run_impl(argv, timeout, stdin_text)
        except FileNotFoundError:
            raise NMError(missing)
        except subprocess.TimeoutExpired:
            raise NMError("NetworkManager did not answer in time")
        if code != 0:
            msg = redact(_first_line(err or out))
            raise NMError(msg, auth=any(p in msg.lower() for p in AUTH_PATTERNS))
        return out

    def _run(self, *args, timeout=10):
        return self._exec([NMCLI] + list(args), timeout)

    def _helper(self, *args, stdin_text=None, timeout=40):
        """Run the root helper through sudo.

        First without a password (works if sudo doesn't need one). Otherwise
        with the password the user typed, handed to sudo by a tiny askpass
        program through the environment: never on the command line, and never
        on stdin, which carries the config.
        """
        try:
            return self._exec([SUDO, "-n", HELPER] + list(args), timeout, stdin_text,
                              missing="sudo is not installed")
        except NMError as e:
            low = str(e).lower()
            if "password is required" not in low:
                if "sudoers" in low or "may not run sudo" in low:
                    raise NMError("Your user isn't allowed to use sudo, so WireZero can't "
                                  "change tunnels.", auth=True)
                raise
        if not self.password:
            raise NMError("Password needed", auth=True, need_password=True)
        try:
            return self._exec([SUDO, "-A", "-k", HELPER] + list(args), timeout, stdin_text,
                              missing="sudo is not installed",
                              extra_env={"SUDO_ASKPASS": ASKPASS, PASSWORD_ENV: self.password})
        except NMError as e:
            low = str(e).lower()
            if "incorrect password" in low or "try again" in low or "no password was provided" in low:
                self.password = None
                raise NMError("Wrong password", auth=True, need_password=True, bad_password=True)
            raise

    # -- queries -----------------------------------------------------------
    def check(self):
        """Return None if NetworkManager is usable, else an error message."""
        if shutil.which(NMCLI) is None and self._run_impl == self._subprocess_run:
            return "NetworkManager (nmcli) is not installed"
        try:
            state = self._run("-t", "-f", "RUNNING", "general")
        except NMError as e:
            return str(e)
        if "running" not in state:
            return "NetworkManager is not running"
        return None

    def net_state(self):
        try:
            return self._run("-t", "-f", "STATE", "general").strip() or "unknown"
        except NMError:
            return "unknown"

    def list_tunnels(self):
        out = self._run("-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT,ACTIVE,DEVICE",
                        "connection", "show")
        tunnels = parse_connections(out)
        if any(t.state == "up" for t in tunnels):
            states = parse_active_states(
                self._run("-t", "-f", "UUID,STATE", "connection", "show", "--active"))
            for t in tunnels:
                if t.uuid in states:
                    t.state = STATE_MAP.get(states[t.uuid], t.state)
        tunnels.sort(key=lambda t: t.name.lower())
        return tunnels

    def details(self, tunnel):
        props = parse_show(self._run("-t", "connection", "show", "uuid", tunnel.uuid))
        get = lambda k: ", ".join(props.get(k, [])) or None  # noqa: E731
        state = {"up": "Connected", "activating": "Connecting...",
                 "deactivating": "Disconnecting...", "down": "Disconnected"}[tunnel.state]
        rows = [("Status", state),
                ("Interface", get("connection.interface-name") or tunnel.device or "-")]
        addr = get("IP4.ADDRESS") or get("ipv4.addresses")
        if addr:
            rows.append(("Address", addr))
        addr6 = get("IP6.ADDRESS") or get("ipv6.addresses")
        if addr6:
            rows.append(("IPv6", addr6))
        dns = get("IP4.DNS") or get("ipv4.dns")
        if dns:
            rows.append(("DNS", dns))
        endpoint = None
        for key, vals in props.items():
            if "peer" in key.lower():
                m = re.search(r"endpoint[=:]\s*([^\s,;]+)", " ".join(vals), re.I)
                if m:
                    endpoint = m.group(1)
        if endpoint:
            rows.append(("Endpoint", endpoint))
        port = get("wireguard.listen-port")
        if port and port != "0":
            rows.append(("Listen port", port))
        iface = tunnel.device or get("connection.interface-name")
        rx, tx = self.traffic(iface) if tunnel.state == "up" else (None, None)
        if rx is not None:
            rows.append(("Received", human_bytes(rx)))
            rows.append(("Sent", human_bytes(tx)))
        rows.append(("Auto-connect", "On" if tunnel.autoconnect else "Off"))
        return rows

    @staticmethod
    def traffic(iface):
        if not iface:
            return None, None
        base = "/sys/class/net/%s/statistics/" % iface
        try:
            with open(base + "rx_bytes") as a, open(base + "tx_bytes") as b:
                return int(a.read()), int(b.read())
        except (OSError, ValueError):
            return None, None

    # -- actions -----------------------------------------------------------
    def up(self, tunnel):
        # Plain NetworkManager first: on most images the user may connect
        # without a password. Fall back to the helper only if refused.
        try:
            self._run("--wait", "25", "connection", "up", "uuid", tunnel.uuid, timeout=35)
        except NMError as e:
            if not e.auth:
                raise
            self._helper("up", tunnel.uuid, timeout=45)

    def down(self, tunnel):
        try:
            self._run("connection", "down", "uuid", tunnel.uuid, timeout=20)
        except NMError as e:
            if not e.auth:
                raise
            self._helper("down", tunnel.uuid, timeout=30)

    def set_autoconnect(self, tunnel, on):
        self._helper("autoconnect", tunnel.uuid, "yes" if on else "no")

    def delete(self, tunnel):
        self._helper("delete", tunnel.uuid)

    def import_conf(self, path, existing=()):
        """Import a wg-quick style .conf. Returns (tunnel name, warnings)."""
        text = read_conf(path)
        err = check_conf_text(text)
        if err:
            raise NMError(err)
        stem = os.path.basename(path)
        stem = stem[:-5] if stem.lower().endswith(".conf") else stem
        taken = set(existing)
        try:
            taken |= set(os.listdir("/sys/class/net"))
        except OSError:
            pass
        return self.import_text(text, sanitize_ifname(stem, taken))

    def import_text(self, text, name):
        """Import config text (e.g. from a QR code) under `name`."""
        err = check_conf_text(text)
        if err:
            raise NMError(err)
        text, problems = normalize_keys(text)
        if problems:
            raise NMError("Invalid key in config. " + " ".join(problems))
        policy = policy_problems(text)
        if policy:
            raise NMError("Config not allowed: " + "; ".join(policy))
        if not NAME_RE.fullmatch(name):
            raise NMError("Invalid tunnel name")
        self._helper("import", name, stdin_text=text)
        return name, import_warnings(text)


def import_warnings(text):
    warns = []
    if script_lines(text):
        warns.append("Its PostUp/PreUp lines were skipped.")
    if is_full_tunnel(text) and not covers_ipv6(text):
        warns.append("IPv6 traffic will use your normal connection.")
    return warns


def find_conf_files(extra_dirs=()):
    """Look for WireGuard .conf files in the places a user can easily drop them."""
    home = os.path.expanduser("~")
    dirs = [home, os.path.join(home, "wireguard"), os.path.join(home, "Downloads"),
            "/boot/firmware", "/boot", "/boot/firmware/wireguard"]
    for root in ("/media", "/run/media", "/mnt"):
        try:
            for a in os.listdir(root):
                p = os.path.join(root, a)
                dirs.append(p)
                if os.path.isdir(p):
                    try:
                        dirs += [os.path.join(p, b) for b in os.listdir(p)]
                    except OSError:
                        pass
        except OSError:
            pass
    dirs += list(extra_dirs)
    seen, found = set(), []
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.lower().endswith(".conf"):
                continue
            p = os.path.realpath(os.path.join(d, n))
            if p in seen or not os.path.isfile(p):
                continue
            seen.add(p)
            if check_conf(p) is None:
                found.append(p)
    return found


class FakeBackend:
    """In-memory stand-in used by the desktop simulator and tests."""

    def __init__(self):
        self.tunnels = [
            Tunnel("home", "11111111-aaaa", True, "up", "home"),
            Tunnel("office", "22222222-bbbb", False, "down"),
            Tunnel("vps-london", "33333333-cccc", False, "down"),
        ]
        self.fail_auth = False
        self.require_password = None   # e.g. "secret": admin actions need it
        self.password = None

    def check(self):
        return None

    def net_state(self):
        return "connected"

    def list_tunnels(self):
        return [Tunnel(**vars(t)) for t in sorted(self.tunnels, key=lambda t: t.name)]

    def _find(self, t):
        return next(x for x in self.tunnels if x.uuid == t.uuid)

    def details(self, t):
        t = self._find(t)
        rows = [("Status", "Connected" if t.state == "up" else "Disconnected"),
                ("Interface", t.name), ("Address", "10.8.0.4/24"),
                ("DNS", "10.8.0.1"), ("Endpoint", "vpn.example.net:51820")]
        if t.state == "up":
            rows += [("Received", "3.4 MB"), ("Sent", "812.0 KB")]
        rows.append(("Auto-connect", "On" if t.autoconnect else "Off"))
        return rows

    def traffic(self, iface):
        return 3_565_158, 831_488

    def _guard(self, admin=True):
        if self.fail_auth:
            raise NMError("Not authorized to control networking.", auth=True)
        if admin and self.require_password is not None:
            if self.password is None:
                raise NMError("Password needed", auth=True, need_password=True)
            if self.password != self.require_password:
                self.password = None
                raise NMError("Wrong password", auth=True, need_password=True, bad_password=True)

    def up(self, t):
        self._guard(admin=False)
        self._find(t).state = "up"

    def down(self, t):
        self._guard(admin=False)
        self._find(t).state = "down"

    def set_autoconnect(self, t, on):
        self._guard()
        self._find(t).autoconnect = on

    def delete(self, t):
        self._guard()
        self.tunnels.remove(self._find(t))

    def import_conf(self, path, existing=()):
        self._guard()
        name = sanitize_ifname(os.path.basename(path)[:-5], existing)
        return self.import_text("", name)

    def import_text(self, text, name):
        self._guard()
        self.tunnels.append(Tunnel(name, "new-%d" % len(self.tunnels), False, "down"))
        return name, []
