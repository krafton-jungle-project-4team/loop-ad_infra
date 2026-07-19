#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_INPUT="${1:?output path is required}"
mkdir -p "$(dirname "$OUTPUT_INPUT")"
OUTPUT="$(cd "$(dirname "$OUTPUT_INPUT")" && pwd)/$(basename "$OUTPUT_INPUT")"
(cd "$SCRIPT_DIR/reference-driver" && env GOMODCACHE="${GOMODCACHE:-/tmp/loopad-reference-go-mod-cache}" GOCACHE="${GOCACHE:-/tmp/loopad-reference-go-cache}" CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -buildvcs=false -ldflags='-s -w' -o "$OUTPUT" .)
sha256sum "$OUTPUT"
