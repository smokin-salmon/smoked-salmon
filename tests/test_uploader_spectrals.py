import os
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from salmon import interaction
from salmon.errors import RequestFailedError
from salmon.uploader import spectrals

FOUR_TRACKS = {1: "01 a.flac", 2: "02 b.flac", 3: "03 c.flac", 4: "04 d.flac"}


@pytest.fixture(autouse=True)
def _interactive(monkeypatch) -> None:
    monkeypatch.setattr(spectrals.cfg.upload, "yes_all", False)


def _accept_default(monkeypatch) -> list[str]:
    """Answer the spectral IDs prompt with its default, and record the defaults offered."""
    offered: list[str] = []

    async def prompt(_text: str, default: str, **_kwargs) -> str:
        offered.append(default)
        if len(offered) > 1:
            raise AssertionError(f"asked again after the default was rejected: {offered}")
        return default

    monkeypatch.setattr(spectrals.click, "prompt", prompt)
    return offered


@pytest.mark.parametrize(
    ("configured", "offered", "picked"),
    [
        ("3", "3", [3]),
        ("1 3", "1 3", [1, 3]),
        # IDs the release does not have are dropped from the default.
        ("2 9", "2", [2]),
        ("03", "03", [3]),
    ],
)
def test_configured_track_ids_are_the_default(monkeypatch, configured: str, offered: str, picked: list[int]) -> None:
    monkeypatch.setattr(spectrals.cfg.image, "default_spectral_ids", configured)
    offers = _accept_default(monkeypatch)

    result = anyio.run(spectrals.prompt_spectrals, FOUR_TRACKS, False, True)

    assert offers == [offered]
    assert result == {i: FOUR_TRACKS[i] for i in picked}


@pytest.mark.parametrize(("lossy_master", "offered"), [(False, "+"), (True, "*")])
def test_configured_track_ids_beyond_the_last_track_fall_back(monkeypatch, lossy_master: bool, offered: str) -> None:
    monkeypatch.setattr(spectrals.cfg.image, "default_spectral_ids", "5 9")
    offers = _accept_default(monkeypatch)

    result = anyio.run(spectrals.prompt_spectrals, FOUR_TRACKS, lossy_master, True)

    # The context default, as if nothing were configured: never a default the prompt itself rejects.
    assert offers == [offered]
    assert result
    assert set(result) <= set(FOUR_TRACKS)


@pytest.mark.parametrize(("configured", "expected"), [("*", FOUR_TRACKS), ("0", None)])
def test_selections_behave_as_before(monkeypatch, configured: str, expected) -> None:
    monkeypatch.setattr(spectrals.cfg.image, "default_spectral_ids", configured)
    offers = _accept_default(monkeypatch)

    assert anyio.run(spectrals.prompt_spectrals, FOUR_TRACKS, False, True) == expected
    assert offers == [configured]


MULTI_DISC = [
    os.path.join("CD1", "01 a.flac"),
    os.path.join("CD1", "02 b.flac"),
    os.path.join("CD2", "01 c.flac"),
    os.path.join("CD2", "02 d.flac"),
]


@pytest.fixture
def release(tmp_path, monkeypatch):
    """A two-disc release whose spectrals are drawn by a fake sox, and whose compression is recorded."""
    path = tmp_path / "release"
    for name in MULTI_DISC:
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_bytes(b"")
    audio_info = {name: {"duration": 100} for name in MULTI_DISC}

    async def fake_sox(args, **_kwargs) -> None:
        for i, arg in enumerate(args):
            if arg == "-o":
                Path(args[i + 1]).write_bytes(b"png")

    compressed: list[str] = []

    async def record_compression(filepath: str, _idx: int) -> None:
        compressed.append(Path(filepath).name)

    async def not_lossy(*_args, **_kwargs) -> bool:
        return False

    async def no_viewer(*_args, **_kwargs) -> None:
        pass

    monkeypatch.setattr(spectrals.anyio, "run_process", fake_sox)
    monkeypatch.setattr(spectrals, "_compress_single_spectral", record_compression)
    # Compression runs only where oxipng is installed (not on Python 3.14); this checks which files it gets.
    monkeypatch.setattr(spectrals, "oxipng", object())
    monkeypatch.setattr(spectrals, "prompt_lossy_master", not_lossy)
    monkeypatch.setattr(interaction, "show_spectrals", no_viewer)
    monkeypatch.setattr(spectrals.cfg.upload.compression, "compress_spectrals", True)
    monkeypatch.setattr(spectrals.cfg.directory, "tmp_dir", None)
    return str(path), audio_info, compressed


def _pick(monkeypatch, answer: str) -> None:
    async def prompt(*_args, **_kwargs) -> str:
        return answer

    monkeypatch.setattr(spectrals.click, "prompt", prompt)


def _images(*track_ids: int) -> list[str]:
    return sorted(f"{i:02d} {kind}.png" for i in track_ids for kind in ("Full", "Zoom"))


def test_only_the_picked_spectrals_are_compressed(monkeypatch, release) -> None:
    path, audio_info, compressed = release
    _pick(monkeypatch, "3")

    _lossy, picked = anyio.run(spectrals.check_spectrals, path, audio_info)

    # Track 3 is the first track of the second disc: its images are numbered on from disc one's.
    assert picked == {3: os.path.join("CD2", "01 c.flac")}
    assert sorted(compressed) == _images(3)


def test_nothing_is_compressed_when_no_spectral_is_picked(monkeypatch, release) -> None:
    path, audio_info, compressed = release
    _pick(monkeypatch, "0")

    _lossy, picked = anyio.run(spectrals.check_spectrals, path, audio_info)

    assert picked is None
    assert compressed == []


def test_spectrals_checked_after_upload_are_compressed_before_they_are_uploaded(monkeypatch, release) -> None:
    path, audio_info, compressed = release
    _pick(monkeypatch, "2 4")
    compressed_when_uploaded: list[str] = []

    async def upload(_spectrals_path, _spectral_ids, _tracker) -> None:
        compressed_when_uploaded.extend(compressed)

    monkeypatch.setattr(spectrals, "upload_spectrals", upload)

    async def check_after_upload():
        return await spectrals.post_upload_spectral_check(
            SimpleNamespace(site_code="RED"),  # type: ignore[arg-type]  # only its code is read when nothing is uploaded
            path,
            1,
            None,
            audio_info,
            "WEB",
            "https://store.test/album",
        )

    result = anyio.run(check_after_upload)

    assert result[3] == {2: os.path.join("CD1", "02 b.flac"), 4: os.path.join("CD2", "02 d.flac")}
    assert sorted(compressed_when_uploaded) == _images(2, 4)


def test_spectrals_given_on_the_command_line_are_compressed(release) -> None:
    path, audio_info, compressed = release

    _lossy, picked = anyio.run(spectrals.check_spectrals, path, audio_info, False, (2, 4))

    assert picked is not None
    assert sorted(compressed) == _images(*picked)


class _FailingDescriptionEditSite:
    """A fake site whose description edit always fails, to prove the flow survives it."""

    base_url = "https://fake.test"
    site_string = "Fake"
    site_code = "RED"

    def __init__(self) -> None:
        self.description_edit_calls = 0
        self.lossy_master_reports: list[tuple[int, str, str]] = []

    async def append_to_torrent_description(self, _torrent_id: int, _description_addition: str) -> None:
        self.description_edit_calls += 1
        raise RequestFailedError("the edit form could not be read")

    async def report_lossy_master(self, torrent_id: int, comment: str, source: str) -> bool:
        self.lossy_master_reports.append((torrent_id, comment, source))
        return True


def test_a_failed_description_edit_still_prints_the_bbcode_and_reports_lossy_master(
    monkeypatch, release, capsys
) -> None:
    path, audio_info, _compressed = release
    _pick(monkeypatch, "2")

    async def lossy(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(spectrals, "prompt_lossy_master", lossy)

    async def upload(_spectrals_path, spectral_ids, _tracker) -> dict[int, list[str]]:
        return {spec_id: [f"{spec_id}-full.png", f"{spec_id}-zoom.png"] for spec_id in spectral_ids}

    monkeypatch.setattr(spectrals, "upload_spectrals", upload)

    site = _FailingDescriptionEditSite()

    async def check_after_upload():
        return await spectrals.post_upload_spectral_check(
            site,  # type: ignore[arg-type]  # a fake site is enough: only append_to_torrent_description/report_lossy_master are called
            path,
            1,
            None,
            audio_info,
            "WEB",
            "https://store.test/album",
        )

    lossy_master, _comment, spectral_urls, _spectral_ids = anyio.run(check_after_upload)

    assert lossy_master is True
    assert spectral_urls is not None

    # The edit was attempted exactly once: no retry.
    assert site.description_edit_calls == 1

    # The lossy master report still went out, as if the edit had worked.
    assert len(site.lossy_master_reports) == 1

    output = capsys.readouterr().out
    assert "was not updated" in output
    assert "https://fake.test/torrents.php?torrentid=1" in output
    # The bbcode itself is printed plainly so it can be pasted in by hand.
    assert "[hide=Spectrals]" in output


def test_spectrals_checked_after_upload_are_deleted_when_none_is_picked(monkeypatch, release) -> None:
    path, audio_info, _compressed = release
    _pick(monkeypatch, "0")

    async def not_lossy(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(spectrals, "prompt_lossy_master", not_lossy)

    async def check_after_upload():
        return await spectrals.post_upload_spectral_check(
            SimpleNamespace(site_code="RED"),  # type: ignore[arg-type]  # only its code is read when nothing is uploaded
            path,
            1,
            None,
            audio_info,
            "WEB",
            "https://store.test/album",
        )

    assert anyio.run(check_after_upload) == (False, None, None, None)
    # Not left in the folder, where the torrent of the next tracker would take them.
    assert not os.path.exists(spectrals.get_spectrals_path(path))
