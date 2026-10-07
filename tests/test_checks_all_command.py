"""`salmon check all` (#541): every check on an album folder, a verdict per check, an exit code and a report.

Synthetic FLACs are checked for real (MQA, upconvert, frequency analysis, tags); `flac` is a fake on PATH, as CI
has none. The trackers are local fakes: no real tracker is contacted.
"""

import os
import stat
import sys
from contextlib import asynccontextmanager
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
from mutagen.flac import FLAC
from test_checks_logs import (  # pyright: ignore[reportMissingImports]
    FakeCambiaOutput,
    FakeChecksum,
    FakeEvaluationCombined,
    FakeParsedCombined,
    FakeParsedLog,
    FakeTrack,
    _patch_cambia,
    _patch_file_crcs,
)
from test_checks_mqa import _write_flac  # pyright: ignore[reportMissingImports]

import salmon.checks.do_not_upload as do_not_upload
import salmon.trackers
from salmon import cfg
from salmon.checks import all_checks
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader.dupe_checker import generate_dupe_check_searchstrs

FOLDER = "Artist - Album (2020) [WEB FLAC]"

# A stand-in for flac's test mode: a file that is a FLAC stream decodes, anything else fails as flac fails.
FAKE_FLAC = """\
import sys
path = sys.argv[-1]
with open(path, "rb") as handle:
    good = handle.read(4) == b"fLaC"
sys.stderr.write(f"{path}: ok\\n" if good else f"{path}: ERROR while decoding data\\n")
sys.exit(0 if good else 1)
"""


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


def _snapshot(path: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime_ns) for p in path.rglob("*") if p.is_file()
    ) + [("", 0, path.stat().st_mtime_ns)]


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
    (lists / "ops.toml").write_text("")
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
    result = _run_with_trackers(monkeypatch, tmp_path, album, trackers, "")
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
