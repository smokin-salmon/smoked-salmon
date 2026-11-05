"""`salmon check all` (#541): every check on an album folder, a verdict per check, an exit code and a report.

Synthetic FLACs are checked for real (MQA, upconvert, frequency analysis, tags); `flac` is a fake on PATH, as CI
has none. The trackers are local fakes: no real tracker is contacted.
"""

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import av
import av.audio.stream
import cambia
import numpy as np
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter
from asyncclick.testing import CliRunner
from helpers.checks_all_command import FAKE_FLAC, _fail_lstat, _fail_lstat_of, _fail_open, _fail_scandir, _snapshot
from helpers.checks_do_not_upload import UNMATCHED
from helpers.checks_logs import (
    FakeCambiaOutput,
    FakeChecksum,
    FakeEvaluationCombined,
    FakeParsedCombined,
    FakeParsedLog,
    FakeTrack,
    _patch_cambia,
    _patch_file_crcs,
)
from helpers.checks_mqa import _write_flac
from mutagen.flac import FLAC

import salmon.checks.album as album_checks
import salmon.checks.do_not_upload as do_not_upload
import salmon.trackers
from salmon import cfg
from salmon.checks import all_checks
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import frequency
from salmon.uploader.dupe_checker import generate_dupe_check_searchstrs

FOLDER = "Artist - Album (2020) [WEB FLAC]"
needs_bound_permissions = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs permissions that bind (not root)"
)


@pytest.fixture(autouse=True)
def fake_flac(tmp_path_factory, monkeypatch) -> None:
    bin_dir = tmp_path_factory.mktemp("bin")
    script = bin_dir / "flac"
    script.write_text(f"#!{sys.executable}\n{FAKE_FLAC}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
def album(tmp_path) -> Path:
    """Two 16bit 44.1 kHz WEB tracks, tagged."""
    path = tmp_path / "music" / FOLDER
    path.mkdir(parents=True)
    for number in (1, 2):
        track = path / f"{number:02d} Track {number}.flac"
        _write_flac(track, mqa_marker=False)
        tags = FLAC(track)
        tags.update(
            {
                "artist": "Artist",
                "album": "Album",
                "date": "2020",
                "title": f"Track {number}",
                "tracknumber": str(number),
                "url": "https://www.qobuz.com/album/album/abc",
            }
        )
        tags.save()
    return path


@pytest.fixture
def no_tracker(monkeypatch) -> None:
    """Fail the test if a tracker client is made."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("check all made a tracker client without -t")

    monkeypatch.setattr(salmon.trackers, "get_class", refuse)
    monkeypatch.setattr(BaseGazelleApi, "__init__", refuse)


def _run(*args: str) -> Any:
    return anyio.run(partial(CliRunner().invoke, all_checks, list(args)))


def _rows(output: str) -> dict[str, str]:
    """Each row's check and verdict, from the printed table."""
    rows = {}
    for line in output.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[0] in ("OK", "WARN", "BLOCK", "INFO") and line.startswith("  "):
            check = parts[1].split("  ")[0].strip()
            rows[check] = parts[0]
    return rows


@pytest.mark.usefixtures("no_tracker")
def test_a_clean_album_gets_a_row_for_every_check_and_exits_0(album):
    result = _run(str(album))
    assert result.exit_code == 0, result.output
    rows = _rows(result.output)
    assert rows == {
        "Source": "OK",
        "Integrity": "OK",
        "MQA": "OK",
        "Upconvert": "INFO",
        "Rip log": "INFO",
        "Tags": "OK",
        "Sample rate": "OK",
        "16bit above 48 kHz": "OK",
        "Path length": "OK",
        "Provenance": "OK",
        "Frequency analysis": rows["Frequency analysis"],
        "Do-Not-Upload (RED)": "OK",
        "Do-Not-Upload (OPS)": "OK",
    }
    assert "Advisory: salmon up runs its own checks." in result.output


@pytest.mark.usefixtures("no_tracker")
def test_mqa_blocks_and_exits_1(album):
    _write_flac(album / "03 Track 3.flac", mqa_marker=True)
    result = _run(str(album))
    assert result.exit_code == 1, result.output
    assert _rows(result.output)["MQA"] == "BLOCK"
    assert "03 Track 3.flac" in result.output


@pytest.mark.usefixtures("no_tracker")
def test_a_file_that_does_not_decode_blocks_and_exits_1(album):
    (album / "03 Track 3.flac").write_bytes(b"not a flac stream")
    result = _run(str(album))
    assert result.exit_code == 1, result.output
    assert _rows(result.output)["Integrity"] == "BLOCK"


@pytest.mark.usefixtures("no_tracker")
def test_an_album_in_library_dirs_is_left_as_it_was(album, monkeypatch):
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(album.parent)])
    # An ID3 tag in a FLAC: salmon up strips it from its copy; check all only reports it.
    with open(album / "01 Track 1.flac", "ab") as handle:
        handle.write(b"TAG" + bytes(125))
    before = _snapshot(album)
    result = _run(str(album), "--report")
    assert _rows(result.output)["Tags"] == "WARN"
    assert _snapshot(album) == before


@pytest.mark.usefixtures("no_tracker")
def test_the_report_names_the_folder_and_no_other_path_or_credential(album):
    (album / "03 Track 3.flac").write_bytes(b"not a flac stream")
    result = _run(str(album), "--report")
    report = result.output[result.output.index(f"\n{FOLDER}\n\n1. Lossless or lossy") :]
    assert "5. Checks (salmon check all)" in report
    assert "03 Track 3.flac" in report
    # The decode failure's row names the file by its name alone, though flac was given its full path.
    assert str(album.parent) not in report
    assert cfg.tracker.red is not None
    assert cfg.tracker.red.session not in report
    assert "http" not in report.replace("https://www.qobuz.com/album/album/abc", "")


def _rip(album: Path) -> None:
    """Make the album a CD rip: no store URL, and an EAC log."""
    for track in album.glob("*.flac"):
        tags = FLAC(track)
        del tags["url"]
        tags.save()
    (album / "rip.log").write_text("Exact Audio Copy V1.6 from 23. October 2020\n")


@pytest.mark.usefixtures("no_tracker")
def test_an_edited_log_blocks_and_exits_1(album, monkeypatch):
    _rip(album)
    log = FakeParsedLog([FakeTrack(1, "AAAAAAAA")], checksum=FakeChecksum(cambia.Integrity.Mismatch))
    _patch_cambia(monkeypatch, FakeCambiaOutput(FakeParsedCombined([log])))
    result = _run(str(album))
    assert result.exit_code == 1, result.output
    assert _rows(result.output)["Rip log"] == "BLOCK"
    assert "rip.log: its checksum does not match, so the log was edited." in result.output


@pytest.mark.usefixtures("no_tracker")
def test_a_log_with_a_low_score_no_checksum_and_other_crcs_warns(album, monkeypatch):
    _rip(album)
    log = FakeParsedLog(
        [FakeTrack(1, "AAAAAAAA"), FakeTrack(2, "BBBBBBBB")], checksum=FakeChecksum(cambia.Integrity.Unknown)
    )
    _patch_cambia(monkeypatch, FakeCambiaOutput(FakeParsedCombined([log]), [FakeEvaluationCombined("95")]))
    _patch_file_crcs(monkeypatch, {"01 Track 1.flac": "AAAAAAAA", "02 Track 2.flac": "CCCCCCCC"})
    result = _run(str(album))
    assert result.exit_code == 0, result.output
    assert _rows(result.output)["Source"] == "OK"
    assert _rows(result.output)["Rip log"] == "WARN"
    assert (
        "rip.log: no checksum to verify it (EAC signs its logs from 1.0 beta 3); score 95/100; "
        "the audio does not match its CRCs." in result.output
    )


def test_a_tracker_not_in_the_config_is_refused(album):
    result = _run(str(album), "-t", "NOPE")
    assert result.exit_code == 2
    assert "NOPE is not a tracker in your config" in result.output


# With -t: the fake trackers


class FakeTracker:
    """A local Gazelle tracker: answers index and browse, and records each request."""

    def __init__(self, results: list[dict], browse: dict | None = None) -> None:
        # browse: the whole answer to a browse, instead of one listing results.
        self.browse = browse if browse is not None else {"results": results}
        self.sent: list[tuple[str, str, dict[str, str]]] = []
        self.url = ""

    async def _handle(self, request: web.Request) -> web.Response:
        self.sent.append((request.method, request.path, dict(request.query)))
        if request.query.get("action") == "index":
            return web.json_response({"status": "success", "response": {"authkey": "a", "passkey": "p"}})
        if request.query.get("action") == "browse":
            return web.json_response({"status": "success", "response": self.browse})
        return web.Response(status=404)

    @asynccontextmanager
    async def serving(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        self.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            yield self
        finally:
            await runner.cleanup()


GROUP = {
    "groupId": 7,
    "groupName": "Album",
    "artist": "Artist",
    "groupYear": 2020,
    "torrents": [{"torrentId": 70, "media": "WEB", "format": "FLAC", "encoding": "Lossless", "remasterYear": 2020}],
}


def _run_with_trackers(
    monkeypatch, tmp_path: Path, album: Path, trackers: dict[str, FakeTracker], red_list: str
) -> Any:
    """Run check all ALBUM -t RED,OPS against the fake trackers, with red_list as RED's Do-Not-Upload list."""
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "red.toml").write_text(red_list)
    (lists / "ops.toml").write_text(UNMATCHED)
    monkeypatch.setattr(do_not_upload, "LISTS_DIR", lists)

    def client(code: str) -> BaseGazelleApi:
        site = salmon.trackers.tracker_classes[code]()
        site.base_url = trackers[code].url
        site._rate_limiter = AsyncLimiter(100, 1)
        return site

    monkeypatch.setattr(salmon.trackers, "get_class", lambda code: partial(client, code))

    async def run():
        async with trackers["RED"].serving(), trackers["OPS"].serving():
            return await CliRunner().invoke(all_checks, [str(album), "-t", "RED,OPS"])

    return anyio.run(run)


def test_named_trackers_get_ups_dupe_search_and_their_do_not_upload_list(album, monkeypatch, tmp_path):
    trackers = {"RED": FakeTracker([]), "OPS": FakeTracker([GROUP])}
    result = _run_with_trackers(
        monkeypatch, tmp_path, album, trackers, '[[entry]]\nartist = "Artist"\nnote = "Fakes."\n'
    )
    assert result.exit_code == 1, result.output
    rows = _rows(result.output)
    assert rows["Do-Not-Upload (RED)"] == "BLOCK"
    assert rows["Dupe (RED)"] == "INFO"
    assert rows["Do-Not-Upload (OPS)"] == "OK"
    assert rows["Dupe (OPS)"] == "WARN"
    assert "Artist - Album (2020): 2020 / WEB / FLAC / Lossless" in result.output
    # RED's list forbids the release: RED is not searched. OPS gets what salmon up sends: index, then a browse
    # per search string, and nothing else.
    assert trackers["RED"].sent == []
    searchstrs = generate_dupe_check_searchstrs([("Artist", "main")], "Album", None)
    assert trackers["OPS"].sent == [
        ("GET", "/ajax.php", {"action": "index"}),
        *(("GET", "/ajax.php", {"action": "browse", "searchstr": s}) for s in searchstrs),
    ]


@pytest.mark.parametrize(
    "browse",
    [
        {},
        {"results": [None]},
        {"results": [{**GROUP, "torrents": [None]}]},
        {"results": [{**GROUP, "torrents": 42}]},
    ],
)
def test_a_malformed_search_answer_is_a_warning_and_the_other_checks_go_on(album, monkeypatch, tmp_path, browse):
    trackers = {"RED": FakeTracker([]), "OPS": FakeTracker([], browse=browse)}
    result = _run_with_trackers(monkeypatch, tmp_path, album, trackers, UNMATCHED)
    assert result.exit_code == 0, result.output
    rows = _rows(result.output)
    assert (rows["Dupe (RED)"], rows["Dupe (OPS)"]) == ("OK", "WARN")
    assert "Could not search OPS: unexpected answer (" in result.output


def _write_aac(path: Path) -> None:
    """One second of stereo noise as AAC in an MP4 container, which mutagen says has 16 bits per sample."""
    with av.open(str(path), "w", format="ipod") as out:
        stream = out.add_stream("aac", rate=44100, layout="stereo")
        assert isinstance(stream, av.audio.stream.AudioStream)
        noise = np.random.default_rng(0).normal(0, 0.1, (2, 44100)).astype(np.float32)
        frame = av.AudioFrame.from_ndarray(noise, format="fltp", layout="stereo")
        frame.sample_rate = 44100
        for packet in [*stream.encode(frame), *stream.encode(None)]:
            out.mux(packet)


@pytest.mark.usefixtures("no_tracker")
def test_an_aac_album_is_lossy_and_gets_no_frequency_analysis(tmp_path):
    album = tmp_path / "Artist - Album (2020) [WEB AAC]"
    album.mkdir()
    for number in (1, 2):
        _write_aac(album / f"{number:02d} Track {number}.m4a")
    result = _run(str(album), "--report")
    assert result.exit_code == 0, result.output
    assert _rows(result.output)["Frequency analysis"] == "INFO"
    assert "No lossless file" in result.output
    assert "1. Lossless or lossy\n   Lossy." in result.output


@pytest.mark.usefixtures("no_tracker")
def test_a_failed_analysis_is_reported_as_failed_not_as_no_lossless_file(album, monkeypatch):
    async def boom(*_args):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(frequency, "generate_frequency_plots", boom)
    result = _run(str(album), "--report")
    assert _rows(result.output)["Frequency analysis"] == "INFO"
    assert "4. Frequency analysis (averaged spectrum per track)\n   Failed: see the Frequency analysis" in result.output
    assert "no lossless file" not in result.output


# A folder os.walk cannot list


@pytest.fixture
def discs(album) -> Path:
    """The album with one track in Disc 1 and the other in Disc 2."""
    for number in (1, 2):
        disc = album / f"Disc {number}"
        disc.mkdir()
        (album / f"{number:02d} Track {number}.flac").rename(disc / f"{number:02d} Track {number}.flac")
    return album


@pytest.mark.usefixtures("no_tracker")
def test_an_unreadable_disc_folder_blocks_names_the_folder_and_exits_1(discs, monkeypatch):
    _fail_scandir(monkeypatch, PermissionError, "Disc 2")
    result = _run(str(discs))
    assert result.exit_code == 1, result.output
    rows = _rows(result.output)
    assert rows["Folders"] == "BLOCK"
    assert next(iter(rows)) == "Folders"
    assert "Could not read 1 folder(s), so nothing in them was checked: Disc 2." in result.output
    assert "1 blocking, 0 warning(s)." in result.output


@pytest.mark.usefixtures("no_tracker")
def test_a_nested_unreadable_folder_is_named_from_the_album_and_the_rest_is_still_checked(discs, monkeypatch):
    (discs / "Disc 2" / "Scans").mkdir()
    _fail_scandir(monkeypatch, PermissionError, "Scans")
    result = _run(str(discs))
    assert result.exit_code == 1, result.output
    assert f"nothing in them was checked: {os.path.join('Disc 2', 'Scans')}." in result.output
    assert "All 2 file(s) decode." in result.output


@pytest.mark.usefixtures("no_tracker")
def test_each_unreadable_folder_is_named(discs, monkeypatch):
    for folder in ("Disc 1", "Disc 2"):
        (discs / folder / "Scans").mkdir()
    (discs / "Art").mkdir()
    _fail_scandir(monkeypatch, OSError, "Scans", "Art")
    result = _run(str(discs))
    assert result.exit_code == 1, result.output
    scans = (os.path.join(folder, "Scans") for folder in ("Disc 1", "Disc 2"))
    assert f"Could not read 3 folder(s), so nothing in them was checked: Art, {', '.join(scans)}." in result.output


@pytest.mark.usefixtures("no_tracker")
def test_the_row_and_the_report_name_nothing_above_the_album(discs, monkeypatch):
    _fail_scandir(monkeypatch, PermissionError, "Disc 2")
    result = _run(str(discs), "--report")
    assert "BLOCK Folders: Could not read 1 folder(s), so nothing in them was checked: Disc 2." in result.output
    assert str(discs.parent) not in result.output
    assert os.path.join(FOLDER, "Disc 2") not in result.output


def test_the_unreadable_folders_are_in_name_order_whatever_order_the_walk_finds_them(monkeypatch, tmp_path):
    def walk(path, onerror):
        for name in ("Disc 2", "Disc 1"):
            onerror(PermissionError(13, "Permission denied", os.path.join(path, name)))
        return iter(())

    monkeypatch.setattr(os, "walk", walk)
    assert album_checks._unchecked(str(tmp_path))[0] == ["Disc 1", "Disc 2"]


@pytest.mark.usefixtures("no_tracker")
def test_as_before_a_readable_album_in_disc_folders_has_no_folders_row_and_exits_0(discs):
    result = _run(str(discs))
    assert result.exit_code == 0, result.output
    assert "Folders" not in _rows(result.output)


@pytest.mark.usefixtures("no_tracker")
def test_as_before_a_folder_gone_mid_walk_has_no_folders_row_and_exits_0(discs, monkeypatch):
    _fail_scandir(monkeypatch, FileNotFoundError, "Disc 2")
    result = _run(str(discs))
    assert result.exit_code == 0, result.output
    assert "Folders" not in _rows(result.output)


@pytest.mark.usefixtures("no_tracker")
def test_as_before_a_top_folder_that_fails_to_list_is_no_audio_file(album, monkeypatch):
    _fail_scandir(monkeypatch, PermissionError, FOLDER)
    result = _run(str(album))
    assert result.exit_code == 1, result.output
    assert f"No audio file in {FOLDER}." in result.output


@pytest.mark.usefixtures("no_tracker")
def test_as_before_an_album_whose_only_audio_is_unreadable_is_no_audio_file(discs, monkeypatch):
    _fail_scandir(monkeypatch, PermissionError, "Disc 1", "Disc 2")
    result = _run(str(discs))
    assert result.exit_code == 1, result.output
    assert f"No audio file in {FOLDER}." in result.output


# A folder that lists its names but can't be entered (chmod 444)


@contextmanager
def _chmod(path: Path, mode: int) -> Iterator[None]:
    """path at this mode; the mode tmp_path's clean-up can work with after."""
    path.chmod(mode)
    try:
        yield
    finally:
        path.chmod(stat.S_IRWXU)


@pytest.fixture
def third_disc(discs) -> Path:
    """The album's Disc 3, holding only an M4A: the one kind of audio no check says it failed to read."""
    disc = discs / "Disc 3"
    disc.mkdir()
    _write_aac(disc / "03 Track 3.m4a")
    return disc


@pytest.mark.usefixtures("no_tracker")
def test_a_folder_that_lists_but_cannot_be_entered_blocks_and_names_the_folder(discs, third_disc, monkeypatch):
    _fail_lstat(monkeypatch, PermissionError, third_disc.name)
    result = _run(str(discs))
    assert result.exit_code == 1, result.output
    assert _rows(result.output)["Folders"] == "BLOCK"
    assert "Could not read 1 folder(s), so nothing in them was checked: Disc 3." in result.output


@needs_bound_permissions
@pytest.mark.usefixtures("no_tracker")
def test_a_real_chmod_444_folder_holding_an_m4a_blocks(discs, third_disc):
    with _chmod(third_disc, 0o444):
        result = _run(str(discs))
    assert result.exit_code == 1, result.output
    assert "Could not read 1 folder(s), so nothing in them was checked: Disc 3." in result.output
    assert "Files" not in _rows(result.output)


@needs_bound_permissions
@pytest.mark.usefixtures("no_tracker")
def test_a_flac_in_a_chmod_444_folder_prints_the_rows_rather_than_a_traceback(discs):
    with _chmod(discs / "Disc 2", 0o444):
        result = _run(str(discs))
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1, result.output
    rows = _rows(result.output)
    assert rows["Folders"] == "BLOCK"
    assert rows["Tags"] == "WARN"
    table = result.output[result.output.index(f"\n{FOLDER}") :]
    assert "02 Track 2.flac: could not read it to look for an ID3 tag" in table
    assert str(discs.parent) not in table


@pytest.mark.usefixtures("no_tracker")
def test_a_folder_that_cannot_be_entered_is_named_once_not_with_what_it_holds(discs, monkeypatch):
    (discs / "Disc 3" / "Scans").mkdir(parents=True)
    _fail_lstat(monkeypatch, PermissionError, "Disc 3")
    _fail_scandir(monkeypatch, PermissionError, "Scans")
    result = _run(str(discs))
    assert "Could not read 1 folder(s), so nothing in them was checked: Disc 3." in result.output


@pytest.mark.usefixtures("no_tracker")
def test_an_album_folder_that_cannot_be_entered_is_named_by_its_own_name(tmp_path, monkeypatch):
    album = tmp_path / FOLDER
    album.mkdir()
    _write_aac(album / "01 Track 1.m4a")
    _fail_lstat(monkeypatch, PermissionError, FOLDER)
    result = _run(str(album))
    assert result.exit_code == 1, result.output
    assert f"nothing in them was checked: {FOLDER}." in result.output


@needs_bound_permissions
@pytest.mark.usefixtures("no_tracker")
def test_as_before_an_empty_chmod_444_folder_hides_nothing_and_has_no_row(discs):
    empty = discs / "Empty"
    empty.mkdir()
    with _chmod(empty, 0o444):
        result = _run(str(discs))
    assert result.exit_code == 0, result.output
    assert "Folders" not in _rows(result.output)


@pytest.mark.usefixtures("no_tracker")
def test_as_before_a_folder_with_one_entry_failing_lstat_has_no_row(discs, third_disc, monkeypatch):
    # Whichever entry the listing gives first: an odd file (EIO, ENAMETOOLONG, EILSEQ) says nothing about its folder.
    (third_disc / "odd.txt").touch()
    _fail_lstat_of(monkeypatch, "odd.txt", "03 Track 3.m4a")
    result = _run(str(discs))
    assert result.exit_code == 0, result.output
    assert "Folders" not in _rows(result.output)


@pytest.mark.usefixtures("no_tracker")
def test_as_before_a_folder_gone_once_it_is_listed_has_no_row(discs, third_disc, monkeypatch):
    _fail_lstat(monkeypatch, FileNotFoundError, third_disc.name)
    result = _run(str(discs))
    assert result.exit_code == 0, result.output
    assert "Folders" not in _rows(result.output)


# A link no walk follows


@pytest.fixture
def linked_disc(discs, tmp_path) -> Path:
    """The album with Disc 2 moved out of it and a symlink left in its place; returns where it went."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    target = outside / "Disc 2"
    (discs / "Disc 2").rename(target)
    (discs / "Disc 2").symlink_to(target, target_is_directory=True)
    return target


@pytest.mark.usefixtures("no_tracker", "linked_disc")
def test_a_symlinked_disc_folder_warns_names_the_link_and_exits_0(discs):
    result = _run(str(discs))
    assert result.exit_code == 0, result.output
    rows = _rows(result.output)
    assert rows["Links"] == "WARN"
    assert next(iter(rows)) == "Links"
    assert "Folders" not in rows
    assert "Broken links" not in rows
    assert (
        "Did not follow 1 link(s) to a folder, so what they lead to was not checked: Disc 2. "
        "salmon up uploads what a link leads to; salmon web refuses the album." in result.output
    )
    assert "Nothing blocking, 1 warning(s) to read first." in result.output


@pytest.mark.usefixtures("no_tracker")
def test_an_undecodable_flac_behind_a_symlinked_disc_is_not_seen_so_it_is_still_a_warning_and_exit_0(
    discs, linked_disc
):
    (linked_disc / "02 Track 2.flac").write_bytes(b"not a FLAC stream")
    result = _run(str(discs))
    assert result.exit_code == 0, result.output
    rows = _rows(result.output)
    assert rows["Links"] == "WARN"
    assert rows["Integrity"] == "OK"


@pytest.mark.usefixtures("no_tracker")
def test_a_link_in_a_disc_folder_is_named_from_the_album(discs, tmp_path):
    (discs / "Disc 1" / "Scans").symlink_to(tmp_path, target_is_directory=True)
    result = _run(str(discs))
    assert result.exit_code == 0, result.output
    assert f"was not checked: {os.path.join('Disc 1', 'Scans')}." in result.output


@pytest.mark.usefixtures("no_tracker")
@pytest.mark.parametrize("name", ["Disc 3", "03 Track 3.m4a"])
def test_a_link_leading_nowhere_blocks_and_names_the_link(discs, tmp_path, name):
    (discs / name).symlink_to(tmp_path / "nowhere")
    result = _run(str(discs))
    assert result.exit_code == 1, result.output
    rows = _rows(result.output)
    assert rows["Broken links"] == "BLOCK"
    assert "Links" not in rows
    assert "Files" not in rows
    assert f"1 link(s) lead nowhere: {name}. salmon up stops on one." in result.output


@pytest.mark.usefixtures("no_tracker")
def test_a_link_leading_nowhere_in_a_disc_folder_is_named_from_the_album(discs, tmp_path):
    (discs / "Disc 1" / "Scans").symlink_to(tmp_path / "nowhere")
    result = _run(str(discs))
    assert result.exit_code == 1, result.output
    assert f"1 link(s) lead nowhere: {os.path.join('Disc 1', 'Scans')}." in result.output


@pytest.mark.usefixtures("no_tracker")
def test_a_link_in_a_loop_blocks_and_names_the_link(discs):
    (discs / "Disc 3").symlink_to(discs / "Disc 4")
    (discs / "Disc 4").symlink_to(discs / "Disc 3")
    result = _run(str(discs))
    assert result.exit_code == 1, result.output
    assert _rows(result.output)["Broken links"] == "BLOCK"
    assert "2 link(s) lead nowhere: Disc 3, Disc 4." in result.output


@pytest.mark.usefixtures("no_tracker")
def test_a_flac_link_leading_nowhere_prints_the_rows_rather_than_a_traceback(discs, tmp_path):
    (discs / "03 Track 3.flac").symlink_to(tmp_path / "nowhere")
    result = _run(str(discs))
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1, result.output
    assert _rows(result.output)["Broken links"] == "BLOCK"
    assert "03 Track 3.flac: could not read it to look for an ID3 tag" in result.output


@pytest.mark.usefixtures("no_tracker")
def test_as_before_a_symlinked_file_that_is_read_has_no_links_row_and_exits_0(discs, tmp_path):
    target = tmp_path / "target.m4a"
    _write_aac(target)
    (discs / "03 Track 3.m4a").symlink_to(target)
    result = _run(str(discs))
    assert result.exit_code == 0, result.output
    assert "Links" not in _rows(result.output)


@pytest.mark.usefixtures("no_tracker")
def test_as_before_an_album_folder_given_through_a_link_is_checked_where_it_leads(album, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(album, target_is_directory=True)
    result = _run(str(link))
    assert result.exit_code == 0, result.output
    assert "Links" not in _rows(result.output)


# An M4A that can't be opened: the integrity check walks FLAC and MP3 only, and _audio_info passes over what fails


@pytest.mark.usefixtures("no_tracker")
@pytest.mark.parametrize("name", ["03 Track 3.m4a", "03 Track 3.M4A"])
def test_an_m4a_that_cannot_be_opened_blocks_and_names_the_file(discs, monkeypatch, name):
    _write_aac(discs / "Disc 2" / name)
    _fail_open(monkeypatch, PermissionError, name)
    result = _run(str(discs))
    assert result.exit_code == 1, result.output
    rows = _rows(result.output)
    assert rows["Files"] == "BLOCK"
    assert "Folders" not in rows
    assert f"so they were not checked: {os.path.join('Disc 2', name)}." in result.output


@needs_bound_permissions
@pytest.mark.usefixtures("no_tracker")
def test_a_real_chmod_000_m4a_that_is_the_albums_only_audio_blocks(tmp_path):
    album = tmp_path / FOLDER
    album.mkdir()
    track = album / "01 Track 1.m4a"
    _write_aac(track)
    with _chmod(track, 0):
        result = _run(str(album))
    assert result.exit_code == 1, result.output
    assert "Could not read 1 file(s), so they were not checked: 01 Track 1.m4a." in result.output


@needs_bound_permissions
@pytest.mark.usefixtures("no_tracker")
def test_a_chmod_000_flac_is_left_to_integrity_and_prints_the_rows_rather_than_a_traceback(discs):
    with _chmod(discs / "Disc 2" / "02 Track 2.flac", 0):
        result = _run(str(discs))
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1, result.output
    rows = _rows(result.output)
    assert rows["Integrity"] == "BLOCK"
    assert "Files" not in rows


@pytest.mark.usefixtures("no_tracker")
def test_as_before_a_readable_m4a_has_no_files_row_and_exits_0(discs):
    _write_aac(discs / "03 Track 3.m4a")
    result = _run(str(discs))
    assert result.exit_code == 0, result.output
    assert "Files" not in _rows(result.output)


@pytest.mark.usefixtures("no_tracker")
def test_as_before_an_m4a_gone_mid_check_has_no_files_row_and_exits_0(discs, monkeypatch):
    _write_aac(discs / "03 Track 3.m4a")
    _fail_open(monkeypatch, FileNotFoundError, "03 Track 3.m4a")
    result = _run(str(discs))
    assert result.exit_code == 0, result.output
    assert "Files" not in _rows(result.output)


@pytest.mark.usefixtures("no_tracker")
def test_as_before_an_m4a_that_opens_but_does_not_parse_is_not_called_unreadable(discs):
    (discs / "03 Track 3.m4a").write_bytes(b"not an MP4 file" * 100)
    result = _run(str(discs))
    assert result.exit_code == 0, result.output
    rows = _rows(result.output)
    assert "Integrity" in rows, result.output
    assert "Files" not in rows


@pytest.mark.usefixtures("no_tracker")
def test_as_before_a_folder_named_like_an_m4a_has_no_files_row_and_exits_0(discs):
    (discs / "Scans.m4a").mkdir()
    result = _run(str(discs))
    assert result.exit_code == 0, result.output
    assert "Files" not in _rows(result.output)


def test_the_links_and_files_are_in_name_order_whatever_order_the_walk_finds_them(monkeypatch, tmp_path):
    for name in ("Disc 1", "Disc 2"):
        (tmp_path / name).symlink_to(tmp_path, target_is_directory=True)
    for name in ("Gone 1", "Gone 2"):
        (tmp_path / name).symlink_to(tmp_path / "nowhere")
    for name in ("a.m4a", "b.m4a"):
        (tmp_path / name).touch()
    _fail_open(monkeypatch, PermissionError, "a.m4a", "b.m4a")
    walked = ["Disc 2", "Disc 1"], ["Gone 2", "Gone 1", "b.m4a", "a.m4a"]
    monkeypatch.setattr(os, "walk", lambda path, onerror: iter([(path, *walked)]))
    assert album_checks._unchecked(str(tmp_path)) == (
        [],
        ["Disc 1", "Disc 2"],
        ["Gone 1", "Gone 2"],
        ["a.m4a", "b.m4a"],
    )
