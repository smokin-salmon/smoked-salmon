"""MQA detection for FLAC files.

Detects MQA syncword by XOR-ing left/right stereo channels and
searching for the magic bit pattern in bits 16-23.

Original algorithm by redsudo (MIT License).
Rewritten to use PyAV for decoding and NumPy for bit operations.
"""

import anyio.to_thread
import asyncclick as click
import av
import av.audio.frame
import av.audio.resampler
import numpy as np
from numpy.typing import NDArray

# MQA magic syncword: 0xbe0498c88 (36 bits)
_MAGIC_HEX = 0xBE0498C88
_MAGIC_BITS = 36
MAGIC: NDArray[np.uint8] = np.array(
    [(_MAGIC_HEX >> (_MAGIC_BITS - 1 - i)) & 1 for i in range(_MAGIC_BITS)],
    dtype=np.uint8,
)


def _decode_first_second(path: str) -> NDArray[np.int32]:
    """Decode the first second of a FLAC file as int32 PCM samples.

    Uses PyAV AudioResampler to convert to s32 (signed 32-bit), which
    automatically left-aligns samples regardless of original bit depth
    (16-bit << 16, 24-bit << 8), matching the original behavior.

    Args:
        path: Path to the FLAC file.

    Returns:
        Array of shape (n_frames, 2) with dtype int32 for stereo audio.

    Raises:
        ValueError: If the file is not FLAC, not stereo, or cannot be decoded.
    """
    container = av.open(path)
    try:
        stream = container.streams.audio[0]
        if stream.codec_context.name != "flac":
            raise ValueError("Not a FLAC file")

        sample_rate = stream.rate
        if sample_rate is None:
            raise ValueError("Cannot determine sample rate")

        resampler = av.audio.resampler.AudioResampler(format="s32", layout="stereo")
        target_samples = sample_rate  # 1 second
        collected: list[NDArray[np.int32]] = []
        total = 0

        for frame in container.decode(audio=0):
            if not isinstance(frame, av.audio.frame.AudioFrame):
                continue
            for resampled in resampler.resample(frame):
                arr: NDArray[np.int32] = resampled.to_ndarray().astype(np.int32).T
                remaining = target_samples - total
                if remaining <= 0:
                    break
                if arr.shape[0] > remaining:
                    arr = arr[:remaining]
                collected.append(arr)
                total += arr.shape[0]
            if total >= target_samples:
                break

        if not collected:
            raise ValueError("No audio frames decoded")

        samples = np.concatenate(collected, axis=0)
    finally:
        container.close()

    if samples.shape[1] != 2:
        raise ValueError("Input must be stereo")

    return samples


def _check_mqa_syncword(
    left: NDArray[np.int32],
    right: NDArray[np.int32],
) -> bool:
    """Check for MQA syncword in the XOR of left and right channels.

    Pure function. Searches bits 16-23 of (left ^ right) for the
    MQA magic bit pattern.

    Args:
        left: Left channel samples, dtype int32.
        right: Right channel samples, dtype int32.

    Returns:
        True if MQA syncword is found in any of the 8 bit positions.
    """
    xor = left ^ right
    magic_len = len(MAGIC)

    for bit_pos in range(16, 24):
        # Extract single bit at bit_pos from each sample
        bits = ((xor >> bit_pos) & 1).astype(np.uint8)
        if len(bits) < magic_len:
            continue
        # Sliding window search: convert 0/1 to -1/+1, correlate
        signal = bits.astype(np.int8) * 2 - 1
        pattern = MAGIC.astype(np.int8) * 2 - 1
        corr = np.correlate(signal, pattern, mode="valid")
        if np.any(corr == magic_len):
            return True

    return False


def _check_mqa_sync(path: str) -> bool:
    """Synchronous MQA check combining decode and syncword detection.

    Args:
        path: Path to the FLAC file.

    Returns:
        True if MQA syncword is detected, False otherwise.
    """
    try:
        samples = _decode_first_second(path)
    except Exception:
        click.secho(f"Failed to decode file: {path}", fg="red")
        return False

    return _check_mqa_syncword(samples[:, 0], samples[:, 1])


async def check_mqa(path: str) -> bool:
    """Check if a FLAC file contains MQA markers.

    Runs the CPU-intensive decode and detection in a thread
    to avoid blocking the event loop.

    Args:
        path: Path to the FLAC file.

    Returns:
        True if MQA syncword is detected, False otherwise.
        Returns False for non-FLAC files.
    """
    return await anyio.to_thread.run_sync(_check_mqa_sync, path)
