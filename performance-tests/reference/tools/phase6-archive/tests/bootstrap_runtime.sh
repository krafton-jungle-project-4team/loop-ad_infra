#!/usr/bin/env bash
set -euo pipefail

umask 077
id loopad-archive >/dev/null 2>&1 || useradd --system --home-dir /opt/loopad/phase6 --shell /usr/sbin/nologin loopad-archive
install -d -o root -g root -m 0711 /opt/loopad /etc/loopad
install -d -o loopad-archive -g loopad-archive -m 0700 /opt/loopad/phase6 /etc/loopad/phase6
python3 -m venv /opt/loopad/phase6/venv
printf '%s\n' 'print("bootstrap-runtime-ok")' > /opt/loopad/phase6/probe.py
printf '%s\n' '{"test":true}' > /etc/loopad/phase6/archive.json
printf '%s\n' '<clickhouse/>' > /opt/loopad/phase6/memory.xml
chown -R loopad-archive:loopad-archive /opt/loopad/phase6 /etc/loopad/phase6

test "$(stat -c '%a' /opt/loopad)" = 711
test "$(stat -c '%a' /etc/loopad)" = 711
test "$(stat -c '%a' /opt/loopad/phase6)" = 700
test "$(stat -c '%a' /etc/loopad/phase6)" = 700
runuser -u loopad-archive -- /opt/loopad/phase6/venv/bin/python /opt/loopad/phase6/probe.py | grep -qx bootstrap-runtime-ok
runuser -u loopad-archive -- test -r /etc/loopad/phase6/archive.json
runuser -u loopad-archive -- test -r /opt/loopad/phase6/memory.xml
