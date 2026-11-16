"""wirezero-helper: the only part of WireZero that runs as root.

It is reached through one sudoers rule (members of the `wirezero` group, no
password) and can only act on WireGuard profiles:

    import NAME          WireGuard config text on stdin -> new NM profile
    up UUID / down UUID  connect / disconnect a WireGuard profile
    delete UUID          delete a WireGuard profile
    autoconnect UUID yes|no

It never reads files named by the caller, never prints secrets and never
touches non-WireGuard connections. Everything else (connect/disconnect,
status) is done by the app as the normal user through NetworkManager.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

from .nm import (MAX_CONF_BYTES, NAME_RE, check_conf_text, clean_conf_text, covers_ipv6,
                 is_full_tunnel, normalize_keys, policy_problems, redact)

NMCLI = "/usr/bin/nmcli"
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
TMP_ROOT = "/run" if os.path.isdir("/run") else None   # tmpfs, root-only: keys never hit disk
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


class Fail(Exception):
    pass


def nmcli(*args, timeout=20):
    p = subprocess.run([NMCLI] + list(args), capture_output=True, text=True,
                       timeout=timeout, env=ENV, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "nmcli failed").strip().splitlines()
        text = re.sub(r"^Error:\s*", "", msg[0] if msg else "nmcli failed")
        # "failed to import '/tmp/wirezero-xxx/name.conf': <reason>" -> "<reason>"
        text = re.sub(r"^failed to import '[^']*':\s*", "Import failed: ", text)
        text = re.sub(r"\s+at /tmp/wirezero-[^/\s]+/[^:\s]+:(\d+)", r" on line \1", text)
        if len(msg) > 1:
            text += " " + " ".join(m.strip() for m in msg[1:3])
        raise Fail(redact(text))
    return p.stdout


def require_wireguard(uuid):
    if not UUID_RE.fullmatch(uuid):
        raise Fail("Invalid tunnel id")
    if nmcli("-g", "connection.type", "connection", "show", "uuid", uuid).strip() != "wireguard":
        raise Fail("Not a WireGuard tunnel")


def do_import(name):
    if not NAME_RE.fullmatch(name):
        raise Fail("Invalid tunnel name")
    data = sys.stdin.buffer.read(MAX_CONF_BYTES + 1)
    if len(data) > MAX_CONF_BYTES:
        raise Fail("Config file is too large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Fail("Config is not valid text")
    err = check_conf_text(text)
    if err:
        raise Fail(err)
    text, problems = normalize_keys(text)
    if problems:
        raise Fail("Invalid key in config. " + " ".join(problems))
    policy = policy_problems(text)
    if policy:
        raise Fail("Config not allowed: " + "; ".join(policy))
    existing = set(nmcli("-g", "NAME", "connection", "show").splitlines())
    if name in existing or os.path.exists("/sys/class/net/" + name):
        raise Fail("A tunnel or interface called '%s' already exists" % name)

    old_umask = os.umask(0o077)
    tmpdir = tempfile.mkdtemp(prefix="wirezero-", dir=TMP_ROOT)
    try:
        path = os.path.join(tmpdir, name + ".conf")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(clean_conf_text(text))
        out = nmcli("connection", "import", "type", "wireguard", "file", path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        os.umask(old_umask)

    # "Connection 'name' (uuid) successfully added." -> take the uuid from here
    m = re.search(r"\(([0-9a-fA-F-]{36})\)", out or "")
    uuid = m.group(1) if m and UUID_RE.fullmatch(m.group(1)) else None
    if not uuid:
        for line in nmcli("-t", "-f", "NAME,UUID,TYPE", "connection", "show").splitlines():
            parts = line.rsplit(":", 2)
            if len(parts) == 3 and parts[0] == name and parts[2] == "wireguard":
                uuid = parts[1]
                break
    if not uuid:
        raise Fail("Import finished but the tunnel was not found")
    # Auto-connect off FIRST (so NM won't bring it back), then make sure it is down.
    nmcli("connection", "modify", "uuid", uuid, "connection.autoconnect", "no")
    try:
        nmcli("connection", "down", "uuid", uuid)
    except Fail:
        pass
    # Full tunnel: make the tunnel's DNS servers the only ones used, so DNS
    # lookups cannot leak to the local network's resolver.
    if is_full_tunnel(text) and re.search(r"^\s*DNS\s*=", text, re.M | re.I):
        nmcli("connection", "modify", "uuid", uuid, "ipv4.dns-priority", "-50")
        if covers_ipv6(text):
            nmcli("connection", "modify", "uuid", uuid, "ipv6.dns-priority", "-50")
    print(name)


def main(argv):
    if os.geteuid() != 0:
        print("wirezero-helper must be run through sudo", file=sys.stderr)
        return 2
    try:
        if len(argv) == 2 and argv[0] == "import":
            do_import(argv[1])
        elif len(argv) == 2 and argv[0] == "up":
            require_wireguard(argv[1])
            nmcli("--wait", "25", "connection", "up", "uuid", argv[1], timeout=35)
        elif len(argv) == 2 and argv[0] == "down":
            require_wireguard(argv[1])
            nmcli("connection", "down", "uuid", argv[1])
        elif len(argv) == 2 and argv[0] == "delete":
            require_wireguard(argv[1])
            nmcli("connection", "delete", "uuid", argv[1])
        elif len(argv) == 3 and argv[0] == "autoconnect" and argv[2] in ("yes", "no"):
            require_wireguard(argv[1])
            nmcli("connection", "modify", "uuid", argv[1], "connection.autoconnect", argv[2])
        else:
            print("usage: wirezero-helper import NAME | up UUID | down UUID | delete UUID | "
                  "autoconnect UUID yes|no", file=sys.stderr)
            return 2
    except Fail as e:
        print(str(e), file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("NetworkManager did not answer in time", file=sys.stderr)
        return 1
    except OSError as e:
        print("System error: %s" % (e.strerror or e), file=sys.stderr)
        return 1
    return 0
