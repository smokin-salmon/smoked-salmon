"""salmon up on an album of ALAC tracks: convert each .m4a holding ALAC to a FLAC, which the upload then takes (#981).

The FLAC keeps the ALAC's samples exactly (bit depth, sample rate and channels), the tags in _TEXT_TAGS and
_NUMBER_TAGS and its first cover if it is a JPEG or PNG; other tags are dropped. Each FLAC is checked to decode to the
same samples as its ALAC before any .m4a is removed.
"""

import contextlib
import hashlib
import os
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, cast

import anyio
import asyncclick as click
from mutagen import MutagenError
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from mutagen.mp4 import MP4, AtomDataType, MP4Cover, MP4FreeForm, MP4Info

from salmon import cfg, interaction
from salmon.common.constants import AUDIO_EXTENSIONS, LOSSY_EXTENSIONS
from salmon.common.files import album_files, process_files
from salmon.tagger.cover import get_8kib_padding

if TYPE_CHECKING:
    import av.audio.frame

# How many lossy files a refusal names before it counts the rest.
_NAMED = 5
_BIT_DEPTHS = (16, 24)
_TEXT_TAGS = {
    "©nam": "TITLE",
    "©ART": "ARTIST",
    "aART": "ALBUMARTIST",
    "©alb": "ALBUM",
    "©day": "DATE",
    "©gen": "GENRE",
    "©wrt": "COMPOSER",
    "cprt": "COPYRIGHT",
    "----:com.apple.iTunes:ISRC": "ISRC",
    "----:com.apple.iTunes:LABEL": "LABEL",
}
_NUMBER_TAGS = {"trkn": ("TRACKNUMBER", "TRACKTOTAL"), "disk": ("DISCNUMBER", "DISCTOTAL")}
_COVER_MIMES = {MP4Cover.FORMAT_JPEG: "image/jpeg", MP4Cover.FORMAT_PNG: "image/png"}
# The FLAC fields the conversion fills, for the message before the question.
_KEPT_FIELDS = ", ".join(
    field.lower() for field in (*_TEXT_TAGS.values(), *(name for pair in _NUMBER_TAGS.values() for name in pair))
)


class ConversionError(Exception):
    """A track could not be converted, or its FLAC does not hold the ALAC's samples."""


@dataclass(frozen=True)
class AlacTrack:
    """An .m4a holding ALAC, and the FLAC it becomes next to it."""

    source: str
    target: str
    name: str  # The .m4a's path in the album, for messages.
    bits: int
    sample_rate: int
    channels: int


async def convert_alac(album: str, staged: str) -> str | None:
    """Convert the ALAC files of the folder the upload works on to FLAC, asking first; why the run stops, if it does.

    An album with no ALAC file is left as it is. One that holds lossy files too, or a FLAC where an ALAC file's would
    go, stops before the question. Under -yyy the answer is yes; a no stops the run, since the files are not AAC.

    Args:
        album: The album folder the user gave.
        staged: The folder the upload works on (see staged_source): the conversion writes there.
    """
    tracks, others, unreadable = _find_alac(staged)
    if not tracks:
        return None
    if refusal := _refusal(album, staged, tracks, others, unreadable):
        return refusal
    count = f"{len(tracks)} ALAC file{'s' if len(tracks) > 1 else ''}"
    in_place = os.path.abspath(staged) == os.path.abspath(album)
    where = f"in {album}" if in_place else "in the copy salmon works on"
    them = "them" if len(tracks) > 1 else "it"
    click.secho(
        f"\n{album} holds {count} (Apple Lossless in .m4a). salmon uploads {them} as FLAC, with the same samples, "
        f"bit depth, sample rate and channels. It keeps these tags: {_KEPT_FIELDS}, and the first cover if it is a "
        f"JPEG or PNG; it drops the others. Each .m4a {where} is replaced by its .flac.",
        fg="yellow",
    )
    if not await interaction.assume_defaults() and not await interaction.confirm(
        click.style(f"Convert the {count} to FLAC for the upload?", fg="magenta"),
        default=True,
        labels=("Convert", "Stop"),
    ):
        return f"Not uploading {album}: salmon uploads ALAC only once converted to FLAC."
    if error := await _convert_all(tracks, album if in_place else None):
        return error
    depths = " and ".join(f"{bits}-bit" for bits in sorted({track.bits for track in tracks}))
    click.secho(f"Converted {count} to {depths} FLAC.", fg="green")
    return None


def _mp4_info(path: str) -> MP4Info | None:
    """The stream info of the .m4a at path; None if it cannot be read."""
    try:
        return MP4(path).info
    except (MutagenError, OSError):
        return None


def _find_alac(folder: str) -> tuple[list[AlacTrack], list[str], list[str]]:
    """The ALAC tracks under folder, the other audio files' paths in it (FLACs, lossy files), and the .m4a files
    that cannot be read."""
    tracks: list[AlacTrack] = []
    others: list[str] = []
    unreadable: list[str] = []
    for path in album_files(folder, AUDIO_EXTENSIONS | LOSSY_EXTENSIONS):
        name = os.path.relpath(path, folder)
        if not path.lower().endswith(".m4a"):
            others.append(name)
        elif (info := _mp4_info(path)) is None:
            unreadable.append(name)
        elif info.codec == "alac":
            target = os.path.splitext(path)[0] + ".flac"
            tracks.append(AlacTrack(path, target, name, info.bits_per_sample, info.sample_rate, info.channels))
        else:
            others.append(name)
    return tracks, others, unreadable


def _named(names: list[str]) -> str:
    """The first few names, then how many more."""
    return ", ".join(names[:_NAMED]) + (f" and {len(names) - _NAMED} more" if len(names) > _NAMED else "")


def _refusal(album: str, staged: str, tracks: list[AlacTrack], others: list[str], unreadable: list[str]) -> str | None:
    """Why the ALAC files cannot be converted, or None."""
    if cfg.directory.protects(staged):  # Never so: staged_source copies such an album. Kept for other callers.
        return f"Not converting the ALAC files in {staged}: it is or holds a library_dirs entry."
    if unreadable:
        return (
            f"{album} holds ALAC files, and .m4a files salmon cannot read ({_named(unreadable)}): "
            "fix or remove them and run salmon again."
        )
    if lossy := [name for name in others if not name.lower().endswith(".flac")]:
        return (
            f"{album} holds ALAC files and lossy files ({_named(lossy)}): salmon converts ALAC to FLAC, and an upload "
            "holds one format. Move the lossy files out of the album and run salmon again."
        )
    if odd := next((track for track in tracks if track.bits not in _BIT_DEPTHS), None):
        return f"{odd.name} is {odd.bits}-bit ALAC: salmon converts only 16-bit and 24-bit ALAC to FLAC."
    taken: dict[str, AlacTrack] = {}
    for track in tracks:
        flac = os.path.relpath(track.target, staged)
        if os.path.lexists(track.target):
            return (
                f"{flac} is already in {album}, where salmon would convert {track.name} to: "
                "move one of them out of the album and run salmon again."
            )
        if (other := taken.get(key := os.path.normcase(track.target).lower())) is not None:
            return (
                f"{other.name} and {track.name} would both be converted to {flac}: "
                "rename one of them and run salmon again."
            )
        taken[key] = track
    return None


async def _convert_all(tracks: list[AlacTrack], album: str | None) -> str | None:
    """Convert every track, then remove the .m4a files; why it stopped, if it did.

    Every FLAC is verified before any .m4a is removed, so a failure leaves every .m4a in place and no FLAC behind.

    Args:
        tracks: The tracks to convert.
        album: The album folder when the tracks are in it, None when they are in a copy of it.
    """

    failed = False

    async def convert(_source: str, index: int) -> str | None:
        nonlocal failed
        if failed:  # Every FLAC goes once one track fails: the tracks not started yet are not converted.
            return None
        track = tracks[index]
        try:
            await anyio.to_thread.run_sync(_convert, track)
        except ConversionError as error:
            failed = True
            return f"Could not convert {track.name} to FLAC: {error}. Nothing was converted."
        return None

    done = False
    try:
        errors = await process_files([track.source for track in tracks], convert, "Converting ALAC to FLAC")
        if error := next((error for error in errors if error), None):
            return error
        done = True
    finally:
        if not done:
            for track in tracks:
                _remove(track.target)
    kept: list[str] = []
    for track in tracks:
        try:
            os.remove(track.source)
        except OSError as error:
            kept.append(f"{track.name} ({error.strerror})")
    if not kept:
        return None
    if album is None:
        # The copy goes when the run ends, its FLACs with it: the album's own .m4a files are the only ones left.
        return (
            f"Converted the ALAC files to FLAC in the copy salmon works on, but could not remove {', '.join(kept)} "
            "there. The album is unchanged: keep its .m4a files, fix what stopped the removal and run salmon again."
        )
    return (
        f"Converted the ALAC files in {album} to FLAC, but could not remove {', '.join(kept)}: "
        f"remove {'it' if len(kept) == 1 else 'them'} and run salmon again."
    )


def _remove(path: str) -> None:
    """Remove a FLAC this run wrote, if it is there."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        click.secho(f"Could not remove {path}: {error}", fg="yellow")


def _convert(track: AlacTrack) -> None:
    """Write track's FLAC, copy its tags and cover, and check it holds the ALAC's samples.

    Raises:
        ConversionError: Saying what went wrong.
    """
    digest = _encode(track)
    try:
        _copy_tags(track.source, track.target)
        info = FLAC(track.target).info
    except (MutagenError, OSError) as error:
        raise ConversionError(f"could not tag its FLAC ({error})") from error
    if (info.bits_per_sample, info.sample_rate, info.channels) != (track.bits, track.sample_rate, track.channels):
        raise ConversionError(
            f"its FLAC is {info.bits_per_sample}-bit {info.sample_rate} Hz, {info.channels} channels, where the ALAC "
            f"is {track.bits}-bit {track.sample_rate} Hz, {track.channels} channels"
        )
    check = hashlib.sha256()
    for chunk in _pcm_chunks(track.target, track.bits, track.channels):
        check.update(chunk)
    if check.digest() != digest:
        raise ConversionError("its FLAC does not decode to the same samples")


def _encode(track: AlacTrack) -> bytes:
    """Encode track's samples into its FLAC with the flac binary; the SHA-256 of the samples given to it."""
    command = [
        "flac",
        f"-{cfg.upload.compression.flac_compression_level}",
        "-V",
        "-s",
        "--force-raw-format",
        "--endian=little",
        "--sign=signed",
        f"--channels={track.channels}",
        f"--bps={track.bits}",
        f"--sample-rate={track.sample_rate}",
        "-o",
        track.target,
        "-",
    ]
    digest = hashlib.sha256()
    # A file, not a pipe, for flac's messages: a full pipe nobody reads would block it while the samples go in.
    with tempfile.TemporaryFile() as messages:
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=messages)
        except OSError as error:
            raise ConversionError(f"could not run flac ({error})") from error
        stdin = cast("IO[bytes]", process.stdin)
        stopped = False
        try:
            for chunk in _pcm_chunks(track.source, track.bits, track.channels):
                digest.update(chunk)
                stdin.write(chunk)
        except BrokenPipeError:  # flac stopped reading: its messages say why.
            stopped = True
        except BaseException:
            process.kill()
            raise
        finally:
            # Flushing what is left fails once flac is gone; the pipe is closed all the same.
            with contextlib.suppress(BrokenPipeError):
                stdin.close()
            returncode = process.wait()
        if stopped or returncode != 0:
            messages.seek(0)
            text = messages.read().decode(errors="replace").strip()
            raise ConversionError(text or f"flac exited with code {returncode}")
    return digest.digest()


def _pcm_chunks(path: str, bits: int, channels: int) -> Iterator[bytes]:
    """Decode the audio file at path into signed little-endian PCM of `bits` bits, channels interleaved, as flac's
    raw input takes it.

    FFmpeg decodes 24-bit ALAC and FLAC to 32-bit samples, the 24 bits in the high bytes; they are taken back down.

    Raises:
        ConversionError: If the file cannot be decoded, or its samples are not of that depth and channel count.
    """
    # Imported here: av and numpy slow down every salmon start (#367).
    import av
    import av.audio.frame

    try:
        with av.open(path) as container:
            stream = container.streams.audio[0]
            for frame in container.decode(stream):
                if not isinstance(frame, av.audio.frame.AudioFrame):
                    continue
                yield _packed(frame, bits, channels)
    except (av.FFmpegError, IndexError) as error:
        raise ConversionError(f"could not decode {os.path.basename(path)} ({error})") from error


def _packed(frame: "av.audio.frame.AudioFrame", bits: int, channels: int) -> bytes:
    """A decoded frame's samples as raw PCM of `bits` bits (see _pcm_chunks)."""
    import numpy as np

    if frame.layout.nb_channels != channels:
        raise ConversionError(f"decodes to {frame.layout.nb_channels} channels, not {channels}")
    samples = frame.to_ndarray()
    # Planar: one row per channel. Packed: one row, the channels already interleaved.
    interleaved = samples.T.reshape(-1) if frame.format.is_planar else samples.reshape(-1)
    name = frame.format.name.rstrip("p")
    if name == "s16" and bits == 16:
        return interleaved.astype("<i2").tobytes()
    if name == "s32" and bits == 24:
        wide = interleaved.astype(np.int32)
        if np.any(wide & 0xFF):
            raise ConversionError("holds more than 24 bits per sample")
        # Each sample shifted back down, written as its low three bytes, little-endian.
        return (wide >> 8).astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    raise ConversionError(f"decodes to {frame.format.name} samples, not {bits}-bit")


def _copy_tags(source: str, target: str) -> None:
    """Copy the tags of the .m4a at source named in _TEXT_TAGS and _NUMBER_TAGS, and its first cover if it is a JPEG
    or PNG, into the FLAC at target."""
    tags = MP4(source).tags or {}
    flac = FLAC(target)
    for atom, field in _TEXT_TAGS.items():
        values = [_text(value) for value in tags.get(atom) or []]
        if values := [value for value in values if value]:
            flac[field] = values
    for atom, (number_field, total_field) in _NUMBER_TAGS.items():
        pairs = tags.get(atom) or []
        if not pairs:
            continue
        number, total = (tuple(pairs[0]) + (0, 0))[:2]
        if number:
            flac[number_field] = str(number)
        if total:
            flac[total_field] = str(total)
    covers = tags.get("covr") or []
    if covers and (mime := _cover_mime(covers[0])):
        picture = Picture()
        picture.type = PictureType.COVER_FRONT
        picture.mime = mime
        picture.data = bytes(covers[0])
        flac.add_picture(picture)
    flac.save(padding=get_8kib_padding)


def _text(value: object) -> str:
    """A tag value as text: freeform atoms hold bytes."""
    if isinstance(value, MP4FreeForm):
        encoding = "utf-16-be" if value.dataformat == AtomDataType.UTF16 else "utf-8"
        return bytes(value).decode(encoding, errors="replace").strip()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def _cover_mime(cover: MP4Cover) -> str | None:
    """The MIME type of a cover atom's image, from its format or else its first bytes; None if neither says."""
    if mime := _COVER_MIMES.get(cover.imageformat):
        return mime
    data = bytes(cover)
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None
