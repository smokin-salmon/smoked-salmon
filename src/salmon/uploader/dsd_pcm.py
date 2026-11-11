"""The spectrals of a DSD file (#919): sox reads no DSD, so PyAV decodes it to PCM and sox reads the samples on stdin.

The samples are at salmon.common.dsd.pcm_rate (176.4 or 192 kHz): a spectrogram of DSD's own rate, 2.8 MHz and up,
would show the audio band as a thin strip at its bottom.
"""

import contextlib
import itertools
import os
import subprocess
import tempfile
from collections.abc import Callable, Generator, Iterator
from typing import TYPE_CHECKING

import anyio
import anyio.to_thread
from mutagen import File as MutagenFile
from mutagen import MutagenError

from salmon.common.dsd import pcm_rate
from salmon.errors import UploadError

if TYPE_CHECKING:
    import av.audio.stream
    import av.container

# About 1 MB of samples per write to sox: few trips to a worker thread for a long track, little memory held.
_BLOCK_BYTES = 1 << 20


async def run_sox_on_dsd(path: str, arguments: Callable[[float], list[str]]) -> None:
    """Run `sox --multi-threaded <the samples of the DSD file at path> <arguments(its length in seconds)>`.

    sox cannot know how long samples on stdin last, as it knows a file's: the arguments are given the length, for
    what depends on it, such as a spectrogram's width in seconds.

    Raises:
        CalledProcessError: If sox fails, with what it printed, as anyio.run_process(check=True) raises it.
        UploadError: If PyAV cannot decode the file.
    """
    rate, channels, length, blocks = await anyio.to_thread.run_sync(_decoder, path)
    command = [
        "sox",
        "--multi-threaded",
        *("-t", "raw", "-r", str(rate), "-e", "floating-point", "-b", "32", "-L", "-c", str(channels), "-"),
        *arguments(length),
    ]
    # A file, not a pipe, for sox's messages: a full pipe nobody reads would block it while the samples go in.
    with tempfile.TemporaryFile() as messages, contextlib.closing(blocks):
        async with await anyio.open_process(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=messages
        ) as process:
            try:
                await _feed(process, blocks)
                returncode = await process.wait()
            except BaseException:
                # Cancelled (the background spectrals are), or the decode failed: sox does not go on without it.
                process.kill()
                with anyio.CancelScope(shield=True):
                    await process.wait()
                raise
        if returncode:
            messages.seek(0)
            raise subprocess.CalledProcessError(returncode, command, stderr=messages.read())


async def _feed(process: anyio.abc.Process, blocks: Iterator[bytes]) -> None:
    """Write every block to sox's stdin, then close it. A sox that stops reading is left to say why as it exits."""
    assert process.stdin is not None
    stdin = process.stdin
    try:
        while (block := await anyio.to_thread.run_sync(next, blocks, None)) is not None:
            await stdin.send(block)
    except (anyio.BrokenResourceError, BrokenPipeError):
        pass
    finally:
        with contextlib.suppress(anyio.BrokenResourceError, BrokenPipeError):
            await stdin.aclose()


def _decoder(path: str) -> tuple[int, int, float, Generator[bytes]]:
    """The PCM rate and channel count the DSD file at path decodes to, its length in seconds, and its samples, as
    _blocks gives them.

    The length is mutagen's, from the file's sample count: FFmpeg's is estimated from the bitrate.

    Raises:
        UploadError: If PyAV cannot open the file, or it has no audio.
    """
    # Imported here: av slows down every salmon start (#367).
    import av

    try:
        dsd = MutagenFile(path)
        container = av.open(path)
    except (MutagenError, av.FFmpegError) as error:
        raise _undecodable(path, error) from error
    if dsd is None:
        container.close()
        raise _undecodable(path, "mutagen cannot read it")
    length = dsd.info.length
    if not container.streams.audio:
        container.close()
        raise _undecodable(path, "no audio stream")
    stream = container.streams.audio[0]
    rate = pcm_rate(stream.codec_context.sample_rate)
    return rate, stream.codec_context.channels, length, _blocks(container, stream, rate)


def _blocks(
    container: "av.container.InputContainer", stream: "av.audio.stream.AudioStream", rate: int
) -> Generator[bytes]:
    """The stream's samples at rate, as little-endian 32-bit floats with the channels interleaved, about _BLOCK_BYTES
    at a time. The container is closed once they are all given, or the generator is closed.

    Raises:
        UploadError: If the decode fails part of the way.
    """
    import av

    resampler = av.AudioResampler(format="flt", layout=stream.codec_context.layout, rate=rate)
    pending: list[bytes] = []
    size = 0
    try:
        # The resampler holds samples back: None flushes the last of them.
        for frame in itertools.chain(container.decode(stream), [None]):
            for resampled in resampler.resample(frame):
                pending.append(resampled.to_ndarray().astype("<f4", copy=False).tobytes())
                size += len(pending[-1])
                if size >= _BLOCK_BYTES:
                    yield b"".join(pending)
                    pending, size = [], 0
        if pending:
            yield b"".join(pending)
    except av.FFmpegError as error:
        raise _undecodable(container.name, error) from error
    finally:
        container.close()


def _undecodable(path: str, reason: object) -> UploadError:
    return UploadError(f"Could not decode {os.path.basename(path)} for its spectrals: {reason}")
