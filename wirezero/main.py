"""WireZero entry point.

    python3 -m wirezero           # on the CardputerZero
    python3 -m wirezero --sim     # desktop: fake tunnels, renders screenshots
"""

import argparse
import os
import signal
import sys

from . import __version__


def run_device():
    from .display import FramebufferDisplay
    from .keys import EvdevKeys
    from .nm import NMBackend
    from .ui import App

    try:
        display = FramebufferDisplay()
    except Exception as e:
        print("wirezero: can't open the framebuffer: %s" % e, file=sys.stderr)
        return 2
    try:
        keys = EvdevKeys()
    except ImportError:
        print("wirezero: python3-evdev is missing", file=sys.stderr)
        display.close()
        return 2
    app = App(NMBackend(), display, keys)

    def stop(*_):
        app.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        app.run()
    finally:
        keys.close()
        display.close()
    return 0


def run_sim(outdir):
    """Drive the UI with scripted key presses and save each screen as a PNG."""
    from .display import PNGDisplay
    from .nm import FakeBackend
    from .ui import App

    class NoKeys:
        def poll(self, timeout):
            return []

    class SimScanner:
        """Shows a fake camera frame with a QR code in it; never decodes."""

        def __init__(self):
            from PIL import Image, ImageDraw
            frame = Image.new("L", (1080, 1080), 120)
            qr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", "tools", "sim-qr.png")
            if os.path.exists(qr_path):           # repo checkout: use the sample QR
                qr = Image.open(qr_path).convert("L").resize((760, 760), Image.NEAREST)
                ImageDraw.Draw(frame).rectangle((140, 140, 940, 940), fill=245)
                frame.paste(qr, (160, 160))
            self.frame = (frame.tobytes(), 1080, 1080)
            self.error = None
            self.result = None

        def latest(self):
            return self.frame

        def take_result(self):
            return None

        def stop(self):
            pass

    os.makedirs(outdir, exist_ok=True)
    nm = FakeBackend()
    disp = PNGDisplay()
    confs = ["/home/pi/wireguard/holiday.conf", "/boot/firmware/work-vpn.conf"]
    app = App(nm, disp, NoKeys(), conf_finder=lambda: list(confs),
              scanner_factory=SimScanner, camera_check=lambda: True)
    app.start()

    def settle():
        for _ in range(50):
            app.tick([])
            if not app.busy:
                break
            import time
            time.sleep(0.01)
        app.toast = None
        app.dirty = True
        app.tick([])

    def shot(name, *keys):
        app.tick(list(keys))
        settle()
        disp.save(os.path.join(outdir, name + ".png"))
        disp.save(os.path.join(outdir, name + "@3x.png"), scale=3)

    shot("01-list")
    shot("02-details", "k6")
    shot("03-import", "esc", "k5")
    app.tick(["enter"])
    settle()
    disp.save(os.path.join(outdir, "04-remove-original@3x.png"), scale=3)
    app.tick(["esc", "down", "down"])
    shot("05-list-more")
    shot("06-confirm", "k8")
    shot("07-help", "esc", "help")
    app.tick(["esc", "k5", "k6"])
    shot("09-scan")
    app.stop_scan()
    app.pending_conf = "[Interface]"
    app.name_text = "home"
    app.screen = "name"
    shot("10-name")
    app.cancel_name()
    app.screen = "list"
    nm.tunnels.clear()
    app.refresh(force=True)
    shot("08-empty")
    print("Saved screenshots to %s" % outdir)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="wirezero", description="WireGuard tunnels for CardputerZero")
    p.add_argument("--sim", metavar="DIR", nargs="?", const="screenshots",
                   help="render the UI with fake tunnels into DIR (default: screenshots)")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = p.parse_args(argv)
    if args.sim:
        return run_sim(args.sim)
    return run_device()


if __name__ == "__main__":
    sys.exit(main())
