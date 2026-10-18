# Fuji Remote

Browser remote control for a Fujifilm X-series camera tethered to this Pi over
USB, using our own PTP implementation (`fujiptp/`). Tested with an X-T3 on
firmware 5.11.

- Live view (MJPEG, ~50 fps on the X-T3)
- Shutter release, including timed bulb exposures when the shutter dial is on B
- Exposure and image settings: shutter, aperture, ISO, exposure compensation,
  film simulation, white balance, colour temperature, dynamic range, quality,
  image size
- Every capture is downloaded to `captures/` on the Pi and shown in a gallery
  with thumbnails (RAF thumbnails come from the JPEG embedded in the RAW)

## Camera setup

1. Set *Connection setting → PC connection mode* to **USB TETHER SHOOTING
   AUTO** (or FIXED). In tether mode the camera does not expose the SD card
   contents over USB, so the gallery shows only what this app captured.
2. Aperture ring and shutter dial on **A** let the app set aperture and
   shutter speed; otherwise the physical dial wins and the camera refuses the
   change.
3. With the shutter dial on **B**, the shutter button takes a timed bulb
   exposure of the length entered in the UI.

## Running

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo deploy/install.sh                # http://<pi-address>/
```

`install.sh` installs nginx, serves the app on port 80 through it
(`deploy/nginx-fuji-remote.conf`), and runs the app as the
`fuji-remote` systemd service (waitress on 127.0.0.1:8080) at boot:

```sh
systemctl status fuji-remote          # is it running?
journalctl -u fuji-remote -f          # logs
sudo systemctl restart fuji-remote    # after changing the code
```

For development, stop the service and run `.venv/bin/python server.py --dev`.

Your user must be in the `plugdev` group (no root needed for USB access).

The desktop's gvfs auto-mounter grabs the camera whenever it is plugged in or
switched on. It is disabled on this Pi with
`systemctl --user mask --now gvfs-gphoto2-volume-monitor.service`
(undo with `unmask`); the app also unmounts it on connect as a fallback.

There is no authentication: anyone who can reach port 80 on your network can
control the camera and delete captures.

## Layout

- `fujiptp/ptp.py`: generic PTP-over-USB transport (pyusb)
- `fujiptp/fuji.py`: Fuji tether protocol; the module docstring documents the
  quirks found on the X-T3
- `server.py`: Flask web server
- `static/index.html`: the UI
- `deploy/`: systemd unit, nginx site and install script
- `tools/dump.py`: dumps every property the camera reports (protocol
  discovery)
