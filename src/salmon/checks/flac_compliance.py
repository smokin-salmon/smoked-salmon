"""RED minimum warn-only checks for FLAC 16/24-only uploads."""

import os

import asyncclick as click

ALLOWED_SAMPLE_RATES = {44100, 48000, 88200, 96000, 176400, 192000}
ALLOWED_BIT_DEPTHS = {16, 24}
LINEAGE_SOURCES = {"Vinyl", "SACD", "Soundboard", "Cassette", "DAT"}


def get_flac_warnings(audio_files, all_files, audio_info, source):
    """Pure check: return list of RED rule warnings (never raises)."""
    warnings = []
    flac_only = [f for f in audio_files if f.lower().endswith(".flac")]
    if len(flac_only) != len(audio_files):
        warnings.append("RED 2.1.1: non-FLAC audio found; you upload FLAC 16/24 only.")
    for f in audio_files:
        info = audio_info.get(f, {})
        depth = info.get("precision")
        rate = info.get("sample rate")
        if depth not in ALLOWED_BIT_DEPTHS:
            warnings.append(f"RED 2.1.1/2.11: {f} depth {depth}, expected 16 or 24.")
        if rate not in ALLOWED_SAMPLE_RATES:
            warnings.append(f"RED 2.11.2.2: {f} rate {rate}, expected 44.1/48/88.2/96/176.4/192.")
        if depth == 16 and rate is not None and rate > 48000:
            warnings.append(f"RED 2.11.2.6.2: {f} is 16-bit above 48 kHz.")
    depths = {audio_info.get(f, {}).get("precision") for f in audio_files}
    rates = {audio_info.get(f, {}).get("sample rate") for f in audio_files}
    if len(depths) > 1 or len(rates) > 1:
        warnings.append("RED 2.1.6.1: mixed depth/rate; must be uniform unless hybrid WEB.")
    if len(audio_files) == 1 and not any(f.lower().endswith(".cue") for f in all_files):
        warnings.append("RED 2.1.5: single-track rip should include cue where applicable.")
    for f in all_files:
        base = os.path.basename(f)
        if base.startswith(" ") or any(p.startswith(" ") for p in f.split(os.sep)):
            warnings.append(f"RED 2.3.20: leading space in '{f}'.")
            break
    if source in LINEAGE_SOURCES:
        warnings.append(f"RED 2.3.9: {source} rips need lineage in desc or .txt/.log.")
    return warnings


def find_id3_in_flac(path):
    """Return FLAC files whose header is not b'fLaC' (likely ID3, RED 2.2.10.8)."""
    bad = []
    for root, _, files in os.walk(path):
        for f in files:
            if not f.lower().endswith(".flac"):
                continue
            fp = os.path.join(root, f)
            try:
                with open(fp, "rb") as fh:
                    if fh.read(4) != b"fLaC":
                        bad.append(os.path.relpath(fp, path))
            except OSError:
                continue
    return bad


async def warn_flac_compliance(path, audio_info, source):
    """Secho all FLAC minimum warnings; never aborts (warn-only)."""
    audio_files = sorted(audio_info.keys())
    all_files = []
    for root, _, files in os.walk(path):
        for f in files:
            all_files.append(os.path.relpath(os.path.join(root, f), path))
    for w in get_flac_warnings(audio_files, all_files, audio_info, source):
        click.secho(f"FLAC check (RED): {w}", fg="yellow")
    for f in find_id3_in_flac(path):
        click.secho(f"FLAC check (RED 2.2.10.8): {f} has ID3 header; use Vorbis only.", fg="yellow")
    # ponytail: compression needs `flac` binary; remind, don't scan
    click.secho("FLAC check (RED 2.2.10.10): ensure FLACs compressed (level 8).", fg="yellow")
    if source == "CD" and not any(f.lower().endswith(".cue") for f in all_files):
        click.secho("FLAC check (RED 2.2.10.7): CD rip without cue is trumpable.", fg="yellow")


def demo():
    info = {
        "01 - T.flac": {"precision": 24, "sample rate": 96000},
        "02 - T.flac": {"precision": 24, "sample rate": 96000},
    }
    files = ["01 - T.flac", "02 - T.flac"]
    assert get_flac_warnings(files, files, info, "WEB") == []
    w = get_flac_warnings(["a.flac"], ["a.flac"], {"a.flac": {"precision": 16, "sample rate": 96000}}, "WEB")
    assert any("16-bit above" in x for x in w)
    w = get_flac_warnings(["a.flac"], [" a.flac"], {"a.flac": {"precision": 16, "sample rate": 44100}}, "CD")
    assert any("leading space" in x for x in w)
    w = get_flac_warnings(["a.flac"], ["a.flac"], {"a.flac": {"precision": 24, "sample rate": 96000}}, "Vinyl")
    assert any("lineage" in x for x in w)


if __name__ == "__main__":
    demo()
    print("flac_compliance demo ok")
