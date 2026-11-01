"""Results of an upload's local checks, made before it runs: salmon web prepares a queued upload (#790).

A preparation runs the checks of an upload that only read the album (``uploader.preparation``) and keeps each file's
result in a Prepared. While the upload runs with it (``using``), each check takes a file's result from it instead of
computing it again, if the file is unchanged since its check started: the same path in the album, size and
modification time. The path is taken within the album, so the copy ``staged_source`` makes of a library album, which
keeps the modification times, takes the results of the album it copies.

The integrity check runs once the files are retagged and renamed, so its results are kept by the file's audio
instead: everything in the file but its tags (``audio_key``). Only a clean pass is kept, so a failure is always checked
again.
"""

import hashlib
import os
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

import anyio.to_thread

T = TypeVar("T")

# FLAC metadata blocks a tagger rewrites: PADDING, VORBIS_COMMENT, PICTURE.
_TAG_BLOCKS = frozenset({1, 4, 6})
_CHUNK = 1 << 20


class Prepared:
    """The results a preparation made for one album, by check. Kept from one thread, read from another."""

    def __init__(self, album: str, folder: str) -> None:
        """Args:
        album: The album folder the results are for.
        folder: A folder of the preparation's own, outside the album, where it writes (spectral images).
        """
        self.album = os.path.realpath(album)
        self.folder = folder
        self._lock = threading.Lock()
        # (check, path in the album) -> (size, modification time, result)
        self._by_file: dict[tuple[str, str], tuple[int, int, Any]] = {}
        # (check, audio_key) -> result
        self._by_audio: dict[tuple[str, str], Any] = {}

    def keep(self, check: str, path: str, before: os.stat_result, result: Any) -> None:
        """Keep a file of the album's result, with the file's state when its check started."""
        relative = os.path.relpath(os.path.realpath(path), self.album)
        with self._lock:
            self._by_file[(check, relative)] = (before.st_size, before.st_mtime_ns, result)

    def keep_audio(self, check: str, key: str, result: Any) -> None:
        """Keep a result by the file's audio_key."""
        with self._lock:
            self._by_audio[(check, key)] = result

    def get(self, check: str, album: str, path: str) -> Any | None:
        """The result kept for the file at `path` in `album`, if the file is unchanged since; else None."""
        relative = os.path.relpath(path, album)
        with self._lock:
            kept = self._by_file.get((check, relative))
        if kept is None:
            return None
        try:
            now = os.stat(path)
        except OSError:
            return None
        size, mtime_ns, result = kept
        return result if (now.st_size, now.st_mtime_ns) == (size, mtime_ns) else None

    def get_audio(self, check: str, key: str) -> Any | None:
        with self._lock:
            return self._by_audio.get((check, key))

    def has_audio(self, check: str) -> bool:
        with self._lock:
            return any(kept == check for kept, _key in self._by_audio)

    def checks(self) -> set[str]:
        """The checks it holds a result of."""
        with self._lock:
            return {check for check, _ in self._by_file} | {check for check, _ in self._by_audio}


_current: ContextVar[Prepared | None] = ContextVar("prepared", default=None)


@contextmanager
def using(prepared: Prepared | None) -> Iterator[None]:
    """Let the block's checks, and those of every task it starts, take `prepared`'s results."""
    token = _current.set(prepared)
    try:
        yield
    finally:
        _current.reset(token)


def result(check: str, album: str, path: str) -> Any | None:
    """The prepared result of `check` for the file at `path` in the album folder `album`, if any (see Prepared.get)."""
    prepared = _current.get()
    return prepared.get(check, album, path) if prepared is not None else None


def reusing(check: str, album: str, compute: Callable[[str, int], Awaitable[T]]) -> Callable[[str, int], Awaitable[T]]:
    """`compute`, a process_files function over files of `album`, taking each file's prepared result where there is
    one."""
    prepared = _current.get()
    if prepared is None:
        return compute

    async def taking(path: str, index: int) -> T:
        kept = prepared.get(check, album, path)
        return kept if kept is not None else await compute(path, index)

    return taking


async def audio_result(check: str, path: str) -> Any | None:
    """The prepared result of `check` for a file with the same audio as the file at `path`, if any."""
    prepared = _current.get()
    if prepared is None or not prepared.has_audio(check):
        return None
    key = await anyio.to_thread.run_sync(audio_key, path)
    return prepared.get_audio(check, key) if key is not None else None


def audio_key(path: str) -> str | None:
    """A digest of a FLAC file without its tags: its PADDING, VORBIS_COMMENT and PICTURE blocks, and which metadata
    block is last, are left out. Two files with the same key have the same audio and the same other metadata.

    Returns:
        None if the file cannot be read or is no FLAC stream.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            start = handle.read(10)
            if start[:3] == b"ID3" and len(start) == 10:
                # An ID3v2 tag before the stream is no part of FLAC, and stays in the key.
                size = int.from_bytes(bytes(b & 0x7F for b in start[6:10]), "big") + 10
                handle.seek(0)
                digest.update(handle.read(size))
            else:
                handle.seek(0)
            if handle.read(4) != b"fLaC":
                return None
            while True:
                header = handle.read(4)
                if len(header) != 4:
                    return None
                block_type, length = header[0] & 0x7F, int.from_bytes(header[1:], "big")
                data = handle.read(length)
                if len(data) != length:
                    return None
                if block_type not in _TAG_BLOCKS:
                    digest.update(bytes([block_type]) + header[1:] + data)
                if header[0] & 0x80:
                    break
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()
