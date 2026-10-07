"""What `salmon check all` concludes from each check's result: a row of OK, WARN, BLOCK or INFO.

Ported from chodeus's fork (`checks/preflight.py`), with these verdicts: a 16bit file is out of the upconvert
check's scope (INFO), an undetectable source is only INFO, and a rip log whose checksum fails BLOCKs.

Pure functions on what checks/album.py gathered, so each verdict is tested without audio or a tracker.
"""

import html
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import asyncclick as click

from salmon.checks.high_rate import sixteen_bit_above_48khz
from salmon.checks.integrity import IntegrityResult, md5_unset_summary
from salmon.checks.source import DetectedSource
from salmon.uploader.dupe_checker import describe_torrent, held_in_group
from salmon.uploader.spectrals import FREQUENCY_HEADINGS

Verdict = Literal["OK", "WARN", "BLOCK", "INFO"]

STANDARD_SAMPLE_RATES = frozenset({44100, 48000, 88200, 96000, 176400, 192000})

# How many names a detail line or a row's notes list before saying how many more there are.
_NAMED = 3
_NOTES = 8


@dataclass(frozen=True)
class Row:
    verdict: Verdict
    check: str
    detail: str
    notes: tuple[str, ...] = ()


def _names(names: list[str]) -> str:
    shown = ", ".join(names[:_NAMED])
    return f"{shown} and {len(names) - _NAMED} more" if len(names) > _NAMED else shown


def _notes(lines: Iterable[str]) -> tuple[str, ...]:
    lines = [line for line in lines if line]
    if len(lines) > _NOTES:
        return (*lines[:_NOTES], f"... and {len(lines) - _NOTES} more.")
    return tuple(lines)


def _khz(rate: int) -> str:
    return f"{rate / 1000:g} kHz"


def source_row(detected: DetectedSource | None) -> Row:
    if detected is None:
        return Row("INFO", "Source", "The files do not prove a media source; salmon up asks for it.")
    return Row("OK", "Source", f"{detected.source}: {detected.reason}.")


def integrity_row(result: IntegrityResult) -> Row:
    details = _notes(click.unstyle(result.details).splitlines())
    if result.decode_failures:
        names = list(result.decode_failures)
        return Row("BLOCK", "Integrity", f"{len(names)} file(s) do not decode: {_names(names)}.", details)
    if not result.passed:
        problems = []
        if result.md5_unset:
            problems.append(f"{md5_unset_summary(len(result.md5_unset), result.checked)}, but the audio decodes")
        if details:
            problems.append("the checker reports damage in a file that still decodes")
        return Row("WARN", "Integrity", "; ".join(problems) + ". salmon up offers to sanitize a copy.", details)
    if result.concerns:
        concerns = _notes(click.unstyle(concern) for concern in result.concerns)
        return Row(
            "WARN", "Integrity", f"Every file decodes, with {len(result.concerns)} note(s) from the checker.", concerns
        )
    return Row("OK", "Integrity", f"All {result.checked} file(s) decode.")


def mqa_row(results: list[dict[str, Any]]) -> Row:
    """results: {"file", "detected"} for each FLAC file."""
    if not results:
        return Row("INFO", "MQA", "No FLAC file to check.")
    hits = [r["file"] for r in results if r["detected"]]
    if hits:
        return Row(
            "BLOCK", "MQA", f"MQA in {len(hits)} of {len(results)} file(s): {_names(hits)}. RED and OPS refuse MQA."
        )
    return Row("OK", "MQA", f"No MQA in {len(results)} FLAC file(s).")


def upconvert_row(results: list[dict[str, Any]]) -> Row:
    """results, for each FLAC file: {"file", "upconverted", "wasted_bits", "bitdepth"}, or {"file", "not_applicable"}
    for a 16bit one, or {"file", "error"}."""
    if not results:
        return Row("INFO", "Upconvert", "No FLAC file to check.")
    tested = [r for r in results if "not_applicable" not in r]
    if not tested:
        return Row("INFO", "Upconvert", "16bit files: out of scope, the check is for 24bit files.")
    upconverted = [r for r in tested if r.get("upconverted")]
    if upconverted:
        notes = _notes(f"{r['file']}: {r['wasted_bits']} of {r['bitdepth']} bits wasted" for r in upconverted)
        return Row(
            "BLOCK",
            "Upconvert",
            f"{len(upconverted)} of {len(tested)} 24bit file(s) look upconverted from a lower bit depth.",
            notes,
        )
    errors = [r for r in tested if "error" in r]
    if errors:
        return Row(
            "WARN",
            "Upconvert",
            f"Could not check {len(errors)} of {len(tested)} file(s): {errors[0]['error']}",
            _notes(f"{r['file']}: {r['error']}" for r in errors),
        )
    return Row("OK", "Upconvert", f"No sign of upconversion in {len(tested)} 24bit file(s).")


def log_rows(source: str | None, logs: list[dict[str, Any]]) -> list[Row]:
    """A row per rip log, for a CD: the detected source, or a log in the folder when no source was detected.

    logs, for each log: {"file", "error"} when it could not be read, else {"file", "score" (None when cambia
    gave none), "checksum" ("Match", "Mismatch" or "Unknown"), "crcs" ("match", "mismatch", or why they were not
    checked)}.
    """
    if source not in (None, "CD"):
        return [Row("INFO", "Rip log", f"Not a CD release ({source}).")]
    if not logs:
        if source == "CD":
            return [Row("WARN", "Rip log", "A CD with no rip log.")]
        return [Row("INFO", "Rip log", "No rip log, and the files do not show a CD source.")]
    rows = []
    for log in logs:
        name = log["file"]
        if "error" in log:
            rows.append(Row("WARN", "Rip log", f"{name}: could not be read ({log['error']})."))
            continue
        if log["checksum"] == "Mismatch":
            rows.append(Row("BLOCK", "Rip log", f"{name}: its checksum does not match, so the log was edited."))
            continue
        issues = []
        if log["checksum"] == "Unknown":
            issues.append("no checksum to verify it (EAC signs its logs from 1.0 beta 3)")
        if log["score"] is None:
            issues.append("no score")
        elif log["score"] < 100:
            issues.append(f"score {log['score']}/100")
        if log["crcs"] == "mismatch":
            issues.append("the audio does not match its CRCs")
        elif log["crcs"] != "match":
            issues.append(f"CRCs not checked: {log['crcs']}")
        if issues:
            rows.append(Row("WARN", "Rip log", f"{name}: {'; '.join(issues)}."))
        else:
            rows.append(Row("OK", "Rip log", f"{name}: score 100/100, checksum and CRCs match."))
    return rows


def tag_issues_row(messages: list[str]) -> Row:
    if messages:
        return Row("WARN", "Tags", f"{len(messages)} tag issue(s).", _notes(messages))
    return Row("OK", "Tags", "No ID3 tag in a FLAC, uncompressed FLAC or dual ID3 MP3.")


def sample_rate_row(audio_info: Mapping[str, dict[str, Any]]) -> Row:
    odd = {
        name: info["sample rate"]
        for name, info in audio_info.items()
        if info.get("sample rate") not in STANDARD_SAMPLE_RATES
    }
    if odd:
        rates = ", ".join(_khz(rate) if rate else "unknown" for rate in sorted(set(odd.values()), key=lambda r: r or 0))
        return Row(
            "WARN",
            "Sample rate",
            f"Non-standard sample rate ({rates}) in {len(odd)} file(s), which may be rejected.",
            _notes(odd),
        )
    rates = ", ".join(_khz(rate) for rate in sorted({info["sample rate"] for info in audio_info.values()}))
    return Row("OK", "Sample rate", f"Standard: {rates}." if rates else "No file read.")


def sixteen_bit_rows(audio_info: Mapping[str, dict[str, Any]], rules: Mapping[str, str]) -> list[Row]:
    """rules: each tracker's TagRules.sixteen_bit_above_48khz, by site code."""
    check = "16bit above 48 kHz"
    files = list(sixteen_bit_above_48khz(audio_info))
    if not files:
        return [Row("OK", check, "No 16bit file above 48 kHz.")]
    found = f"{len(files)} 16bit file(s) above 48 kHz"
    notes = _notes(files)
    rows = []
    for code, rule in rules.items():
        if rule == "refused":
            rows.append(Row("BLOCK", f"{check} ({code})", f"{found}: {code} refuses them.", notes))
        elif rule == "trumpable":
            rows.append(Row("WARN", f"{check} ({code})", f"{found}: {code} can trump them.", notes))
    return rows or [Row("INFO", check, f"{found}; no rule known for {', '.join(rules) or 'these trackers'}.", notes)]


def path_rows(paths: list[str], limits: Mapping[str, int]) -> list[Row]:
    """paths: every in-torrent path (folder, sub-folders, file name). limits: each tracker's, by site code."""
    longest = max((len(p) for p in paths), default=0)
    if all(longest <= limit for limit in limits.values()):
        within = ", ".join(f"{code} {limit}" for code, limit in limits.items())
        return [Row("OK", "Path length", f"Longest path {longest} characters, within {within}.")]
    rows = []
    for code, limit in limits.items():
        over = [p for p in paths if len(p) > limit]
        if over:
            rows.append(
                Row(
                    "WARN",
                    f"Path length ({code})",
                    f"{len(over)} path(s) over {code}'s {limit} characters, the longest {longest}.",
                    _notes(f"{len(p)}: {p}" for p in sorted(over, key=len, reverse=True)),
                )
            )
        else:
            rows.append(Row("OK", f"Path length ({code})", f"Longest path {longest} characters, within {limit}."))
    return rows


def provenance_row(provenance: dict[str, Any]) -> Row:
    if not provenance["files"]:
        return Row("INFO", "Provenance", "Not checked: the tags of a file could not be read.")
    contradictions = provenance["contradictions"]
    if contradictions:
        return Row(
            "WARN", "Provenance", f"{len(contradictions)} tag marker(s) the audio contradicts.", _notes(contradictions)
        )
    vendors = ", ".join(provenance["vendors"]) or "not in the tags"
    return Row("OK", "Provenance", f"No marker the audio contradicts. Encoder: {vendors}.")


def frequency_row(level: str | None, notes: list[str], *, error: str = "") -> Row:
    """level and notes: frequency.assess's, or None when no lossless file was analysed."""
    check = "Frequency analysis"
    if error:
        return Row("INFO", check, f"Failed, so it says nothing about this release: {error}")
    if level is None:
        return Row("INFO", check, "No lossless file: a lossy file carries a lossy encoder's marks.")
    heading = FREQUENCY_HEADINGS[level][0].capitalize() + "."
    verdicts: dict[str, Verdict] = {"suspect": "WARN", "look": "WARN", "ok": "OK"}
    verdict = verdicts.get(level, "INFO")
    return Row(verdict, check, heading, _notes(notes))


def do_not_upload_row(tracker: str, reason: str | None) -> Row:
    if reason:
        return Row("BLOCK", f"Do-Not-Upload ({tracker})", reason)
    return Row("OK", f"Do-Not-Upload ({tracker})", f"Not on {tracker}'s Do-Not-Upload list.")


def _group_name(group: dict[str, Any]) -> str:
    artist = html.unescape(str(group.get("artist") or ""))
    name = html.unescape(str(group.get("groupName") or group.get("groupId")))
    year = f" ({group['groupYear']})" if group.get("groupYear") else ""
    return f"{artist} - {name}{year}" if artist else f"{name}{year}"


def dupe_row(tracker: str, release: dict[str, Any], results: list[dict[str, Any]]) -> Row:
    """results: the tracker's browse results for the release's search strings.

    release: as up's rls_data: source, format, encoding, year and edition_title name its edition.
    """
    check = f"Dupe ({tracker})"
    if not results:
        return Row("OK", check, f"No group found on {tracker}.")
    groups = [_group_name(group) for group in results]
    wanted = [str(release.get(key) or "") for key in ("source", "format", "encoding")]
    if not all(wanted):
        return Row(
            "WARN",
            check,
            f"{len(results)} group(s) found; this release's source or encoding is not known from the files, so "
            "check them for it by hand.",
            _notes(groups),
        )
    held = [
        f"{_group_name(group)}: {describe_torrent(torrent, group.get('group') or {})}"
        for group in results
        for torrent in held_in_group(group, release)
    ]
    if held:
        return Row("WARN", check, f"{tracker} already has this edition's {' '.join(wanted)}.", _notes(held))
    return Row(
        "OK",
        check,
        f"{len(results)} group(s) found, none with this edition's {' '.join(wanted)}.",
        _notes(groups),
    )


def dupe_not_searched_row(tracker: str, why: str) -> Row:
    return Row("INFO", f"Dupe ({tracker})", f"Not searched: {why}")


def dupe_error_row(tracker: str, error: str) -> Row:
    return Row("WARN", f"Dupe ({tracker})", f"Could not search {tracker}: {error}")
