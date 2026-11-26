"""--overwrite reaches the metadata search: the files' artist and year tags are not sent to MusicBrainz (#1178)."""

import copy
from typing import Any

import pytest
from helpers.library_dirs import _album as tag_album
from helpers.library_dirs import _run_tag
from helpers.uploader_dry_run import METADATA, _album, _run_up
from test_uploader_dry_run import dirs, image_uploads  # noqa: F401  (fixtures)

from salmon import cfg


def _capturing_get_metadata(sent: list[dict[str, Any]]):
    async def get_metadata(_path: str, _tags: Any, _rls_data: Any, **kwargs: Any) -> tuple[dict[str, Any], None]:
        sent.append(kwargs)
        return copy.deepcopy(METADATA), None

    return get_metadata


@pytest.mark.parametrize(("args", "overwrite"), [(("-ow",), True), ((), False)])
def test_up_tells_the_metadata_search_whether_it_overwrites(
    monkeypatch,
    tmp_path,
    dirs,  # noqa: F811 (the fixture imported above)
    image_uploads,  # noqa: F811 (the fixture imported above)
    args: tuple[str, ...],
    overwrite: bool,
) -> None:
    _library, _downloads, torrents = dirs
    sent: list[dict[str, Any]] = []
    run = _run_up(
        monkeypatch,
        _album(tmp_path / "seeding" / "Album"),
        torrents,
        args=("--dry-run", *args),
        get_metadata=_capturing_get_metadata(sent),
    )
    assert run.result.exit_code == 0, run.result.output
    assert [call.get("overwrite") for call in sent] == [overwrite]


@pytest.mark.parametrize(("args", "overwrite"), [(("-ow",), True), ((), False)])
def test_tag_tells_the_metadata_search_whether_it_overwrites(
    monkeypatch, tmp_path, args: tuple[str, ...], overwrite: bool
) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    sent: list[dict[str, Any]] = []
    result = _run_tag(
        monkeypatch, tag_album(tmp_path / "seeding" / "Album"), args, get_metadata=_capturing_get_metadata(sent)
    )
    assert result.exit_code == 0, result.output
    assert [call.get("overwrite") for call in sent] == [overwrite]
