"""QR code scanning: the IMX219 camera via Picamera2, decoding via libzbar.

zbar is called through ctypes so no extra Python package is needed, just the
system library (libzbar0t64 / libzbar0). The decoded text (which contains a
private key) only ever lives in memory.
"""

import ctypes
import ctypes.util
import threading
import time

# IMX219 1920x1080 mode = native-resolution centre crop of the sensor: about
# twice the detail per QR module of the 640x480 (binned) mode at the same
# distance. Dense WireGuard QR codes (~75x75 modules) need that.
FRAME_SIZE = (1920, 1080)
SENSOR_MODE = {"output_size": (1920, 1080), "bit_depth": 10}
ROI = 1080          # decode only the centre square inside the on-screen guide
MAX_PAYLOAD = 8192   # largest QR code holds ~3 KB; anything bigger is bogus
_Y800 = 0x30303859  # fourcc('Y','8','0','0'): 8-bit greyscale


class CameraError(Exception):
    pass


class Zbar:
    """Minimal libzbar binding: greyscale bytes in, list of QR payloads out."""

    def __init__(self):
        path = None
        for name in ("libzbar.so.0", ctypes.util.find_library("zbar")):
            if not name:
                continue
            try:
                lib = ctypes.CDLL(name)
                path = name
                break
            except OSError:
                continue
        if path is None:
            raise CameraError("QR decoder (libzbar) is not installed")
        vp, cp, ui = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint
        sig = {
            "zbar_image_scanner_create": (vp, []),
            "zbar_image_scanner_destroy": (None, [vp]),
            "zbar_image_scanner_set_config": (ctypes.c_int, [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int]),
            "zbar_image_create": (vp, []),
            "zbar_image_destroy": (None, [vp]),
            "zbar_image_set_format": (None, [vp, ctypes.c_ulong]),
            "zbar_image_set_size": (None, [vp, ui, ui]),
            "zbar_image_set_data": (None, [vp, vp, ctypes.c_ulong, vp]),
            "zbar_scan_image": (ctypes.c_int, [vp, vp]),
            "zbar_image_first_symbol": (vp, [vp]),
            "zbar_symbol_next": (vp, [vp]),
            "zbar_symbol_get_data": (vp, [vp]),   # void*: data may contain NULs
            "zbar_symbol_get_data_length": (ui, [vp]),
            "zbar_symbol_get_type": (ctypes.c_int, [vp]),
        }
        for fn, (res, args) in sig.items():
            f = getattr(lib, fn)
            f.restype, f.argtypes = res, args
        self.lib = lib
        self.scanner = lib.zbar_image_scanner_create()
        # Only look for QR codes (ZBAR_NONE=0 cfg ENABLE=0 off, then QRCODE=64 on).
        lib.zbar_image_scanner_set_config(self.scanner, 0, 0, 0)
        lib.zbar_image_scanner_set_config(self.scanner, 64, 0, 1)

    def decode(self, grey, width, height):
        lib = self.lib
        buf = ctypes.create_string_buffer(bytes(grey), width * height)
        img = lib.zbar_image_create()
        try:
            lib.zbar_image_set_format(img, _Y800)
            lib.zbar_image_set_size(img, width, height)
            lib.zbar_image_set_data(img, ctypes.cast(buf, ctypes.c_void_p), width * height, None)
            out = []
            if lib.zbar_scan_image(self.scanner, img) > 0:
                sym = lib.zbar_image_first_symbol(img)
                while sym:
                    n = lib.zbar_symbol_get_data_length(sym)
                    ptr = lib.zbar_symbol_get_data(sym)
                    if ptr and 0 < n <= MAX_PAYLOAD:
                        out.append(ctypes.string_at(ptr, n))
                    sym = lib.zbar_symbol_next(sym)
            return out
        finally:
            lib.zbar_image_destroy(img)
            ctypes.memset(buf, 0, width * height)

    def close(self):
        if self.scanner:
            self.lib.zbar_image_scanner_destroy(self.scanner)
            self.scanner = None


def decode_text(payloads):
    """First payload that decodes as UTF-8 text, else None."""
    for p in payloads:
        try:
            return p.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


class PicameraSource:
    """Greyscale frames from the onboard camera (Y plane of YUV420)."""

    def __init__(self):
        try:
            from libcamera import Transform
            from picamera2 import Picamera2
        except ImportError:
            raise CameraError("Camera support (python3-picamera2) is not installed")
        try:
            infos = Picamera2.global_camera_info()
        except Exception:
            infos = []
        if not infos:
            raise CameraError("No camera found")
        self.cam = Picamera2()
        try:
            flipped = self.cam.camera_properties.get("Rotation", 0) == 180
            cfg = self.cam.create_video_configuration(
                main={"size": FRAME_SIZE, "format": "YUV420"},
                sensor=SENSOR_MODE,
                buffer_count=3,
                controls={"FrameRate": 15, "Sharpness": 1.5, "Contrast": 1.3},
                transform=Transform(hflip=flipped, vflip=flipped))
            self.cam.configure(cfg)
            self.cam.start()
        except Exception as e:
            self.close()
            raise CameraError("Camera failed to start: %s" % e)

    def grab(self):
        """Return the centre square of the Y plane as (grey_bytes, side, side)."""
        arr = self.cam.capture_array("main")
        w, h = FRAME_SIZE
        x0 = (w - ROI) // 2
        y0 = (h - ROI) // 2
        return arr[y0:y0 + ROI, x0:x0 + ROI].tobytes(), ROI, ROI

    def close(self):
        try:
            self.cam.stop()
        except Exception:
            pass
        try:
            self.cam.close()
        except Exception:
            pass


class Scanner:
    """Background thread: grab frames, keep the latest for preview, decode."""

    def __init__(self, source_factory=PicameraSource, decoder_factory=Zbar, accept=None):
        self._accept = accept
        self.rejected = False
        self._source_factory = source_factory
        self._decoder_factory = decoder_factory
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.frame = None          # (grey bytes, w, h) for preview
        self.result = None         # decoded text
        self.error = None
        self.ready = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        src = dec = None
        try:
            dec = self._decoder_factory()
            src = self._source_factory()
            self.ready = True
            while not self._stop.is_set():
                grey, w, h = src.grab()
                with self._lock:
                    self.frame = (grey, w, h)
                text = decode_text(dec.decode(grey, w, h))
                if text is not None and self._accept and not self._accept(text):
                    self.rejected = True      # some other QR code: keep scanning
                    text = None
                    time.sleep(1.0)
                if text is not None:
                    with self._lock:
                        self.result = text
                    break
                time.sleep(0.01)
        except CameraError as e:
            self.error = str(e)
        except Exception as e:  # camera stack errors must not crash the app
            self.error = "Camera error: %s" % e
        finally:
            if src:
                src.close()
            if dec:
                dec.close()
            with self._lock:
                self.frame = None

    def latest(self):
        with self._lock:
            return self.frame

    def take_result(self):
        with self._lock:
            r, self.result = self.result, None
            return r

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)


def camera_present():
    """Cheap check used to dim the scan option on models without a camera."""
    import glob
    return bool(glob.glob("/dev/video*") or glob.glob("/dev/media*"))
