import argparse
import concurrent.futures as cf
import re
import shutil
import subprocess as sp
from collections.abc import Collection, Iterable, Iterator
from pathlib import Path

import asyncclick as click
from mutagen import flac, mp3
from mutagen.flac import VCFLACDict
from mutagen.id3 import APIC, TXXX, Frames

from salmon import cfg

# ######################################################
#
# Basic settings:
#
# Set the location of flac and lame programs:
lame_prog = "lame"
flac_prog = "flac"
#
# Regex pattern for input flac folder:
flac_folder_pattern = r"\[FLAC.*\]"
#
# Above pattern will be replaced by this text:
replace_folder_pattern_with = "[MP3 {qual}]"  # {qual} will be replaced by lame quality (V0, 320 etc.)
#
# Misc settings:
copy_embedded_pics = True  # True or False
copy_extensions = (".jpg", ".jpeg", ".png", ".pdf", ".txt")  # set empty parentheses to not copy extra files
default_lame_quality = (
    "320",
    "V0",
)  # This is used when -q option is not used in command line.
flac_tags_not_to_copy = ("encoder",)
lame_command_dict = {"V0": ("-V", "0", "--vbr-new"), "320": ("-b", "320", "-h")}
#
# Advanced settings
flac_options = ("-Vdsc",)
lame_options = ("--quiet", "--add-id3v2", "--ignore-tag-errors")
id3v2_version = 4  # 3 or 4
#
vorbis_to_id3_map = {
    "title": "TIT2",
    "album": "TALB",
    "artist": "TPE1",
    "albumartist": "TPE2",
    "album artist": "TPE2",
    "conductor": "TPE3",
    "remixer": "TPE4",
    "composer": "TCOM",
    "tracknumber": "TRCK",
    "discnumber": "TPOS",
    "date": "TDRC",
    "comment": "COMM",
    "genre": "TCON",
    "language": "TLAN",
    "key": "TKEY",
    "bpm": "TBPM",
    "publisher": "TPUB",
    "label": "TPUB",
    "isrc": "TSRC",
}
##############################################################

assert all(fr in Frames for fr in vorbis_to_id3_map.values())
folder_re = re.compile(flac_folder_pattern)


def flac_to_mp3(lame_qual: str, flac_path: str, mp3_path: Path):
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    flac_command = [flac_prog, *flac_options, flac_path]
    lame_command = [lame_prog, *lame_command_dict[lame_qual], *lame_options, "-", mp3_path]

    click.secho(f"Encoding: {mp3_path}", fg="cyan")
    p1 = sp.Popen(flac_command, stdout=sp.PIPE, stderr=sp.PIPE)
    p2 = sp.run(lame_command, stdin=p1.stdout, stderr=sp.PIPE)
    if p1.poll() and p1.stderr:
        err = p1.stderr.read().decode()
        if err:
            click.secho(err, fg="yellow")
    if p2.returncode:
        raise RuntimeError(p2.stderr.decode())


def get_id3_frame(tag_name: str, tag_value: list):
    if tag_name in vorbis_to_id3_map:
        frame_name = vorbis_to_id3_map[tag_name]
        frame_type = Frames[frame_name]

        return frame_type(encoding=3, text=tag_value)

    return TXXX(encoding=3, desc=tag_name, text=tag_value)


def copy_tags(tag_dict: dict, flac_thing: flac.FLAC, mp3_path: Path):
    mp3_thing = mp3.MP3(mp3_path)
    click.secho(f"     Copy tags: {mp3_path}", fg="cyan")

    if not mp3_thing.tags:
        mp3_thing.add_tags()
    if mp3_thing.tags is None:
        raise ValueError(f"Failed to create tags for MP3 file: {mp3_path}")
    for k, v in tag_dict.items():
        frame_to_add = get_id3_frame(k, v)
        mp3_thing.tags.add(frame_to_add)

    if copy_embedded_pics:
        for pic in flac_thing.pictures:
            mp3_thing.tags.add(APIC(encoding=3, mime=pic.mime, type=pic.type, desc=pic.desc, data=pic.data))

    mp3_thing.save(v1=0, v2_version=id3v2_version)


TOT_MAP = {
    "tracknumber": {"tracktotal", "totaltracks", "total tracks"},
    "discnumber": {"disctotal", "totaldiscs", "total discs"},
}


def prepare_tags(tags: dict[str, list]):
    for key in tags.copy():
        if key.startswith("replaygain") or key in flac_tags_not_to_copy:
            del tags[key]

    for tag, tots in TOT_MAP.items():
        if tag not in tags:
            continue
        used = tots & tags.keys()
        if not used:
            continue
        tot_vals = set()
        for t in used:
            tot_vals.add(int(tags[t][0]))
            del tags[t]

        if len(tot_vals) == 1:
            total = str(tot_vals.pop())
            nr = tags[tag][0]
            tags[tag] = [f"{nr}/{total}"]
        else:
            raise ValueError(f"conflicting values of {' and '.join(used)}")


def copy_extra_files(path: Path, out_dirs: Iterable[Path]):
    for p in path.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in copy_extensions:
            continue
        rel_name = p.relative_to(path)
        click.secho(f"Copy {rel_name}", fg="cyan")
        for op in out_dirs:
            (op / rel_name.parent).mkdir(parents=True, exist_ok=True)
            shutil.copy(p, op / rel_name)


def process_flac_file(flac_thing: flac.FLAC, tag_dict: dict, mp3_qual: str, mp3_file_path: Path):
    if flac_thing.info.channels > 2:
        raise ValueError(
            f"{flac_thing.filename} has {flac_thing.info.channels} channels. This can't be converted to mp3."
        )
    if flac_thing.filename is None:
        raise ValueError("FLAC file has no filename")
    flac_to_mp3(mp3_qual, str(flac_thing.filename), mp3_file_path)
    copy_tags(tag_dict, flac_thing, mp3_file_path)


def get_flac_n_tag_dict(flac_file_path: Path) -> tuple[flac.FLAC, dict[str, list[str]]]:
    """Get FLAC file and its tag dictionary.

    Args:
        flac_file_path: Path to the FLAC file.

    Returns:
        Tuple of (FLAC object, tag dictionary).

    Raises:
        ValueError: If FLAC file has no tags.
    """
    fl = flac.FLAC(flac_file_path)
    tags = fl.tags
    if tags is None:
        raise ValueError(f"FLAC file has no tags: {flac_file_path}")
    # For FLAC files, tags is VCommentDict which has as_dict() method
    if not isinstance(tags, VCFLACDict):
        raise ValueError(f"FLAC tags are not VCommentDict: {flac_file_path}")
    # VCommentDict.as_dict() returns dict[str, list[str]]
    tag_dict: dict[str, list[str]] = tags.as_dict()
    prepare_tags(tag_dict)
    return fl, tag_dict


def single_file(
    flac_file_path: Path, lame_qual: Iterable[str], out_dir: Path | None = None
) -> Iterator[tuple[flac.FLAC, dict, str, Path]]:
    flac_thing, tag_dict = get_flac_n_tag_dict(flac_file_path)
    mp3_p = flac_file_path.with_suffix(".mp3")
    if out_dir:
        mp3_p = out_dir / mp3_p.name

    for q in lame_qual:
        yield flac_thing, tag_dict, q, mp3_p.with_stem(f"{mp3_p.stem} [{q}]")


def folder_mode(
    album_path: Path, lame_qual: Iterable[str], mp3_album_dirs: dict
) -> Iterator[tuple[flac.FLAC, dict, str, Path]]:
    for flac_path in album_path.rglob("*.flac"):
        flac_thing, tag_dict = get_flac_n_tag_dict(flac_path)
        rel_mp3_name = flac_path.relative_to(album_path).with_suffix(".mp3")

        for q in lame_qual:
            mp3_file_path = mp3_album_dirs[q] / rel_mp3_name
            yield flac_thing, tag_dict, q, mp3_file_path


def mp3_dirs(album_dir: Path, lame_qual: Iterable[str], out_dir: Path | None) -> dict[str, Path]:
    if out_dir:
        return {q: out_dir for q in lame_qual}

    if folder_re.search(album_dir.name):
        dir_name = folder_re.sub(replace_folder_pattern_with, album_dir.name)
    else:
        click.secho("Pattern does not match album dir. Appending MP3 section instead of replacing", fg="yellow")
        dir_name = f"{album_dir.name}_{replace_folder_pattern_with}"

    out = album_dir.with_name(dir_name)

    return {q: out.with_name(out.name.replace("{qual}", q.lstrip("b"))) for q in lame_qual}


def transcode(flac_path: Path, lame_qual: Iterable[str], out_dir: Path | None):
    mp3_album_dirs = None
    if flac_path.is_dir():
        mp3_album_dirs = mp3_dirs(flac_path, lame_qual, out_dir)
        arg_gen = folder_mode(flac_path, lame_qual, mp3_album_dirs)
    elif flac_path.is_file():
        arg_gen = single_file(flac_path, lame_qual, out_dir)
    else:
        raise ValueError(f"What is this thing: {flac_path}")

    with cf.ThreadPoolExecutor(max_workers=cfg.upload.simultaneous_threads) as executor:
        futures = []
        for args in arg_gen:
            futures.append(executor.submit(process_flac_file, *args))

        for future in cf.as_completed(futures):
            if exc := future.exception():
                executor.shutdown(cancel_futures=True)
                raise exc

        if flac_path.is_dir() and mp3_album_dirs and copy_extensions:
            executor.submit(copy_extra_files, flac_path, mp3_album_dirs.values())


def copy_only(flac_file: Path, mp3_file: Path):
    flac_thing, tag_dict = get_flac_n_tag_dict(flac_file)
    copy_tags(tag_dict, flac_thing, mp3_file)


def parse_args() -> tuple[Path, Collection[str], Path | None, Path | None]:
    parser = argparse.ArgumentParser(prog="m3ercat", description="m3ercat: the Mp3 EncodeR that Copies All Tags")
    parser.add_argument(
        "flac_path", help="Album folder or single flac file. (must be single file in combination with -c/--copy_to)"
    )
    parser.add_argument(
        "-o",
        "--out",
        help="Output folder (Does not need to exist). When omitted, output will be created as sibling of the input.",
        metavar="",
    )
    parser.add_argument(
        "-q",
        action="append",
        choices=("b320", "b256", "b192", "b128", "V0", "V1", "V2", "V3", "V4"),
        help=f"Lame bitrate setting. Can be used multiple times. Defaults to {' and '.join(default_lame_quality)}.",
    )
    parser.add_argument(
        "-c", "--copy_to", help="Only copy tags, no transcoding. Must point to existing .mp3 file", metavar=""
    )
    args = parser.parse_args()

    p = Path(args.flac_path)
    assert p.exists(), f"Flac path does not exist: {p}"

    q = args.q or default_lame_quality
    o: Path | None = args.out
    c: Path | None = args.copy_to
    if c:
        c = Path(c)
        assert c.is_file(), f'Must be an existing file: "{c}"'
        assert p.is_file(), f'flac_path must be a file when using -c/--copy_to: "{p}"'
    elif o:
        o = Path(args.out)
        o.mkdir(parents=True, exist_ok=True)

    return p, q, o, c


def main() -> None:
    p, q, o, c = parse_args()
    if c:
        copy_only(p, c)
        return
    transcode(p, q, o)


if __name__ == "__main__":
    main()
