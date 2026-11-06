"""#952: salmon cross-upload's own .torrent folder and client label, as the config and the queue see them."""

from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import salmon.trackers
from salmon import cfg
from salmon.config.validations import Cfg, Directory, GazelleTrackerSettings, Seedbox, Tracker
from salmon.uploader import seedbox
from salmon.webui import paths

LEVELS = ("tracker_cross", "global_cross", "tracker_up", "global_up")


def _folders(tmp_path: Path) -> SimpleNamespace:
    found = SimpleNamespace(**{level: tmp_path / level for level in LEVELS})
    for folder in vars(found).values():
        folder.mkdir()
    return found


def _config(monkeypatch, folders: SimpleNamespace, *levels: str) -> None:
    """Set the levels named on RED. Without global_up, dottorrents_dir is the folders' parent."""

    def path(level: str) -> str | None:
        return str(getattr(folders, level)) if level in levels else None

    monkeypatch.setattr(cfg.directory, "dottorrents_dir", path("global_up") or str(folders.global_up.parent))
    monkeypatch.setattr(cfg.directory, "cross_upload_dottorrents_dir", path("global_cross"))
    red = GazelleTrackerSettings(
        session="s", dottorrents_dir=path("tracker_up"), cross_upload_dottorrents_dir=path("tracker_cross")
    )
    monkeypatch.setattr(cfg.tracker, "red", red)
    monkeypatch.setattr(cfg, "seedbox", [])


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        (LEVELS, "tracker_cross"),
        (LEVELS[1:], "global_cross"),
        (LEVELS[2:], None),
        (LEVELS[3:], None),
    ],
)
def test_the_first_set_cross_upload_folder_wins(monkeypatch, tmp_path, levels, expected) -> None:
    folders = _folders(tmp_path)
    _config(monkeypatch, folders, *levels)

    assert cfg.cross_upload_dottorrents_dir("RED") == (str(getattr(folders, expected)) if expected else None)


def test_salmon_ups_folder_is_unchanged_with_every_cross_upload_key_set(monkeypatch, tmp_path) -> None:
    folders = _folders(tmp_path)
    _config(monkeypatch, folders, *LEVELS)

    assert salmon.trackers.get_class("RED")().dot_torrents_dir == str(folders.tracker_up)


def test_the_new_keys_are_unset_by_default() -> None:
    assert Seedbox().cross_upload_label is None
    assert GazelleTrackerSettings(session="s").cross_upload_dottorrents_dir is None


def _seed_labels(monkeypatch, tmp_path, box: Seedbox, *, cross_upload: bool) -> list[str]:
    labels: list[str] = []

    class Client:
        def add_to_downloader(self, remote_folder, torrent, is_paused, label) -> bool:
            labels.append(label)
            return True

    monkeypatch.setattr(seedbox.cfg, "seedbox", [box])
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: Client()))
    monkeypatch.setattr(seedbox.click, "secho", lambda *args, **kwargs: None)
    release = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    release.mkdir()
    torrent = tmp_path / "Album.torrent"
    torrent.write_bytes(b"d4:infod4:name5:Albumee")
    manager = seedbox.UploadManager(cross_upload=cross_upload)
    manager.add_upload_task(str(torrent), task_type="seed", is_flac=True, folder=str(release))
    anyio.run(manager.execute_upload)
    return labels


@pytest.mark.parametrize(
    ("cross_label", "cross_upload", "expected"),
    [("cross", True, "cross"), (None, True, "up"), ("cross", False, "up"), ("", True, "")],
)
def test_a_seed_task_carries_the_label_of_the_command_that_queued_it(
    monkeypatch, tmp_path, cross_label, cross_upload, expected
) -> None:
    box = Seedbox(
        name="box",
        type="local",
        directory="/seed",
        torrent_client="qbittorrent+http://127.0.0.1:9",
        label="up",
        cross_upload_label=cross_label,
    )

    assert _seed_labels(monkeypatch, tmp_path, box, cross_upload=cross_upload) == [expected]


@pytest.mark.parametrize("where", ["global", "red"])
def test_a_missing_cross_upload_folder_is_refused_naming_the_key(tmp_path, where) -> None:
    missing = str(tmp_path / "missing")

    with pytest.raises(ValueError, match="cross_upload_dottorrents_dir is not a valid directory"):
        if where == "global":
            Directory(
                dottorrents_dir=str(tmp_path), download_directory=str(tmp_path), cross_upload_dottorrents_dir=missing
            )
        else:
            GazelleTrackerSettings(session="s", cross_upload_dottorrents_dir=missing)


@pytest.mark.parametrize("where", ["global", "red"])
def test_a_cross_upload_folder_in_a_library_is_refused_naming_the_key(tmp_path, where) -> None:
    library = tmp_path / "data"
    (library / "torrents").mkdir(parents=True)
    inside = str(library / "torrents")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    base = {"dottorrents_dir": str(downloads), "download_directory": str(downloads), "library_dirs": [str(library)]}

    if where == "global":
        with pytest.raises(ValueError, match="must not contain cross_upload_dottorrents_dir"):
            Directory(cross_upload_dottorrents_dir=inside, **base)
    else:
        settings = GazelleTrackerSettings(session="s", cross_upload_dottorrents_dir=inside)
        with pytest.raises(ValueError, match=r"must not contain tracker\.red\.cross_upload_dottorrents_dir"):
            Cfg(directory=Directory(**base), tracker=Tracker(red=settings))


def test_withheld_folders_lists_the_cross_upload_folders(monkeypatch, tmp_path) -> None:
    folders = _folders(tmp_path)
    _config(monkeypatch, folders, "tracker_cross", "global_cross")

    withheld = paths.withheld_folders()

    assert str(folders.tracker_cross.resolve()) in withheld
    assert str(folders.global_cross.resolve()) in withheld
