#!/bin/sh
# Install nginx and run Fuji Remote as a boot-time service behind it.
# Usage: sudo deploy/install.sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "run with sudo: sudo $0" >&2
    exit 1
fi

APPDIR=$(cd "$(dirname "$0")/.." && pwd)
USER_NAME=${SUDO_USER:-$(stat -c %U "$APPDIR")}

if [ ! -x "$APPDIR/.venv/bin/python" ]; then
    echo "missing $APPDIR/.venv; create it first (see README)" >&2
    exit 1
fi

echo "== installing nginx"
apt-get update -q
apt-get install -y -q nginx

echo "== installing fuji-remote.service (runs as $USER_NAME from $APPDIR)"
sed -e "s|@USER@|$USER_NAME|g" -e "s|@APPDIR@|$APPDIR|g" \
    "$APPDIR/deploy/fuji-remote.service" > /etc/systemd/system/fuji-remote.service
systemctl daemon-reload
systemctl enable fuji-remote.service
systemctl restart fuji-remote.service

echo "== configuring nginx"
cp "$APPDIR/deploy/nginx-fuji-remote.conf" /etc/nginx/sites-available/fuji-remote
ln -sf /etc/nginx/sites-available/fuji-remote /etc/nginx/sites-enabled/fuji-remote
# Our site is the default server on port 80
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl reload-or-restart nginx

echo "== done: http://$(hostname -I | awk '{print $1}')/ or http://localhost/"
