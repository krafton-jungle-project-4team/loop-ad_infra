#!/bin/bash
set -euo pipefail

lock_file=/tmp/loopad-phase6-archive.lock
held_marker=/tmp/loopad-phase6-archive-held
flock -x "${lock_file}" sh -c "touch '${held_marker}'; sleep 2" &
holder_pid=$!

for _ in {1..50}; do
    [[ -f "${held_marker}" ]] && break
    sleep 0.02
done
[[ -f "${held_marker}" ]]

set +e
flock -n -E 75 "${lock_file}" true
overlap_exit=$?
set -e
wait "${holder_pid}"

printf 'holder_acquired=true\noverlap_exit=%s\n' "${overlap_exit}"
[[ "${overlap_exit}" -eq 75 ]]
