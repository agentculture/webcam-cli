#!/usr/bin/env bash
# On-host acceptance for the a-v-streaming build plan (task t9).
#
# Proves the blind-consumer contract against real hardware: `list` -> `stream`
# -> attach and `list` -> `record`, driven from a stable device id and `--json`
# alone, with a live second-process decode, a busy-device probe, and the
# pre-streaming before-state cited from git history.
#
# Headless and non-interactive by construction: no TTY is required, nothing is
# prompted, and every step's verdict is printed. Re-runnable.
#
#   scripts/acceptance/run.sh [--device STABLE_ID] [--port N] [--seconds N]
#                             [--measure-warmup] [--skip-suite]
#
# PRIVACY. This switches on a camera and a microphone. Every media artifact is
# written under a run directory in $TMPDIR — never inside the repository — and
# deleted on exit, including on failure. Only byte counts, durations, frame
# counts and decoder caps survive into the record.

set -euo pipefail

# --- configuration ----------------------------------------------------------

# The reference host's Logitech C270. The *only* device this script targets:
# the Arducam and the Reachy Mini microphone are explicitly out of scope.
DEVICE="${WEBCAM_ACCEPTANCE_DEVICE:-usb-046d_C270_HD_WEBCAM_200901010001}"

# The commit `main` sat at before any capture code existed. Verified, not
# asserted: step 0 refuses to continue if this tree says otherwise.
BEFORE_COMMIT="${WEBCAM_ACCEPTANCE_BEFORE:-52aa9fd}"

PORT=5000
SECONDS_PER_CONSUMER=4
MEASURE_WARMUP=0
RUN_SUITE=1

while [ $# -gt 0 ]; do
    case "$1" in
        --device) DEVICE="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --seconds) SECONDS_PER_CONSUMER="$2"; shift 2 ;;
        --measure-warmup) MEASURE_WARMUP=1; shift ;;
        --skip-suite) RUN_SUITE=0; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$HERE" rev-parse --show-toplevel)

RUN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/webcam-acceptance.XXXXXX")
export WEBCAM_ACTIVATION_LOG="$RUN_DIR/activations.jsonl"

STREAM_PID=""

cleanup() {
    if [ -n "$STREAM_PID" ] && kill -0 "$STREAM_PID" 2>/dev/null; then
        kill -INT "$STREAM_PID" 2>/dev/null || true
        sleep 2
        kill -KILL "$STREAM_PID" 2>/dev/null || true
    fi
    # Media never outlives the run. Byte counts already went to the report.
    find "$RUN_DIR" -type f \
        \( -name '*.mkv' -o -name '*.webm' -o -name '*.wav' -o -name '*.gray' \
           -o -name '*.raw' -o -name '*.jpg' -o -name '*.png' \) -delete 2>/dev/null || true
    echo
    echo "media deleted; evidence (json payloads, logs) left in $RUN_DIR"
}
trap cleanup EXIT

PASS=0
FAIL=0
step() { echo; echo "=============================================================="; echo "$*"; echo "=============================================================="; }
ok()   { PASS=$((PASS + 1)); echo "  PASS: $*"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL: $*"; }

WEBCAM=(uv run --project "$REPO" webcam)
jget() { python3 "$HERE/payload.py" get "$@"; }

echo "webcam-cli on-host acceptance (task t9)"
echo "device : $DEVICE"
echo "run dir: $RUN_DIR"
echo "tty    : $(tty 2>/dev/null || echo 'not a tty')"

# --- step 0: the before-state, verified rather than asserted ----------------

step "step 0 — before-state: $BEFORE_COMMIT predates all capture code"
git -C "$REPO" log -1 --format='  %H%n  %ad  %s' --date=short "$BEFORE_COMMIT"
BEFORE_FILES=$(git -C "$REPO" ls-tree -r --name-only "$BEFORE_COMMIT" -- webcam_cli)
echo "  webcam_cli/ at that commit:"
echo "$BEFORE_FILES" | sed 's/^/    /'
if echo "$BEFORE_FILES" | grep -qE 'devices\.py|access\.py|engine\.py|activation\.py|list_devices\.py|stream\.py|record\.py'; then
    bad "the cited before-state already contains capture code"
else
    ok "no devices/access/engine/activation/list/stream/record module existed"
fi

# --- step 1: list, by stable id, JSON only ----------------------------------

step "step 1 — list: resolve the target from --json alone"
"${WEBCAM[@]}" list --json >"$RUN_DIR/list.json" 2>"$RUN_DIR/list.err"
if [ -s "$RUN_DIR/list.err" ]; then
    bad "list wrote to stderr on success"
else
    ok "list exited 0 with an empty stderr"
fi

python3 - "$RUN_DIR/list.json" "$DEVICE" >"$RUN_DIR/target.env" <<'PY'
import json, sys
devices = json.load(open(sys.argv[1]))["devices"]
matches = [d for d in devices if d["stable_id"] == sys.argv[2]]
if len(matches) != 1:
    sys.exit(f"expected exactly one device with stable_id {sys.argv[2]}, got {len(matches)}")
d = matches[0]
print(f"TARGET_NODE={d['capture_node']}")
print(f"TARGET_ALSA={d['audio']['alsa_address'] if d['audio'] else ''}")
print(f"TARGET_VIDEO_ACCESS={d['video_access']['state']}")
print(f"TARGET_AUDIO_ACCESS={d['audio_access']['state'] if d.get('audio_access') else 'none'}")
print(f"TARGET_NODES={len(d['video_nodes'])}")
PY
# shellcheck disable=SC1091
. "$RUN_DIR/target.env"
echo "  capture_node=$TARGET_NODE  alsa=$TARGET_ALSA  nodes=$TARGET_NODES"
echo "  access: video=$TARGET_VIDEO_ACCESS audio=$TARGET_AUDIO_ACCESS"
if [ "$TARGET_VIDEO_ACCESS" = "ok" ] && [ "$TARGET_AUDIO_ACCESS" = "ok" ]; then
    ok "both media of $DEVICE are openable from this session"
else
    bad "access is not ok (video=$TARGET_VIDEO_ACCESS audio=$TARGET_AUDIO_ACCESS)"
fi

# --- step 2: stream av --apply, then a blind consumer -----------------------

step "step 2 — stream av --apply, attach a blind consumer from the payload"
: >"$RUN_DIR/stream.json"
setsid "${WEBCAM[@]}" stream av "$DEVICE" --apply --port "$PORT" --json \
    >"$RUN_DIR/stream.json" 2>"$RUN_DIR/stream.err" </dev/null &
STREAM_PID=$!

for _ in $(seq 1 60); do
    if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$RUN_DIR/stream.json" \
        2>/dev/null; then
        break
    fi
    sleep 0.5
done

if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$RUN_DIR/stream.json" 2>/dev/null
then
    ok "stream announced its attachment point: $(jget "$RUN_DIR/stream.json" attach.uri)"
    echo "  negotiated: $(jget "$RUN_DIR/stream.json" negotiation.negotiated)"
    echo "  warm-up   : $(jget "$RUN_DIR/stream.json" warmup.ms) ms / $(jget "$RUN_DIR/stream.json" warmup.frames) frames"
else
    bad "stream never emitted a JSON payload"
    cat "$RUN_DIR/stream.err" >&2 || true
    exit 1
fi
[ -s "$RUN_DIR/stream.err" ] && bad "stream wrote to stderr" || ok "stream stderr empty"

if bash "$HERE/blind-consumer.sh" "$RUN_DIR/stream.json" "$RUN_DIR/consumer" \
    "$SECONDS_PER_CONSUMER"; then
    ok "blind consumer decoded live video and audio from the payload alone"
else
    bad "blind consumer could not consume the announced stream"
fi

# --- step 3: busy probe while the device is genuinely held ------------------

step "step 3 — busy probe: a second open of the held C270"
for medium in video audio; do
    start=$(date +%s.%N)
    set +e
    "${WEBCAM[@]}" stream "$medium" "$DEVICE" --apply --port $((PORT + 11)) --json \
        >"$RUN_DIR/busy-$medium.out" 2>"$RUN_DIR/busy-$medium.err"
    rc=$?
    set -e
    elapsed=$(python3 -c "print(f'{$(date +%s.%N) - $start:.2f}')")
    message=$(python3 -c "
import json,sys
try:
    print(json.load(open(sys.argv[1]))['message'])
except Exception as exc:
    print(f'(unparseable: {exc})')
" "$RUN_DIR/busy-$medium.err")
    echo "  $medium: exit=$rc elapsed=${elapsed}s"
    echo "    $message"
    if [ "$rc" -eq 2 ] && printf '%s' "$message" | grep -qi 'busy'; then
        ok "$medium second open returned the typed busy error in ${elapsed}s"
    else
        bad "$medium second open did not return a typed busy error (exit $rc)"
    fi
done

# --- step 4: stop the stream, check the consent record ----------------------

step "step 4 — stop the stream and audit the activation log"
kill -INT "$STREAM_PID" 2>/dev/null || true
for _ in $(seq 1 20); do kill -0 "$STREAM_PID" 2>/dev/null || break; sleep 0.5; done
STREAM_PID=""

python3 - "$WEBCAM_ACTIVATION_LOG" <<'PY'
import json, sys
lines = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
for entry in lines:
    detail = entry["detail"]
    print(f"  {entry['verb']:<13} target={entry['target']}")
    print(f"      ended={entry['ended_at'] is not None} pid={detail.get('pid')} "
          f"negotiated={detail.get('negotiated')} warmup_ms={detail.get('warmup_ms')}")
applied = [e for e in lines if e["detail"].get("mode") == "apply" and "error" not in e["detail"]]
assert applied, "no successful apply activation was recorded"
for entry in applied:
    detail = entry["detail"]
    assert entry["ended_at"], "an activation has no ended_at"
    assert detail.get("pid"), "an applied activation did not record the pipeline pid"
    assert detail.get("negotiated"), "an applied activation did not record the negotiated format"
    assert detail.get("warmup_ms") is not None, "an applied activation did not record warm-up"
print("  every applied activation carries ended_at, pid, negotiated format and warm-up")
PY
ok "activation log is complete for every applied activation"

# --- step 5: list -> record, from the same stable id ------------------------

step "step 5 — record: bounded av clip driven from the same stable id"
CLIP="$RUN_DIR/clip.mkv"
"${WEBCAM[@]}" record "$DEVICE" "$CLIP" --kind av --duration 3 --json \
    >"$RUN_DIR/record-dry.json" 2>"$RUN_DIR/record-dry.err"
if [ -e "$CLIP" ]; then
    bad "the dry run wrote a file it only promised to write"
else
    ok "dry run wrote nothing; would_write=$(jget "$RUN_DIR/record-dry.json" would_write)"
fi

"${WEBCAM[@]}" record "$DEVICE" "$CLIP" --kind av --duration 3 --apply --json \
    >"$RUN_DIR/record.json" 2>"$RUN_DIR/record.err"
BYTES=$(jget "$RUN_DIR/record.json" bytes_written)
echo "  bytes_written=$BYTES stopped_reason=$(jget "$RUN_DIR/record.json" stopped_reason)"
echo "  video=$(jget "$RUN_DIR/record.json" video_format.negotiated)"
echo "  audio=$(jget "$RUN_DIR/record.json" audio_format.negotiated)"
echo "  warmup=$(jget "$RUN_DIR/record.json" warmup_s)s / $(jget "$RUN_DIR/record.json" warmup_frames) frames"
if [ -s "$CLIP" ] && [ "$BYTES" -gt 0 ]; then
    ok "record produced exactly one non-empty artifact ($BYTES bytes)"
else
    bad "record produced no artifact"
fi

if python3 - "$CLIP" >"$RUN_DIR/clip-decode.log" 2>&1 <<'PY'
import re, subprocess, sys
argv = [
    "gst-launch-1.0", "-v", "fdsrc", "!", "matroskademux", "name=d",
    "d.", "!", "queue", "!", "jpegdec", "!", "videoconvert",
    "!", "identity", "name=c0", "silent=false", "!", "fakesink", "sync=false",
    "d.", "!", "queue", "!", "audioconvert",
    "!", "identity", "name=c1", "silent=false", "!", "fakesink", "sync=false",
]
with open(sys.argv[1], "rb") as handle:
    out = subprocess.run(argv, stdin=handle, capture_output=True, text=True, timeout=120).stdout
video = out.count("GstIdentity:c0: last-message = chain")
audio = out.count("GstIdentity:c1: last-message = chain")
caps = re.search(r"GstIdentity:c0\.GstPad:sink: caps = ([^\n]+)", out)
print(f"decoded {video} video frames and {audio} audio buffers")
print(f"video caps: {caps.group(1)[:120] if caps else 'none'}")
sys.exit(0 if video > 0 and audio > 0 else 1)
PY
then
    ok "recorded clip decodes: $(head -1 "$RUN_DIR/clip-decode.log")"
else
    bad "recorded clip did not decode: $(tail -2 "$RUN_DIR/clip-decode.log")"
fi

# --- step 6 (optional): measure the auto-exposure settle curve --------------

if [ "$MEASURE_WARMUP" -eq 1 ]; then
    step "step 6 — measure the C270 auto-exposure settle curve"
    python3 "$HERE/warmup-measure.py" "$TARGET_NODE" "$RUN_DIR" \
        && ok "warm-up measured" || bad "warm-up measurement failed"
fi

# --- step 7: the suite, the rubric, the lints -------------------------------

if [ "$RUN_SUITE" -eq 1 ]; then
    step "step 7 — suite and rubric"
    (cd "$REPO" && uv run pytest -n auto -q 2>&1 | tail -3) && ok "pytest -n auto green" \
        || bad "pytest failed"
    (cd "$REPO" && uv run teken cli doctor . --strict 2>&1 | tail -3) \
        && ok "teken cli doctor --strict green" || bad "rubric gate failed"
fi

# --- verdict ----------------------------------------------------------------

step "verdict: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
