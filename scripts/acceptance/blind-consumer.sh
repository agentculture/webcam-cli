#!/usr/bin/env bash
# Blind consumer: attach to a live `webcam stream` using nothing but its
# --json payload.
#
# This script is deliberately, structurally blind. Its ONLY input is the path
# to a payload file. It never runs `webcam`, never reads /dev, never inspects
# /proc/asound, and never learns the device's stable id. If it can decode
# video and audio with only that file, the attachment contract holds; if it
# needs any fact the payload did not announce, the contract has failed and
# that is a finding, not something to patch around here.
#
# Usage: blind-consumer.sh <payload.json> <workdir> [seconds]
#
# Writes its own evidence to <workdir>. Media it captures is the caller's to
# delete; this script never writes inside a git repository by design (the
# caller passes a workdir under $TMPDIR).

set -euo pipefail

payload="${1:?usage: blind-consumer.sh <payload.json> <workdir> [seconds]}"
workdir="${2:?usage: blind-consumer.sh <payload.json> <workdir> [seconds]}"
seconds="${3:-4}"

[ -r "$payload" ] || { echo "blind-consumer: cannot read $payload" >&2; exit 2; }
mkdir -p "$workdir"

# --- everything below is derived from the payload alone ---------------------

get() { python3 "$(dirname "$0")/payload.py" get "$payload" "$1"; }

uri=$(get attach.uri)
host=$(get attach.host)
port=$(get attach.port)
container=$(get attach.container)
verbatim=$(get attach.consumer.gst_launch_str)
generic=$(get attach.consumer.generic)
save_to_file=$(get attach.consumer.save_to_file)
medium=$(get medium)

echo "blind-consumer: payload announces uri=$uri container=$container medium=$medium"
echo "blind-consumer: announced consumer command:"
echo "  $verbatim"

pass=0
fail=0
note() { echo "  -> $1"; }
ok() { pass=$((pass + 1)); note "PASS: $1"; }
bad() { fail=$((fail + 1)); note "FAIL: $1"; }

# gst-launch-1.0 treats SIGINT as "send EOS, finalize, exit 0", so a clean
# timed stop is exit 0. Any other exit, or an ERROR line, is a real failure.
run_announced() {
    local label="$1" command="$2" log="$3"
    set +e
    # shellcheck disable=SC2086
    timeout --signal=INT "$seconds" bash -c "$command" >"$log" 2>&1
    local status=$?
    set -e
    if [ "$status" -ne 0 ] && [ "$status" -ne 124 ]; then
        bad "$label exited $status"
        tail -5 "$log" | sed 's/^/     /'
        return
    fi
    if grep -qE '^ERROR|Erroneous pipeline|WARNING.*not-negotiated' "$log"; then
        bad "$label reported an error"
        grep -E '^ERROR|Erroneous pipeline' "$log" | head -3 | sed 's/^/     /'
        return
    fi
    ok "$label ran clean for ${seconds}s against $uri"
}

echo
echo "[1/5] announced consumer command, verbatim"
run_announced "attach.consumer.gst_launch_str" "$verbatim" "$workdir/verbatim.log"

echo
echo "[2/5] announced generic (decodebin) command, verbatim"
run_announced "attach.consumer.generic" "$generic" "$workdir/generic.log"

echo
echo "[3/5] announced raw_socket contract (EBML magic on a bare TCP socket)"
if python3 "$(dirname "$0")/payload.py" ebml "$host" "$port" >"$workdir/ebml.log" 2>&1; then
    ok "$(cat "$workdir/ebml.log")"
else
    bad "raw socket did not yield an EBML header: $(cat "$workdir/ebml.log")"
fi

echo
echo "[4/5] live decode with per-branch buffer counts"
echo "      (the announced command, instrumented with 'identity' counters --"
echo "       a string substitution on it, not a fact from outside the payload)"
if python3 "$(dirname "$0")/payload.py" live "$payload" "$seconds" \
    >"$workdir/live.log" 2>&1; then
    ok "$(head -1 "$workdir/live.log")"
else
    bad "live decode produced no buffers: $(head -3 "$workdir/live.log")"
fi

echo
echo "[5/5] announced save_to_file, verbatim, then decode the captured bytes"
# The announced string carries a *relative* location, so running it from the
# workdir keeps it verbatim and still lands the file outside any repo.
save_status=0
set +e
(cd "$workdir" && timeout --signal=INT "$seconds" bash -c "$save_to_file") \
    >"$workdir/save.log" 2>&1
save_status=$?
set -e
captured="$workdir/stream.mkv"
if { [ "$save_status" -eq 0 ] || [ "$save_status" -eq 124 ]; } && [ -s "$captured" ]; then
    bytes=$(wc -c <"$captured")
    ok "save_to_file captured $bytes bytes to the announced location"
    if python3 "$(dirname "$0")/payload.py" decode "$payload" "$captured" \
        >"$workdir/decode.log" 2>&1; then
        ok "captured container decoded: $(cat "$workdir/decode.log")"
    else
        bad "captured container failed to decode: $(tail -3 "$workdir/decode.log")"
    fi
else
    bad "save_to_file produced no bytes (exit $save_status)"
fi

echo
echo "blind-consumer: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
