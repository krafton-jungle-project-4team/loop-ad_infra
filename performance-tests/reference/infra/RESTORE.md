# Restore the performance infrastructure reference

Use this procedure to reconstruct the captured source in a disposable directory.
Do not restore over an active checkout.

## Prerequisites

- A verified copy of `snapshot_20260719T200907Z` when full history or omitted
  intermediate files are required
- Node.js and npm versions compatible with the captured `package-lock.json`
- `jq` and `shasum`
- Enough local disk space for `npm ci` and CDK output

AWS credentials are not required for file restoration or local compilation. This
procedure does not include deploy, destroy, cleanup, or load commands.

## Restore files

Set `REFERENCE` to this `infra` directory and `TARGET` to a new empty directory.
Copy every manifest entry to its `restorePath`. Files ending in `.reference` are
copied without that storage suffix; their contents are unchanged.

```sh
mkdir -p "$TARGET"
jq -c '.files[]' "$REFERENCE/manifest.json" | while IFS= read -r entry; do
  stored=$(printf '%s' "$entry" | jq -r '.storedPath')
  restore=$(printf '%s' "$entry" | jq -r '.restorePath')
  mkdir -p "$TARGET/$(dirname "$restore")"
  cp "$REFERENCE/$stored" "$TARGET/$restore"
done
```

## Verify restored bytes

Every restored file must match the SHA-256 captured in `manifest.json`.

```sh
jq -c '.files[]' "$REFERENCE/manifest.json" | while IFS= read -r entry; do
  restore=$(printf '%s' "$entry" | jq -r '.restorePath')
  expected=$(printf '%s' "$entry" | jq -r '.sha256')
  actual=$(shasum -a 256 "$TARGET/$restore" | awk '{print $1}')
  test "$actual" = "$expected" || exit 1
done
```

Also verify that all manifest paths exist and that the file count matches
`manifest.json.fileCount`. A missing file is a restore failure; do not substitute a
similar file from another run or commit.

## Optional local qualification

Run these commands only inside the disposable target:

```sh
cd "$TARGET"
npm ci
npm run build
npm test
```

The captured tree may require its original CDK entry point or other snapshot files
for synth. If qualification fails, record the exact missing input and recover it
from `workspace.tar.zst` or `repo.bundle`; do not alter the manifest or relax a test.

Local qualification does not make this reference an active deployment target.
