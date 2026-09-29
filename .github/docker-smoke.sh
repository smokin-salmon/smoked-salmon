#!/bin/sh
# Smoke test run inside the built salmon image to check the tools it depends
# on actually work. Extend this script rather than adding a separate one; the
# workflow that calls it (.github/workflows/docker-pr.yml) just runs it.
set -eu

# Usage: docker-smoke.sh <expected rclone version, e.g. 1.75.1>
expected_rclone=${1:?usage: docker-smoke.sh <expected rclone version>}

workdir=$(mktemp -d)
cleanup() {
    rm -rf "$workdir"
}
trap cleanup EXIT

wav="$workdir/tone.wav"
mp3="$workdir/tone.mp3"
full_png="$workdir/full.png"
zoom_png="$workdir/zoom.png"

echo "== flac --version =="
flac --version

echo "== rclone is the pinned version ($expected_rclone) =="
actual_rclone=$(rclone version | sed -n '1s/^rclone v//p')
if [ "$actual_rclone" != "$expected_rclone" ]; then
    echo "rclone is '$actual_rclone', expected '$expected_rclone'" >&2
    exit 1
fi

echo "== mp3val present =="
command -v mp3val

echo "== generating a 2s test tone =="
sox -n "$wav" synth 2 sine 440

echo "== encoding it to mp3 with lame =="
lame --silent "$wav" "$mp3"

echo "== running salmon's sox spectrogram invocation against the mp3 =="
sox --multi-threaded "$mp3" --buffer 128000 -n \
    remix 1 spectrogram -x 2000 -y 513 -z 120 -w Kaiser -o "$full_png" \
    remix 1 spectrogram -x 500 -y 1025 -z 120 -w Kaiser -S 0 -d 0:02 -o "$zoom_png"

for png in "$full_png" "$zoom_png"; do
    if [ ! -s "$png" ]; then
        echo "expected non-empty PNG at $png" >&2
        exit 1
    fi
done

echo "== smoke test passed =="
