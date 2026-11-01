"""Helpers of tests/test_checks_mqa.py that other test modules use too."""

from pathlib import Path

import av
import av.audio.stream
import numpy as np

import salmon.checks.mqa as mqa

SAMPLE_RATE = 44100


def _write_flac(path: Path, mqa_marker: bool) -> None:
    """Write 1.5 s of 16-bit stereo noise. With mqa_marker, the MQA syncword is in the lowest bit of
    left XOR right, where the detector looks once the samples are widened to 32 bits."""
    rng = np.random.default_rng(len(path.name))
    frames = int(SAMPLE_RATE * 1.5)
    left = rng.integers(-8000, 8000, frames, dtype=np.int16)
    right = left.copy()
    if mqa_marker:
        right[1000 : 1000 + len(mqa.MAGIC)] ^= mqa.MAGIC.astype(np.int16)

    with av.open(str(path), "w", format="flac") as container:
        stream = container.add_stream("flac", rate=SAMPLE_RATE, layout="stereo")
        assert isinstance(stream, av.audio.stream.AudioStream)
        frame = av.AudioFrame.from_ndarray(np.stack([left, right]), format="s16p", layout="stereo")
        frame.sample_rate = SAMPLE_RATE
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
