"""What salmon web prepares for a queued upload while it waits (#790): the checks that only read the album.

Each file's result goes into a Prepared (``salmon.prepared``), which the upload takes once it runs. The steps are
those the upload runs before anything needs an answer or a request, in the order it runs them: the MQA check, the
upconvert check (of the 24bit files, whether or not the user then asks for it), the log CRCs of a CD, the spectrals,
and the integrity check. What the upload's options skip is not prepared.

Nothing here asks a question, sends a request, or writes into the album: the spectral images go into the
preparation's own folder. Nothing is printed either: there is no one to read it. A step or a file that fails leaves no
result, and the upload then checks that file as it would have without a preparation.
"""

import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from importlib import import_module
from typing import Any

import anyio.to_thread

from salmon import cfg
from salmon.common.files import get_audio_files, process_files
from salmon.errors import UpconvertCheckError, UpconvertCheckNotApplicable
from salmon.prepared import Prepared, audio_key
from salmon.uploader import spectrals

# By their modules: salmon.checks has commands of the same names (integrity, mqa, log), which hide them as attributes.
integrity = import_module("salmon.checks.integrity")
logs = import_module("salmon.checks.logs")
mqa = import_module("salmon.checks.mqa")
upconverts = import_module("salmon.checks.upconverts")


async def prepare_upload(
    prepared: Prepared,
    *,
    source: str,
    skip_mqa: bool,
    skip_up: bool,
    skip_log_check: bool,
    skip_integrity_check: bool,
    spectrals_after: bool,
    spectral_ids: tuple[int, ...],
) -> None:
    """Prepare what the upload of prepared.album with these options checks before it asks or sends anything.

    Args:
        source: The upload's source: the log CRCs are prepared for a CD.
        spectral_ids: The upload's --spectrals: () for every track, (0,) for none.
    """
    steps: list[Callable[[], Awaitable[None]]] = []
    if not skip_mqa:
        steps.append(lambda: prepare_mqa(prepared))
    if not skip_up:
        steps.append(lambda: prepare_upconverts(prepared))
    if source == "CD" and not skip_log_check:
        steps.append(lambda: prepare_log_crcs(prepared))
    if not spectrals_after and spectral_ids != (0,):
        steps.append(lambda: _prepare_spectrals_of_album(prepared, spectral_ids))
    if not skip_integrity_check:
        steps.append(lambda: prepare_integrity(prepared))
    for step in steps:
        try:
            await step()
        except Exception:  # noqa: S112
            # Left to the upload, which reports it as it would have.
            continue


def _flacs(album: str) -> list[str]:
    """The FLAC files under the album, in the order the MQA, upconvert and integrity checks list them."""
    return sorted(
        os.path.join(root, name)
        for root, _, names in os.walk(album)
        for name in names
        if name.lower().endswith(".flac")
    )


async def _each(paths: list[str], prepare: Callable[[str], Awaitable[None]]) -> None:
    """Run prepare on each path, as many at once as the checks run, printing nothing; a file that fails is skipped."""

    async def one(path: str, _index: int) -> None:
        with suppress(Exception):
            await prepare(path)

    await process_files(paths, one, "Preparing", on_done=lambda: None)


async def prepare_mqa(prepared: Prepared) -> None:
    flacs = _flacs(prepared.album)
    if not cfg.upload.mqa_check_all_tracks:
        flacs = flacs[:1]

    async def check(path: str) -> None:
        before = os.stat(path)
        prepared.keep("mqa", path, before, await mqa.check_mqa(path))

    await _each(flacs, check)


async def prepare_upconverts(prepared: Prepared) -> None:
    async def check(path: str) -> None:
        before = os.stat(path)
        try:
            found = await upconverts.check_upconvert(path)
        except (UpconvertCheckNotApplicable, UpconvertCheckError):
            # 16bit, or a file the upload reports on itself.
            return
        prepared.keep("upconvert", path, before, found)

    await _each(_flacs(prepared.album), check)


async def prepare_log_crcs(prepared: Prepared) -> None:
    """The CRC of each audio file's decoded audio, if the album holds a log: what verify_log_crcs compares."""
    album = prepared.album
    if not any(name.lower().endswith(".log") for _, _, names in os.walk(album) for name in names):
        return

    async def crc(path: str) -> None:
        before = os.stat(path)
        prepared.keep("crc", path, before, await logs._calculate_file_crc_async(path))

    await _each(sorted(logs._find_audio_files(album)), crc)


async def _prepare_spectrals_of_album(prepared: Prepared, spectral_ids: tuple[int, ...]) -> None:
    # Imported here: it reads every file's tags, which salmon web loads when a job needs it.
    from salmon.tagger.audio_info import gather_audio_info

    audio_info = await anyio.to_thread.run_sync(gather_audio_info, prepared.album, True)
    await prepare_spectrals(prepared, audio_info, spectral_ids)


async def prepare_spectrals(prepared: Prepared, audio_info: dict[str, Any], spectral_ids: tuple[int, ...] = ()) -> None:
    """The spectral images of each FLAC file the upload makes them of, in the preparation's folder."""
    album = prepared.album
    if spectral_ids:
        wanted = spectrals.get_wanted_filenames(list(audio_info), spectral_ids)
        files = [name for name in get_audio_files(album) if name in wanted]
    else:
        files = get_audio_files(album, True)
    files = [name for name in files if name.lower().endswith(".flac") and name in audio_info]

    async def make(name: str, index: int) -> None:
        path = os.path.join(album, name)
        try:
            before = os.stat(path)
            await spectrals._generate_spectral_for_file(album, name, prepared.folder, audio_info, index)
        except Exception:
            return
        made = tuple(os.path.join(prepared.folder, f"{index + 1:02d} {kind}.png") for kind in ("Full", "Zoom"))
        prepared.keep("spectral", path, before, made)

    await process_files(files, make, "Preparing", on_done=lambda: None)


async def prepare_integrity(prepared: Prepared) -> None:
    """flac's test of each FLAC file, kept by the file's audio when it passed clean."""

    async def check(path: str) -> None:
        key = await anyio.to_thread.run_sync(audio_key, path)
        if key is None:
            return
        found = await integrity._check_flac_integrity(path)
        # Changed while flac read it: which audio passed is not known.
        if await anyio.to_thread.run_sync(audio_key, path) != key:
            return
        if found.passed and not found.details and not found.concerns and not found.md5_unset:
            prepared.keep_audio("integrity", key, found)

    await _each(_flacs(prepared.album), check)
