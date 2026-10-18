"""Fujifilm X-series tethering on top of the generic PTP transport.

Protocol notes (verified on an X-T3, fw 5.11, "USB tether shooting" mode):
- Storage 0x10000001 "Still" receives captured images; 0x10000002 "Live"
  holds the current live view JPEG. The SD card itself is not exposed.
- 0xD207 must be 2 (PC priority) to control exposure; aperture (0x5007),
  shutter (0x500D) and exposure comp (0x5010) only answer after that.
  While live view is open the camera rejects SetDevicePropValue
  (DeviceBusy) and InitiateCapture (GeneralError), so both pause it.
- Capture: 0xD208=0x0200 + InitiateCapture (half press), poll 0xD209 until
  != 1, then 0xD208=0x0304 + InitiateCapture. With the shutter dial on B the
  release is 0xD208=0x0500 (open) ... 0xD208=0x000C (close) instead.
- Live view: InitiateOpenCapture (0x101C) then GetObject on the Live storage
  handle; the frame only advances after DeleteObject. TerminateOpenCapture
  (0x1018) ends it. DeviceBusy from 0x101C means it is already running.
- Never send the class Device Reset request: it wedges the X-T3 (EIO).
"""
import subprocess
import threading
import time

import usb.core

from .ptp import PTPDevice, PTPError

FUJI_VENDOR = 0x04CB
STILL, LIVE = 0x10000001, 0x10000002

OP_InitiateOpenCapture = 0x101C
OP_TerminateOpenCapture = 0x1018

DPC_Priority = 0xD207
DPC_CaptureControl = 0xD208
DPC_AFStatus = 0xD209

CC_HalfPress = 0x0200
CC_Release = 0x0304
CC_BulbOpen = 0x0500
CC_BulbClose = 0x000C

SHUTTER_BULB = 0xFFFFFFFF

U16, I16, U32, I32, STR = 4, 3, 6, 5, 0xFFFF


def _shutter(us):
    if us == SHUTTER_BULB:
        return "Bulb"
    if us >= 1_000_000:
        s = us / 1_000_000
        return f"{s:g}s" if s >= 10 else f"{s:.1f}s".replace(".0s", "s")
    return f"1/{round(1_000_000 / us)}"


def _ev(v):
    return "0" if v == 0 else f"{v / 1000:+.1f}".replace(".0", "") + " EV"


def _table(t):
    return lambda v: t.get(v, f"0x{v:x}")


FILM_SIMS = {1: "Provia / Standard", 2: "Velvia / Vivid", 3: "Astia / Soft",
             4: "Pro Neg. Hi", 5: "Pro Neg. Std", 6: "Monochrome",
             7: "Monochrome + Ye", 8: "Monochrome + R", 9: "Monochrome + G",
             10: "Sepia", 11: "Classic Chrome", 12: "Acros", 13: "Acros + Ye",
             14: "Acros + R", 15: "Acros + G", 16: "Eterna"}
WHITE_BALANCE = {2: "Auto", 4: "Daylight", 6: "Incandescent",
                 0x8001: "Fluorescent 1", 0x8002: "Fluorescent 2",
                 0x8003: "Fluorescent 3", 0x8006: "Shade"}
QUALITY = {1: "RAW", 2: "Fine", 3: "Normal", 4: "RAW + Fine", 5: "RAW + Normal"}
FOCUS_MODE = {1: "Manual", 0x8001: "AF-S", 0x8002: "AF-C"}
PROGRAM = {1: "Manual", 2: "Program", 3: "Aperture priority",
           4: "Shutter priority", 6: "Program"}

# name -> (code, datatype, label, value formatter)
SETTINGS = {
    "shutter": (0x500D, U32, "Shutter", _shutter),
    "aperture": (0x5007, U16, "Aperture", lambda v: f"f/{v / 100:g}"),
    "iso": (0x500F, I32, "ISO", lambda v: f"Auto {-v}" if v < 0 else str(v)),
    "exposure_comp": (0x5010, I16, "Exp. comp.", _ev),
    "film_sim": (0xD001, U16, "Film simulation", _table(FILM_SIMS)),
    "white_balance": (0x5005, U16, "White balance", _table(WHITE_BALANCE)),
    "color_temp": (0xD017, U16, "Color temp.", lambda v: f"{v} K"),
    "dynamic_range": (0xD007, U16, "Dynamic range",
                      lambda v: "Auto" if v == 0xFFFF else f"DR{v}"),
    "quality": (0xD018, U16, "Quality", _table(QUALITY)),
    "image_size": (0x5003, STR, "Image size", str),
    "focus_mode": (0x500A, U16, "Focus mode", _table(FOCUS_MODE)),
    "exposure_program": (0x500E, U16, "Mode", _table(PROGRAM)),
}
# Set by the physical dials, so only shown
READ_ONLY = {"exposure_program", "focus_mode"}


class CameraError(Exception):
    pass


def release_from_gvfs():
    """The desktop's gvfs gphoto2 monitor auto-mounts the camera and holds
    the USB interface. Unmount it so we can claim the device."""
    try:
        out = subprocess.run(["gio", "mount", "-l"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    roots = {w for line in out.splitlines() for w in line.split()
             if w.startswith("gphoto2://") and w.endswith("/")}
    for uri in roots:
        subprocess.run(["gio", "mount", "-u", uri], capture_output=True, timeout=10)
    return bool(roots)


class FujiCamera:
    """Thread-safe Fuji camera. Every public method takes self.lock."""

    def __init__(self):
        self.ptp = None
        self.lock = threading.RLock()
        self.info = None
        self.liveview = False

    # --- connection ------------------------------------------------------

    @property
    def connected(self):
        return self.ptp is not None

    def connect(self):
        with self.lock:
            if self.ptp:
                return
            last = None
            for attempt in range(3):
                try:
                    self._open()
                    return
                except usb.core.USBError as e:
                    last = e
                    self._drop()
                    if e.errno == 16:  # EBUSY: someone else holds it
                        release_from_gvfs()
                    else:  # wedged pipe: port reset and re-enumerate
                        dev = usb.core.find(idVendor=FUJI_VENDOR)
                        if dev is not None:
                            try:
                                dev.reset()
                            except usb.core.USBError:
                                pass
                    time.sleep(1.5)
                except RuntimeError as e:  # not plugged in
                    raise CameraError(str(e))
            raise CameraError(f"could not connect to camera: {last}")

    def _open(self):
        self.ptp = PTPDevice(vendor=FUJI_VENDOR)
        self.ptp.open_session()
        self.info = self.ptp.device_info()
        self.liveview = False
        # A previous run may have left live view open, which blocks the
        # priority change
        self._try(OP_TerminateOpenCapture, (0,))
        self.ptp.set_prop(DPC_Priority, U16, 2)

    def _drop(self):
        if self.ptp:
            try:
                self.ptp.close()
            except Exception:
                pass
        self.ptp = None
        self.liveview = False

    def disconnect(self):
        with self.lock:
            if self.ptp and self.liveview:
                self._try(OP_TerminateOpenCapture, (0,))
            self._drop()

    def _try(self, op, params=()):
        try:
            self.ptp.transaction(op, params)
            return True
        except PTPError:
            return False

    def _call(self, fn):
        """Run fn with a connected camera. USB failures drop the connection
        so the next call reconnects (with a port reset if needed)."""
        with self.lock:
            self.connect()
            try:
                return fn(self.ptp)
            except usb.core.USBError as e:
                self._drop()
                raise CameraError(f"USB error: {e}") from e

    # --- settings --------------------------------------------------------

    def settings(self):
        def read(ptp):
            out = []
            for name, (code, _, label, fmt) in SETTINGS.items():
                try:
                    d = ptp.prop_desc(code)
                except PTPError:
                    continue
                opts = d.get("enum")
                if opts is None and "range" in d:
                    lo, hi, step = d["range"]
                    opts = list(range(lo, hi + 1, step or 1))
                writable = d["writable"] and name not in READ_ONLY
                if name == "shutter" and opts:
                    # Drop odd long-exposure codes that sit just above 64 s
                    opts = [v for v in opts if v % 1000 == 0 or v < 64_000_000
                            or v == SHUTTER_BULB]
                out.append({"name": name, "label": label, "value": d["current"],
                            "display": fmt(d["current"]), "writable": writable,
                            "options": [{"value": v, "display": fmt(v)}
                                        for v in (opts or [])]})
            return out
        return self._call(read)

    def set(self, name, value):
        if name not in SETTINGS or name in READ_ONLY:
            raise CameraError(f"unknown or read-only setting {name!r}")
        code, dtype, label, fmt = SETTINGS[name]

        def write(ptp):
            self._pause_liveview(ptp)
            try:
                ptp.set_prop(code, dtype, value)
            except PTPError as e:
                raise CameraError(f"camera refused {label} = {fmt(value)} "
                                  f"(check the physical dial/ring): {e}")
            return ptp.get_prop(code, dtype)
        return self._call(write)

    def is_bulb(self):
        return self._call(lambda p: p.get_prop(0x500D, U32) == SHUTTER_BULB)

    def status(self):
        def read(ptp):
            return {"model": self.info["model"], "firmware": self.info["version"],
                    "serial": self.info["serial"], "liveview": self.liveview,
                    "bulb": ptp.get_prop(0x500D, U32) == SHUTTER_BULB}
        return self._call(read)

    # --- live view -------------------------------------------------------

    def start_liveview(self):
        def start(ptp):
            if self.liveview:
                return
            for _ in range(10):
                try:
                    ptp.transaction(OP_InitiateOpenCapture, (0, 0))
                    break
                except PTPError as e:
                    if e.code != 0x2019:
                        raise CameraError(f"live view failed: {e}")
                    # Busy: already running from an earlier session
                    if ptp.object_handles(LIVE):
                        break
                    time.sleep(0.2)
            self.liveview = True
        self._call(start)

    def _pause_liveview(self, ptp):
        """Close live view; the next liveview_frame() reopens it."""
        if self.liveview:
            self._try(OP_TerminateOpenCapture, (0,))
            self.liveview = False

    def stop_liveview(self):
        if self.connected:
            self._call(self._pause_liveview)

    def liveview_frame(self, timeout=2.0):
        """Return the next live view JPEG, or None if none arrived in time."""
        def grab(ptp):
            handles = ptp.object_handles(LIVE)
            if not handles:
                return None
            jpeg = ptp.get_object(handles[0])
            ptp.delete_object(handles[0])  # makes the camera produce the next
            return jpeg
        if not self.liveview:
            self.start_liveview()
        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = self._call(grab)
            if frame:
                return frame
            time.sleep(0.01)
        return None

    # --- capture ---------------------------------------------------------

    def capture(self, bulb_seconds=None):
        """Take a picture. Returns [(filename, bytes)] (RAW+JPEG gives two).
        The camera lock is held throughout so live view pauses meanwhile."""
        with self.lock:
            def shoot(ptp):
                self._pause_liveview(ptp)
                # Leftovers from a crashed run would be mistaken for this shot
                for h in ptp.object_handles(STILL):
                    ptp.delete_object(h)
                bulb = ptp.get_prop(0x500D, U32) == SHUTTER_BULB
                if bulb and not bulb_seconds:
                    raise CameraError("shutter dial is on B: give an exposure time")
                ptp.set_prop(DPC_CaptureControl, U16, CC_HalfPress)
                ptp.initiate_capture()
                deadline = time.time() + 5
                while ptp.get_prop(DPC_AFStatus, U16) == 1 and time.time() < deadline:
                    time.sleep(0.05)
                try:
                    if bulb:
                        ptp.set_prop(DPC_CaptureControl, U16, CC_BulbOpen)
                        ptp.initiate_capture()
                        time.sleep(bulb_seconds)
                        ptp.set_prop(DPC_CaptureControl, U16, CC_BulbClose)
                        ptp.initiate_capture()
                    else:
                        ptp.set_prop(DPC_CaptureControl, U16, CC_Release)
                        ptp.initiate_capture()
                except PTPError as e:
                    raise CameraError(f"camera refused to release the shutter: {e}")
                return self._collect(ptp, 60 + (bulb_seconds or 0))
            return self._call(shoot)

    def _collect(self, ptp, timeout):
        deadline = time.time() + timeout
        handles = []
        while not handles:
            if time.time() > deadline:
                raise CameraError("no image arrived from the camera")
            time.sleep(0.05)
            handles = ptp.object_handles(STILL)
        # With RAW+JPEG the second file shows up shortly after the first
        settle = time.time() + 1.5
        while time.time() < settle:
            time.sleep(0.1)
            more = ptp.object_handles(STILL)
            if len(more) > len(handles):
                handles, settle = more, time.time() + 1.5
        files = []
        for h in handles:
            info = ptp.object_info(h)
            files.append((info["filename"], ptp.get_object(h)))
            ptp.delete_object(h)
        return files
