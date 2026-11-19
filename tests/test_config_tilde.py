"""A leading ~ in a folder setting is expanded when the config loads (#1095)."""

from pathlib import Path

import pytest

from salmon.config.validations import Cfg, Directory, GazelleTrackerSettings, Tracker


@pytest.fixture
def home_dirs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    music = home / "music"
    torrents = home / "torrents"
    library = home / "library"
    tmp = home / "tmp"
    for folder in (music, torrents, library, tmp):
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return {"home": home, "music": music, "torrents": torrents, "library": library, "tmp": tmp}


def test_a_tilde_in_a_folder_setting_is_expanded_at_load(home_dirs) -> None:
    directories = home_dirs
    directory = Directory(
        download_directory="~/music",
        dottorrents_dir="~/torrents",
        tmp_dir="~/tmp",
        cross_upload_dottorrents_dir="~/torrents",
        library_dirs=["~/library"],
    )

    assert Path(directory.download_directory) == directories["music"]
    assert Path(directory.dottorrents_dir) == directories["torrents"]
    assert directory.tmp_dir is not None
    assert Path(directory.tmp_dir) == directories["tmp"]
    assert directory.cross_upload_dottorrents_dir is not None
    assert Path(directory.cross_upload_dottorrents_dir) == directories["torrents"]
    assert [Path(entry) for entry in directory.library_dirs] == [directories["library"]]


def test_a_tilde_folder_that_does_not_exist_is_still_refused(home_dirs) -> None:
    with pytest.raises(ValueError, match="download_directory is not a valid directory"):
        Directory(download_directory="~/missing", dottorrents_dir=str(home_dirs["torrents"]))


def test_library_dirs_protection_runs_on_the_expanded_path(home_dirs) -> None:
    with pytest.raises(ValueError, match="must not contain download_directory"):
        Directory(
            download_directory="~/music",
            dottorrents_dir="~/torrents",
            library_dirs=["~/music"],
        )


def test_a_trackers_tilde_folder_is_expanded_and_checked_against_library_dirs(home_dirs) -> None:
    (home_dirs["library"] / "torrents").mkdir()
    directory = Directory(
        download_directory=str(home_dirs["music"]),
        dottorrents_dir=str(home_dirs["torrents"]),
        library_dirs=["~/library"],
    )

    inside = GazelleTrackerSettings(session="session", cross_upload_dottorrents_dir="~/library/torrents")
    with pytest.raises(ValueError, match=r"must not contain tracker\.red\.cross_upload_dottorrents_dir"):
        Cfg(directory=directory, tracker=Tracker(red=inside))

    beside = GazelleTrackerSettings(
        session="session",
        dottorrents_dir="~/torrents",
        cross_upload_dottorrents_dir="~/torrents",
    )
    loaded = Cfg(directory=directory, tracker=Tracker(red=beside))
    assert loaded.tracker.red is not None
    assert loaded.tracker.red.dottorrents_dir is not None
    assert Path(loaded.tracker.red.dottorrents_dir) == home_dirs["torrents"]
    assert loaded.tracker.red.cross_upload_dottorrents_dir is not None
    assert Path(loaded.tracker.red.cross_upload_dottorrents_dir) == home_dirs["torrents"]
