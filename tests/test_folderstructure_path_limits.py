"""The path-length limit now comes from the tracker's TAG_RULES instead of a hard-coded 180.

_check_path_lengths blocks (a scene release raises directly rather than offering to auto-fix, which
is the simplest way to assert the limit without going through the interactive confirm loop).
_max_path_length_for_run picks the value a whole run is checked against: the tracker's own limit, or
the strictest among every tracker configured, when the run might go on to another of them.
"""

import pytest

import salmon.trackers
import salmon.uploader as uploader
from salmon import cfg
from salmon.errors import NoncompliantFolderStructure
from salmon.tagger.folderstructure import DEFAULT_MAX_PATH_LENGTH, _check_path_lengths
from salmon.trackers.dic import DICApi
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi


def _folder_with_file(tmp_path, folder_name: str, name_length: int):
    """A release folder holding one file, whose in-torrent path is exactly folder/filename."""
    folder = tmp_path / folder_name
    folder.mkdir()
    filename = ("x" * (name_length - 5)) + ".flac"
    (folder / filename).write_bytes(b"fLaC")
    return str(folder)


def test_red_180_limit_blocks_at_181(tmp_path) -> None:
    # "F" + "/" + an 179-char file name is a 181-char in-torrent path.
    folder = _folder_with_file(tmp_path, "F", 179)

    with pytest.raises(NoncompliantFolderStructure):
        _check_path_lengths(folder, scene=True, max_path_length=RedApi.TAG_RULES.max_path_length)


def test_red_180_limit_allows_exactly_180(tmp_path) -> None:
    folder = _folder_with_file(tmp_path, "F", 178)

    _check_path_lengths(folder, scene=True, max_path_length=RedApi.TAG_RULES.max_path_length)


def test_ops_255_limit_allows_up_to_255_and_blocks_at_256(tmp_path) -> None:
    (tmp_path / "allowed").mkdir()
    (tmp_path / "blocked").mkdir()
    allowed = _folder_with_file(tmp_path / "allowed", "F", 253)
    blocked = _folder_with_file(tmp_path / "blocked", "F", 254)

    _check_path_lengths(allowed, scene=True, max_path_length=OpsApi.TAG_RULES.max_path_length)
    with pytest.raises(NoncompliantFolderStructure):
        _check_path_lengths(blocked, scene=True, max_path_length=OpsApi.TAG_RULES.max_path_length)


def test_salmon_tag_has_no_tracker_and_keeps_180() -> None:
    assert DEFAULT_MAX_PATH_LENGTH == 180


def test_a_single_configured_tracker_run_uses_that_trackers_own_limit(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", True)
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["OPS"])

    assert uploader._max_path_length_for_run(OpsApi()) == 255


def test_a_run_with_red_and_ops_configured_uses_the_strictest(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", True)
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])

    assert uploader._max_path_length_for_run(OpsApi()) == 180
    assert uploader._max_path_length_for_run(RedApi()) == 180


def test_multi_tracker_upload_disabled_uses_only_this_trackers_limit(monkeypatch) -> None:
    # Even with RED also configured, a run that will not offer another tracker afterward
    # is checked against only the one it is actually going to.
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", False)
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])

    assert uploader._max_path_length_for_run(OpsApi()) == 255


def test_dic_keeps_the_180_default_pending_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", True)
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["DIC"])

    assert uploader._max_path_length_for_run(DICApi()) == 180


def test_truncation_that_cannot_fit_raises_instead_of_leaving_the_path_too_long(tmp_path) -> None:
    """A deep sub-folder can already sit right at the limit, leaving no room to shorten a short
    file name into: truncating it down to just ".." plus its extension can still be too long."""
    max_path_length = 20
    folder = tmp_path / "F"
    subfolder = folder / ("S" * 16)  # "F/SSSSSSSSSSSSSSSS" is 18 chars, within the limit.
    subfolder.mkdir(parents=True)
    (subfolder / "x.flac").write_bytes(b"fLaC")  # the full path is 25 chars, over the limit.

    with pytest.raises(NoncompliantFolderStructure):
        _check_path_lengths(str(folder), scene=False, max_path_length=max_path_length)
