"""Dump everything the camera reports over PTP (for protocol discovery)."""
import sys
sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from fujiptp.ptp import PTPDevice, PTPError

cam = PTPDevice(vendor=0x04CB)
cam.open_session()
info = cam.device_info()
print(f"{info['manufacturer']} {info['model']} fw {info['version']} mode {info['functional_mode']}")
print("capture formats", [hex(x) for x in info["capture_formats"]])
for code in info["properties"]:
    try:
        d = cam.prop_desc(code)
    except Exception as e:
        print(f"{code:#06x} ERR {e}")
        continue
    extra = ""
    if "enum" in d:
        e = d["enum"]
        extra = f"enum[{len(e)}]=" + ",".join(hex(v) if isinstance(v, int) else repr(v) for v in e[:40])
    elif "range" in d:
        extra = "range=" + str(d["range"])
    cur = hex(d["current"]) if isinstance(d["current"], int) else repr(d["current"])
    print(f"{code:#06x} t={d['type']:#06x} {'rw' if d['writable'] else 'ro'} cur={cur} {extra}")
try:
    for sid in cam.storage_ids():
        print("storage", hex(sid), cam.storage_info(sid))
    hs = cam.object_handles()
    print("objects", len(hs), [hex(h) for h in hs[:10]])
    if hs:
        print(cam.object_info(hs[-1]))
except PTPError as e:
    print("storage ERR", e)
cam.close()
