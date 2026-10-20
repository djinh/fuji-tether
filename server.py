"""Web remote for a Fujifilm camera tethered over USB.

Run:  .venv/bin/python server.py [--host 127.0.0.1] [--port 8080] [--dev]
"""
import argparse
import io
import os
import struct
import threading
import time
from datetime import datetime

from flask import Flask, Response, abort, jsonify, request, send_file
from PIL import Image

from fujiptp.fuji import CameraError, FujiCamera

ROOT = os.path.dirname(os.path.abspath(__file__))
CAPTURES = os.path.join(ROOT, "captures")
THUMBS = os.path.join(CAPTURES, ".thumbs")
PHOTO_EXT = (".jpg", ".jpeg", ".raf")

app = Flask(__name__, static_folder=os.path.join(ROOT, "static"), static_url_path="/static")
camera = FujiCamera()


class LiveView:
    """Grabs frames on one background thread while anyone is watching, so any
    number of browsers can share a single camera stream."""

    IDLE_STOP = 3.0  # seconds without viewers before live view is closed

    def __init__(self, cam):
        self.cam = cam
        self.cond = threading.Condition()
        self.frame, self.seq, self.error = None, 0, None
        self.last_viewer = 0.0
        self.thread = None

    def _run(self):
        while time.time() - self.last_viewer < self.IDLE_STOP:
            try:
                frame = self.cam.liveview_frame()
                err = None
            except Exception as e:  # never let the grabber thread die
                frame, err = None, str(e)
                time.sleep(1)
            with self.cond:
                self.error = err
                if frame:
                    self.frame, self.seq = frame, self.seq + 1
                self.cond.notify_all()
        try:
            self.cam.stop_liveview()
        except Exception:
            pass
        with self.cond:
            self.thread = None

    def frames(self):
        seq = None
        while True:
            with self.cond:
                self.last_viewer = time.time()
                if self.thread is None:
                    self.frame = None  # don't replay a stale frame from last time
                    self.thread = threading.Thread(target=self._run, daemon=True)
                    self.thread.start()
                self.cond.wait_for(lambda: self.frame is not None and self.seq != seq,
                                   timeout=1.0)
                if self.frame is None or self.seq == seq:
                    continue  # starting up, paused for a capture, or camera gone
                seq, frame = self.seq, self.frame
            yield frame


liveview = LiveView(camera)


def error(msg, code=500):
    return jsonify({"error": msg}), code


@app.errorhandler(CameraError)
def camera_error(e):
    return error(str(e), 503)


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/status")
def status():
    try:
        return jsonify({"connected": True, **camera.status(),
                        "settings": camera.settings()})
    except CameraError as e:
        return jsonify({"connected": False, "error": str(e)})


@app.post("/api/settings/<name>")
def set_setting(name):
    value = (request.get_json(silent=True) or {}).get("value")
    if value is None:
        return error("missing value", 400)
    camera.set(name, value)
    return jsonify({"settings": camera.settings()})


@app.post("/api/capture")
def capture():
    body = request.get_json(silent=True) or {}
    bulb = body.get("bulb_seconds")
    if bulb is not None:
        bulb = float(bulb)
        if not 0 < bulb <= 3600:
            return error("bulb exposure must be between 0 and 3600 s", 400)
    files = camera.capture(bulb_seconds=bulb)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = []
    for name, data in files:
        fname = f"{stamp}_{name}"
        with open(os.path.join(CAPTURES, fname), "wb") as f:
            f.write(data)
        saved.append(photo_entry(fname))
    return jsonify({"photos": saved})


@app.get("/api/liveview.mjpg")
def liveview_stream():
    def gen():
        for frame in liveview.frames():
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store"})


# --- gallery ---------------------------------------------------------------

def photo_path(name):
    if os.sep in name or name.startswith(".") or not name.lower().endswith(PHOTO_EXT):
        abort(404)
    path = os.path.join(CAPTURES, name)
    if not os.path.isfile(path):
        abort(404)
    return path


def photo_entry(name):
    st = os.stat(os.path.join(CAPTURES, name))
    return {"name": name, "size": st.st_size, "time": st.st_mtime,
            "raw": name.lower().endswith(".raf")}


def preview_jpeg(path):
    """JPEG bytes for a photo: RAFs carry a full-size JPEG at the offset in
    their header (bytes 84..91, big-endian offset and length)."""
    with open(path, "rb") as f:
        if not path.lower().endswith(".raf"):
            return f.read()
        head = f.read(92)
        if not head.startswith(b"FUJIFILMCCD-RAW"):
            abort(415)
        off, length = struct.unpack_from(">II", head, 84)
        f.seek(off)
        return f.read(length)


@app.get("/api/photos")
def photos():
    names = [n for n in os.listdir(CAPTURES)
             if n.lower().endswith(PHOTO_EXT) and not n.startswith(".")]
    return jsonify({"photos": sorted((photo_entry(n) for n in names),
                                     key=lambda p: p["name"], reverse=True)})


@app.delete("/api/photos/<name>")
def delete_photo(name):
    os.remove(photo_path(name))
    thumb = os.path.join(THUMBS, name + ".jpg")
    if os.path.exists(thumb):
        os.remove(thumb)
    return jsonify({"ok": True})


@app.get("/photos/<name>")
def download(name):
    return send_file(photo_path(name), as_attachment=True)


@app.get("/preview/<name>")
def preview(name):
    return send_file(io.BytesIO(preview_jpeg(photo_path(name))), mimetype="image/jpeg",
                     max_age=86400)


@app.get("/thumbs/<name>")
def thumb(name):
    path = photo_path(name)
    cached = os.path.join(THUMBS, name + ".jpg")
    if not os.path.exists(cached):
        img = Image.open(io.BytesIO(preview_jpeg(path)))
        img.draft("RGB", (480, 480))  # cheap DCT downscale while decoding
        img = img.convert("RGB")
        img.thumbnail((480, 480))
        img.save(cached, "JPEG", quality=80)
    return send_file(cached, mimetype="image/jpeg", max_age=86400)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 behind nginx, 0.0.0.0 to serve the LAN directly")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--dev", action="store_true", help="use Flask's dev server")
    args = ap.parse_args()
    os.makedirs(THUMBS, exist_ok=True)
    try:
        camera.connect()
        print(f"Connected to {camera.info['manufacturer']} {camera.info['model']}")
    except CameraError as e:
        print(f"Camera not available yet ({e}); will retry on first request")
    try:
        if args.dev:
            app.run(host=args.host, port=args.port, threaded=True)
        else:
            from waitress import serve
            # Each live view viewer holds a thread for as long as it watches
            serve(app, host=args.host, port=args.port, threads=16,
                  channel_timeout=3700)  # long bulb exposures
    finally:
        camera.disconnect()


if __name__ == "__main__":
    main()
