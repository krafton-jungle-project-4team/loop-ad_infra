#!/usr/bin/env python3
"""Capture read-only local failure state before exact-project teardown."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from archive import ArchiveWorker, ClickHouseHttp, build_s3_client, iso_now, load_config


def list_keys(s3: Any, bucket: str) -> List[Dict[str, Any]]:
    objects: List[Dict[str, Any]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            objects.append(
                {
                    "key": str(item["Key"]),
                    "bytes": int(item["Size"]),
                    "etag": str(item.get("ETag", "")).strip('"'),
                }
            )
    return sorted(objects, key=lambda item: item["key"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    config = load_config(args.config, None)
    clickhouse = ClickHouseHttp(config.clickhouse_url)
    s3 = build_s3_client(config)
    worker = ArchiveWorker(config, clickhouse, s3)
    background = clickhouse.one(
        "SELECT\n"
        "  (SELECT count() FROM system.merges WHERE database = 'loopad' AND table = 'events') AS merges,\n"
        "  (SELECT count() FROM system.mutations WHERE database = 'loopad' AND table = 'events' AND is_done = 0) AS mutations"
    )
    source_relation = worker._source_relation()
    document = {
        "schemaVersion": "1.0",
        "capturedAt": iso_now(),
        "partition": config.partition,
        "source": worker._metrics(source_relation),
        "sourceSchema": worker._schema(source_relation),
        "activeMergesAtSnapshot": int(background["merges"]),
        "activeMutationsAtSnapshot": int(background["mutations"]),
        "commitExists": worker._head_exists(config.commit_key),
        "objects": list_keys(s3, config.bucket),
        "readOnly": True,
        "awsCalls": 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
