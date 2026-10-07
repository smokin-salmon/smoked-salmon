"""A plain-text report of what a release is, to paste into a tracker help thread (`salmon check all --report`).

Laid out as chodeus's fork's (`checks/report.py`): what it claims to be, how it was encoded, its real numbers,
the frequency analysis, then the verdicts. It names the album folder and no other path, and holds nothing from
the config: no credential, no tracker URL.
"""

from typing import Any

from salmon.checks.album import AlbumChecks


def _format_line(info: dict[str, Any]) -> str:
    precision = info.get("precision")
    rate = info.get("sample rate")
    depth = f"{precision}-bit" if precision else "lossy"
    khz = f"{rate / 1000:g} kHz" if rate else "unknown rate"
    channels = {1: "mono", 2: "stereo"}.get(info.get("channels") or 0) or (
        f"{info['channels']}ch" if info.get("channels") else "unknown channels"
    )
    bitrate = info.get("bit rate")
    kbps = f"{bitrate / 1000:.0f} kbps" if bitrate else "unknown bitrate"
    return f"{depth} / {khz} {channels}, {kbps}"


def build_report(checks: AlbumChecks) -> str:
    """The report, from what check_album gathered."""
    # numpy: loaded with the analysis, which has run by now.
    from salmon.uploader.frequency import describe, summarize

    audio_info, provenance = checks.audio_info, checks.provenance
    lines = [checks.folder, "", "1. Lossless or lossy"]
    depths = sorted({info["precision"] for info in audio_info.values() if info.get("precision")})
    if depths:
        lines.append(f"   Lossless ({', '.join(f'{depth}-bit' for depth in depths)}).")
    else:
        lines.append("   Lossy.")
    lines += [f"   Claim the audio contradicts: {note}" for note in provenance.get("contradictions", [])]

    lines += ["", "2. Format and encoder"]
    vendors = provenance.get("vendors") or []
    lines.append(f"   Encoder: {', '.join(vendors)}" if vendors else "   Encoder: not recorded in the tags.")
    lines += [f"   Tag marker: {marker}" for marker in provenance.get("markers", [])[:5]]

    lines += ["", "3. Bitrate, sample rate and bit depth"]
    lines += [f"   {name}: {_format_line(info)}" for name, info in audio_info.items()]

    lines += ["", "4. Frequency analysis (averaged spectrum per track)"]
    if checks.spectra:
        lines += [f"   {describe(result)}" for result in checks.spectra]
        lines += [f"   {note}" for note in summarize(checks.spectra)[1]]
    else:
        lines.append("   Not run: no lossless file.")

    lines += ["", "5. Checks (salmon check all)"]
    for row in checks.rows:
        lines.append(f"   {row.verdict:<5} {row.check}: {row.detail}")
        lines += [f"         {note}" for note in row.notes]

    lines += ["", "6. About the release", "   "]
    return "\n".join(lines)
