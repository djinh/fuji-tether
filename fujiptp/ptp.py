"""Minimal PTP (ISO 15740) over USB transport, built on pyusb."""
import struct
import threading

import usb.core
import usb.util

# Container types
CMD, DATA, RESP, EVENT = 1, 2, 3, 4

# Standard operation codes
OP_GetDeviceInfo = 0x1001
OP_OpenSession = 0x1002
OP_CloseSession = 0x1003
OP_GetStorageIDs = 0x1004
OP_GetStorageInfo = 0x1005
OP_GetNumObjects = 0x1006
OP_GetObjectHandles = 0x1007
OP_GetObjectInfo = 0x1008
OP_GetObject = 0x1009
OP_GetThumb = 0x100A
OP_DeleteObject = 0x100B
OP_InitiateCapture = 0x100E
OP_GetDevicePropDesc = 0x1014
OP_GetDevicePropValue = 0x1015
OP_SetDevicePropValue = 0x1016
OP_GetPartialObject = 0x101B

RC_OK = 0x2001
RC_SessionAlreadyOpen = 0x201E

RESPONSE_NAMES = {
    0x2001: "OK", 0x2002: "GeneralError", 0x2003: "SessionNotOpen",
    0x2005: "OperationNotSupported", 0x2006: "ParameterNotSupported",
    0x2009: "InvalidObjectHandle", 0x200A: "DevicePropNotSupported",
    0x2019: "DeviceBusy", 0x201C: "InvalidDevicePropFormat",
    0x201D: "InvalidDevicePropValue", 0x201E: "SessionAlreadyOpen",
}

# Datatype codes -> struct format
_SCALAR = {1: "b", 2: "B", 3: "h", 4: "H", 5: "i", 6: "I", 7: "q", 8: "Q"}
TYPE_STR = 0xFFFF


class PTPError(Exception):
    def __init__(self, op, code):
        self.op, self.code = op, code
        super().__init__(f"op 0x{op:04x} failed: 0x{code:04x} "
                         f"{RESPONSE_NAMES.get(code, '')}")


class Reader:
    """Little-endian cursor over a PTP data payload."""

    def __init__(self, buf, off=0):
        self.buf, self.off = buf, off

    def take(self, fmt):
        v = struct.unpack_from("<" + fmt, self.buf, self.off)
        self.off += struct.calcsize("<" + fmt)
        return v[0] if len(v) == 1 else v

    def string(self):
        n = self.take("B")
        s = self.buf[self.off:self.off + 2 * n].decode("utf-16le").rstrip("\0")
        self.off += 2 * n
        return s

    def array(self, fmt):
        n = self.take("I")
        return [self.take(fmt) for _ in range(n)]

    def value(self, dtype):
        if dtype == TYPE_STR:
            return self.string()
        if dtype in _SCALAR:
            return self.take(_SCALAR[dtype])
        if dtype & 0x4000 and (dtype & 0xFF) in _SCALAR:
            return self.array(_SCALAR[dtype & 0xFF])
        raise ValueError(f"unsupported PTP datatype 0x{dtype:04x}")


def pack_value(dtype, value):
    if dtype == TYPE_STR:
        if not value:
            return b"\0"
        enc = (value + "\0").encode("utf-16le")
        return struct.pack("<B", len(enc) // 2) + enc
    return struct.pack("<" + _SCALAR[dtype], value)


class PTPDevice:
    """One PTP session over USB. All transactions are serialized by a lock."""

    def __init__(self, vendor=None, product=None, timeout=10000):
        dev = None
        for d in usb.core.find(find_all=True):
            if vendor and d.idVendor != vendor:
                continue
            if product and d.idProduct != product:
                continue
            if any(i.bInterfaceClass == 6 for i in d.get_active_configuration()):
                dev = d
                break
        if dev is None:
            raise RuntimeError("no PTP camera found on USB")
        self.dev = dev
        self.timeout = timeout
        intf = next(i for i in dev.get_active_configuration()
                    if i.bInterfaceClass == 6)
        self.intf = intf.bInterfaceNumber
        try:
            if dev.is_kernel_driver_active(self.intf):
                dev.detach_kernel_driver(self.intf)
        except (NotImplementedError, usb.core.USBError):
            pass
        usb.util.claim_interface(dev, self.intf)
        eps = {(e.bEndpointAddress & 0x80, usb.util.endpoint_type(e.bmAttributes)): e
               for e in intf}
        self.ep_out = eps[(0x00, usb.util.ENDPOINT_TYPE_BULK)]
        self.ep_in = eps[(0x80, usb.util.ENDPOINT_TYPE_BULK)]
        self.ep_int = eps.get((0x80, usb.util.ENDPOINT_TYPE_INTR))
        self.tid = 0
        self.lock = threading.RLock()
        self.session = False
        self.recover()

    def recover(self):
        """Discard stale data left by an interrupted transfer.

        Note: the X-T3 breaks (EIO on next read) if sent the Still Image class
        'Device Reset' request, so a wedged camera needs usb_reset() instead."""
        while True:
            try:
                self.ep_in.read(1 << 20, 200)
            except usb.core.USBError:
                break
        self.session = False

    def usb_reset(self):
        """Port-reset the device. The object becomes unusable; reconnect after."""
        try:
            self.dev.reset()
        except usb.core.USBError:
            pass
        self.session = False

    # --- low level -------------------------------------------------------

    def _read_container(self):
        buf = bytes(self.ep_in.read(1 << 20, self.timeout))
        length, ctype, code, tid = struct.unpack_from("<IHHI", buf)
        chunks, got = [buf], len(buf)
        while got < length:
            # Large objects (55 MB RAFs) must be read in bounded chunks or
            # libusb fails with ENOMEM
            chunk = bytes(self.ep_in.read(min(length - got + 512, 1 << 20),
                                          self.timeout))
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        buf = b"".join(chunks)
        return ctype, code, tid, buf[12:length]

    def transaction(self, op, params=(), data_out=None):
        """Run one PTP transaction. Returns (response_params, data_in)."""
        with self.lock:
            self.tid += 1
            tid = self.tid
            p = b"".join(struct.pack("<I", x) for x in params)
            self.ep_out.write(struct.pack("<IHHI", 12 + len(p), CMD, op, tid) + p,
                              self.timeout)
            if data_out is not None:
                self.ep_out.write(struct.pack("<IHHI", 12 + len(data_out), DATA, op, tid)
                                  + data_out, self.timeout)
            data = b""
            while True:
                ctype, code, rtid, payload = self._read_container()
                if ctype == DATA:
                    data = payload
                elif ctype == RESP:
                    if code != RC_OK:
                        raise PTPError(op, code)
                    n = len(payload) // 4
                    return list(struct.unpack_from("<%dI" % n, payload)), data

    def read_event(self, timeout=100):
        """Poll the interrupt endpoint. Returns (code, params) or None."""
        if self.ep_int is None:
            return None
        try:
            buf = bytes(self.ep_int.read(64, timeout))
        except usb.core.USBTimeoutError:
            return None
        length, ctype, code, tid = struct.unpack_from("<IHHI", buf)
        n = (min(length, len(buf)) - 12) // 4
        return code, list(struct.unpack_from("<%dI" % n, buf, 12))

    # --- session ---------------------------------------------------------

    def open_session(self):
        try:
            self.transaction(OP_OpenSession, (1,))
        except PTPError as e:
            if e.code != RC_SessionAlreadyOpen:
                raise
        self.session = True

    def close(self):
        try:
            if self.session:
                self.transaction(OP_CloseSession)
        except Exception:
            pass
        self.session = False
        try:
            usb.util.release_interface(self.dev, self.intf)
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass

    # --- standard operations ---------------------------------------------

    def device_info(self):
        _, b = self.transaction(OP_GetDeviceInfo)
        r = Reader(b)
        info = {"std_version": r.take("H"), "vendor_ext_id": r.take("I"),
                "vendor_ext_version": r.take("H"), "vendor_ext": r.string(),
                "functional_mode": r.take("H"),
                "operations": r.array("H"), "events": r.array("H"),
                "properties": r.array("H"), "capture_formats": r.array("H"),
                "image_formats": r.array("H")}
        info["manufacturer"] = r.string()
        info["model"] = r.string()
        info["version"] = r.string()
        info["serial"] = r.string()
        return info

    def prop_desc(self, code):
        _, b = self.transaction(OP_GetDevicePropDesc, (code,))
        r = Reader(b)
        d = {"code": r.take("H")}
        d["type"] = r.take("H")
        d["writable"] = r.take("B") == 1
        d["default"] = r.value(d["type"])
        d["current"] = r.value(d["type"])
        # Some Fuji firmware truncates the descriptor before the form flag
        form = r.take("B") if r.off < len(b) else 0
        if form == 1:
            d["range"] = (r.value(d["type"]), r.value(d["type"]), r.value(d["type"]))
        elif form == 2:
            n = r.take("H")
            d["enum"] = [r.value(d["type"]) for _ in range(n)]
        return d

    def get_prop(self, code, dtype):
        _, b = self.transaction(OP_GetDevicePropValue, (code,))
        return Reader(b).value(dtype)

    def set_prop(self, code, dtype, value):
        self.transaction(OP_SetDevicePropValue, (code,), pack_value(dtype, value))

    def storage_ids(self):
        _, b = self.transaction(OP_GetStorageIDs)
        return Reader(b).array("I")

    def storage_info(self, sid):
        _, b = self.transaction(OP_GetStorageInfo, (sid,))
        r = Reader(b)
        return {"type": r.take("H"), "fs_type": r.take("H"), "access": r.take("H"),
                "capacity": r.take("Q"), "free_bytes": r.take("Q"),
                "free_images": r.take("I"), "description": r.string(),
                "label": r.string()}

    def object_handles(self, sid=0xFFFFFFFF, fmt=0, parent=0):
        _, b = self.transaction(OP_GetObjectHandles, (sid, fmt, parent))
        return Reader(b).array("I")

    def object_info(self, handle):
        _, b = self.transaction(OP_GetObjectInfo, (handle,))
        r = Reader(b)
        return {"handle": handle, "storage": r.take("I"), "format": r.take("H"),
                "protection": r.take("H"), "size": r.take("I"),
                "thumb_format": r.take("H"), "thumb_size": r.take("I"),
                "thumb_w": r.take("I"), "thumb_h": r.take("I"),
                "width": r.take("I"), "height": r.take("I"), "depth": r.take("I"),
                "parent": r.take("I"), "assoc_type": r.take("H"),
                "assoc_desc": r.take("I"), "seq": r.take("I"),
                "filename": r.string(), "captured": r.string(),
                "modified": r.string()}

    def get_object(self, handle):
        return self.transaction(OP_GetObject, (handle,))[1]

    def get_thumb(self, handle):
        return self.transaction(OP_GetThumb, (handle,))[1]

    def delete_object(self, handle):
        self.transaction(OP_DeleteObject, (handle, 0))

    def initiate_capture(self, sid=0, fmt=0):
        self.transaction(OP_InitiateCapture, (sid, fmt))
