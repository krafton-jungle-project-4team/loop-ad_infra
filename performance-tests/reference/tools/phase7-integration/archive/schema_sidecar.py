#!/usr/bin/env python3
"""Initialize the Phase 4 ClickHouse schema, then stay alive as a task guard."""

from __future__ import annotations

import base64
import os
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path


def execute(query: str) -> None:
    credentials = base64.b64encode(
        f"{os.environ['CLICKHOUSE_USER']}:{os.environ['CLICKHOUSE_PASSWORD']}".encode()
    ).decode()
    request = urllib.request.Request(
        os.environ.get("CLICKHOUSE_HTTP_URL", "http://127.0.0.1:8123"),
        data=query.encode("utf-8"),
        headers={"Authorization": f"Basic {credentials}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def main() -> int:
    deadline = time.monotonic() + 180
    while True:
        try:
            execute("SELECT 1")
            break
        except (OSError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
    schema = Path("/opt/loopad/archive/phase4-schema.sql").read_text(encoding="utf-8")
    for statement in schema.split(";"):
        if statement.strip():
            execute(statement.strip())
    Path("/run/loopad/schema-ready").write_text("ready\n", encoding="utf-8")
    stopped = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopped:
        time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
