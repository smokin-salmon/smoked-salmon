"""Every check salmon has, on one album folder, for `salmon check all` (#541).

Ported from chodeus's fork (`checks/album.py`, `checks/preflight.py`). Advisory only: salmon up runs its own
checks and stays the authority.

Reads only. Nothing here writes into the folder, which may be in library_dirs: no sanitizing, no ID3
stripping, no spectrals or plots. No tracker is contacted unless trackers are named, and then only for
salmon up's dupe search: the same browse requests, through the tracker client, with no site log.
"""

import os
import re
from dataclasses import dataclass
from typing import Any

import anyio.to_thread
import asyncclick as click
import cambia
from mutagen import File as MutagenFile

import salmon.trackers
from salmon.checks import verdicts
from salmon.checks.do_not_upload import LISTS, Candidate, do_not_upload_reason
from salmon.checks.integrity import check_integrity
from salmon.checks.logs import log_checksum, log_score, parse_log, verify_log_crcs
from salmon.checks.mqa import check_mqa
from salmon.checks.provenance import gather_provenance, lossless_depth
from salmon.checks.source import DetectedSource, detect_source
from salmon.checks.tag_rules import find_tag_issues, in_torrent_path
from salmon.checks.upconverts import check_upconvert
from salmon.checks.verdicts import Row
from salmon.common.files import get_audio_files, process_files
from salmon.errors import (
    CRCMismatchError,
    LogCheckSkipped,
    RequestError,
    UpconvertCheckError,
    UpconvertCheckNotApplicable,
)
from salmon.tagger.pre_data import construct_artists_li, parse_format, parse_title
from salmon.tagger.tagfile import TagFile
from salmon.uploader.dupe_checker import generate_dupe_check_searchstrs, get_search_results

# Checked without -t: the trackers whose rules salmon knows.
DEFAULT_TRACKERS = ("RED", "OPS")


@dataclass
class AlbumChecks:
    """What check all found: its rows, and what the report is made from."""

    folder: str
    rows: list[Row]
    audio_info: dict[str, dict[str, Any]]
    provenance: dict[str, Any]
    spectra: list[Any]

    @property
    def blocking(self) -> list[Row]:
        return [row for row in self.rows if row.verdict == "BLOCK"]


def _audio_info(path: str) -> dict[str, dict[str, Any]]:
    """gather_audio_info's, leaving out a file mutagen cannot read: the integrity check reports that one.

    The precision is set for a lossless file only: an AAC stream reports 16 bits per sample too.
    """
    info = {}
    for filename in get_audio_files(path, True):
        try:
            mut = MutagenFile(os.path.join(path, filename))
        except Exception:
            continue
        if mut is None:
            continue
        stream = mut.info
        info[filename] = {
            "channels": stream.channels,
            "sample rate": stream.sample_rate,
            "bit rate": stream.bitrate,
            "precision": lossless_depth(stream),
        }
    return info


async def _mqa(path: str, flacs: list[str]) -> list[dict[str, Any]]:
    async def one(filename: str, _: int) -> dict[str, Any]:
        return {"file": filename, "detected": await check_mqa(os.path.join(path, filename))}

    return await process_files(flacs, one, "Checking for MQA")


async def _upconverts(path: str, flacs: list[str]) -> list[dict[str, Any]]:
    async def one(filename: str, _: int) -> dict[str, Any]:
        try:
            result = await check_upconvert(os.path.join(path, filename))
        except UpconvertCheckNotApplicable as e:
            return {"file": filename, "not_applicable": str(e)}
        except UpconvertCheckError as e:
            return {"file": filename, "error": str(e)}
        return {
            "file": filename,
            "upconverted": result.is_upconverted,
            "wasted_bits": result.wasted_bits,
            "bitdepth": result.bitdepth,
        }

    return await process_files(flacs, one, "Checking for upconverts")


def _log_paths(path: str) -> list[str]:
    return sorted(
        os.path.join(root, name)
        for root, _dirs, files in os.walk(path)
        for name in files
        if name.lower().endswith(".log")
    )


async def _log(logpath: str, path: str) -> dict[str, Any]:
    """Score, checksum and CRCs of one rip log, as check_log_cambia reads them, without printing them."""
    name = os.path.relpath(logpath, path)
    try:
        output = await anyio.to_thread.run_sync(parse_log, logpath)
    except (LogCheckSkipped, OSError) as e:
        return {"file": name, "error": str(e).replace(logpath, name)}
    checksum = log_checksum(output)
    result = {"file": name, "score": log_score(output), "checksum": checksum.name, "crcs": "match"}
    if checksum == cambia.Integrity.Mismatch:
        return result
    try:
        await verify_log_crcs(output, logpath, path)
    except CRCMismatchError:
        result["crcs"] = "mismatch"
    except LogCheckSkipped as e:
        result["crcs"] = str(e)
    except Exception as e:
        result["crcs"] = f"the audio could not be verified ({e})"
    return result


def _in_torrent_paths(path: str) -> list[str]:
    folder = os.path.basename(path)
    return [
        in_torrent_path(folder, os.path.relpath(os.path.join(root, name), path))
        for root, _dirs, files in os.walk(path)
        for name in files
    ]


def _release(path: str, audio_info: dict[str, dict[str, Any]], source: str | None) -> dict[str, Any]:
    """The release as salmon up's rls_data would start, from the tags of the files mutagen reads; {} when it reads
    none. A lossy release's encoding stays None: up asks for it."""
    tags = {name: TagFile(os.path.join(path, name)) for name in audio_info}
    if not tags:
        return {}
    first_name, first = next(iter(tags.items()))
    title, edition_title = parse_title(first.album) if first.album else (None, None)
    year = re.search(r"\d{4}", str(first.date or ""))
    release_format = parse_format(first_name)
    precisions = {info.get("precision") for info in audio_info.values()}
    encoding = None
    if release_format == "FLAC":
        encoding = "24bit Lossless" if 24 in precisions else "Lossless" if 16 in precisions else None
    return {
        "artists": construct_artists_li(tags),
        "title": title,
        "edition_title": edition_title,
        "year": year[0] if year else None,
        "label": first.label,
        "catno": first.catno,
        "source": source,
        "format": release_format,
        "encoding": encoding,
    }


async def _dupe_row(tracker: str, release: dict[str, Any]) -> Row:
    """salmon up's dupe search on one tracker: index, then one browse per search string."""
    searchstrs = generate_dupe_check_searchstrs(release["artists"], release["title"], release["catno"])
    if not searchstrs:
        return verdicts.dupe_not_searched_row(tracker, "no search string from the tags.")
    site = salmon.trackers.get_class(tracker)()
    try:
        results = await get_search_results(site, searchstrs)
    except RequestError as e:
        return verdicts.dupe_error_row(tracker, str(e))
    except (KeyError, TypeError) as e:
        # An answer without the fields a search returns: say so in the row rather than stop every check.
        return verdicts.dupe_error_row(tracker, f"unexpected answer ({e!r})")
    finally:
        await site.close()
    try:
        return verdicts.dupe_row(tracker, release, results)
    except (AttributeError, KeyError, TypeError) as e:
        # A result that is not a group as a search gives one, such as null or a group whose torrents are not a list.
        return verdicts.dupe_error_row(tracker, f"unexpected answer ({e!r})")


def _without_parents(path: str, rows: list[Row]) -> list[Row]:
    """The rows naming no folder above the album: a checker prints the paths it was given."""
    parent = os.path.dirname(path) + os.sep

    def clean(text: str) -> str:
        return text.replace(parent, "")

    return [Row(row.verdict, row.check, clean(row.detail), tuple(clean(n) for n in row.notes)) for row in rows]


async def check_album(path: str, trackers: list[str]) -> AlbumChecks:
    """Run every check on the album folder.

    Args:
        path: The album folder, absolute.
        trackers: The site codes named with -t: their dupe search runs, and their rules apply. None named
            applies RED's and OPS's rules and contacts no tracker.

    Raises:
        click.ClickException: If the folder holds no audio file.
    """
    audio_files = get_audio_files(path, True)
    if not audio_files:
        raise click.ClickException(f"No audio file in {os.path.basename(path)}.")
    audio_info = await anyio.to_thread.run_sync(_audio_info, path)
    detected: DetectedSource | None = await anyio.to_thread.run_sync(detect_source, path)
    source = detected.source if detected else None
    flacs = [name for name in audio_files if name.lower().endswith(".flac")]

    rows = [verdicts.source_row(detected)]
    try:
        rows.append(verdicts.integrity_row(await check_integrity(path)))
    except click.Abort:
        rows.append(Row("INFO", "Integrity", "No FLAC or MP3 file to check."))
    rows.append(verdicts.mqa_row(await _mqa(path, flacs)))
    rows.append(verdicts.upconvert_row(await _upconverts(path, flacs)))
    log_paths = _log_paths(path) if source in (None, "CD") else []
    rows += verdicts.log_rows(source, [await _log(logpath, path) for logpath in log_paths])
    rows.append(verdicts.tag_issues_row(await anyio.to_thread.run_sync(find_tag_issues, path, audio_info)))
    rows.append(verdicts.sample_rate_row(audio_info))

    rule_trackers = trackers or list(DEFAULT_TRACKERS)
    rules = {code: salmon.trackers.tracker_classes[code].TAG_RULES for code in rule_trackers}
    rows += verdicts.sixteen_bit_rows(audio_info, {code: rule.sixteen_bit_above_48khz for code, rule in rules.items()})
    rows += verdicts.path_rows(_in_torrent_paths(path), {code: rule.max_path_length for code, rule in rules.items()})

    provenance = await anyio.to_thread.run_sync(gather_provenance, path)
    rows.append(verdicts.provenance_row(provenance))

    # numpy, PyAV and Pillow: loaded when the analysis runs.
    from salmon.uploader import frequency

    lossless = [name for name, info in audio_info.items() if info.get("precision")]
    spectra: list[Any] = []
    if not lossless:
        rows.append(verdicts.frequency_row(None, []))
    else:
        try:
            # No plot path: nothing is drawn, so nothing is written.
            spectra = await frequency.generate_frequency_plots(path, lossless, {})
        except Exception as e:
            rows.append(verdicts.frequency_row(None, [], error=repr(e)))
        else:
            rows.append(verdicts.frequency_row(*frequency.assess(spectra)))

    release = await anyio.to_thread.run_sync(_release, path, audio_info, source)
    for code in rule_trackers:
        # The lists ship with salmon: checking them sends nothing.
        listed = None
        if code in LISTS and release.get("title"):
            listed = do_not_upload_reason(code, Candidate.from_metadata(release))
            rows.append(verdicts.do_not_upload_row(code, listed))
        elif code in LISTS:
            rows.append(Row("INFO", f"Do-Not-Upload ({code})", "Not checked: no album title in the tags."))
        if code not in trackers:
            continue
        if listed:
            rows.append(verdicts.dupe_not_searched_row(code, f"{code}'s Do-Not-Upload list forbids the release."))
        elif not (release.get("title") and release.get("artists")):
            rows.append(verdicts.dupe_not_searched_row(code, "no album title or artist in the tags."))
        else:
            rows.append(await _dupe_row(code, release))

    return AlbumChecks(os.path.basename(path), _without_parents(path, rows), audio_info, provenance, spectra)
