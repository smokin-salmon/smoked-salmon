"""A multi-file mutation that fails partway must never continue silently.

None of these can be atomic, so the album really can end up inconsistent. The
requirement is that it is reported and the run stops as failed: continuing would
upload a release whose tracks disagree with each other.
"""

import os

import pytest
from mutagen.flac import FLAC
from test_tagger_tags import _write_flac  # pyright: ignore[reportMissingImports]

from salmon.errors import UploadError
from salmon.tagger import retagger, tags
from salmon.tagger.mutation import abort_partial
from salmon.tagger.retagger import Change


def test_abort_partial_names_what_changed_and_stops(capsys):
    # An error, not click.Abort: the upload catches an abort and ends as if the user had said no.
    with pytest.raises(UploadError, match=r"Retagging 02\.flac failed \(disk full\): the album is now inconsistent"):
        abort_partial("Retagging 02.flac", ["01.flac"], "02.flac", ["03.flac"], OSError("disk full"))
    out = capsys.readouterr().out
    assert "Already changed: 01.flac" in out
    assert "May be partially changed: 02.flac" in out
    assert "Not changed: 03.flac" in out


def test_the_failing_file_is_not_called_unchanged(capsys):
    """A write can fail after modifying the file, so its state is unknown."""
    with pytest.raises(UploadError) as stopped:
        abort_partial("Retagging 01.flac", [], "01.flac", [], OSError("denied"))
    out = capsys.readouterr().out
    assert "May be partially changed: 01.flac" in out
    assert "Not changed" not in out
    assert "inconsistent" in str(stopped.value), "a half-written first file is still an inconsistency"


def test_abort_partial_is_quiet_about_consistency_when_nothing_was_touched(capsys):
    with pytest.raises(UploadError) as stopped:
        abort_partial("Reading 01.flac", [], None, ["01.flac"], OSError("denied"))
    assert "inconsistent" not in str(stopped.value)
    assert "Not changed: 01.flac" in capsys.readouterr().out


# Through the album-wide writes


def _fails_to_save(opened, name: str):
    """`opened`, with the file named `name` failing to save, as on a full disk."""

    def opening(path, *args, **kwargs):
        mut = opened(path, *args, **kwargs)
        if os.path.basename(path) == name:

            def full_disk(*_args, **_kwargs) -> None:
                raise OSError(28, "No space left on device")

            mut.save = full_disk
        return mut

    return opening


def test_a_retag_that_fails_partway_names_what_it_changed_and_stops(tmp_path, monkeypatch, capsys) -> None:
    for name in ("01.flac", "02.flac", "03.flac"):
        _write_flac(tmp_path / name, title="Old")
    monkeypatch.setattr(retagger, "TagFile", _fails_to_save(retagger.TagFile, "02.flac"))
    changes = {name: [Change("title", "Old", "New")] for name in ("01.flac", "02.flac", "03.flac")}

    with pytest.raises(UploadError, match="inconsistent"):
        retagger.retag_files(str(tmp_path), {}, changes)

    out = capsys.readouterr().out
    assert "Already changed: 01.flac" in out
    assert "May be partially changed: 02.flac" in out
    assert "Not changed: 03.flac" in out
    assert [FLAC(tmp_path / name)["title"] for name in ("01.flac", "03.flac")] == [["New"], ["Old"]]


def test_a_retag_that_cannot_open_its_first_file_says_nothing_changed(tmp_path, monkeypatch, capsys) -> None:
    # Only save() writes: a file that fails before it is as it was, so the album is not called inconsistent.
    for name in ("01.flac", "02.flac"):
        _write_flac(tmp_path / name, title="Old")
    opened = retagger.TagFile

    def unreadable_first(path, *args, **kwargs):
        if os.path.basename(path) == "01.flac":
            raise OSError(13, "Permission denied")
        return opened(path, *args, **kwargs)

    monkeypatch.setattr(retagger, "TagFile", unreadable_first)
    changes = {name: [Change("title", "Old", "New")] for name in ("01.flac", "02.flac")}

    with pytest.raises(UploadError) as stopped:
        retagger.retag_files(str(tmp_path), {}, changes)

    out = capsys.readouterr().out
    assert "Not changed: 01.flac, 02.flac" in out
    assert "May be partially changed" not in out
    assert "inconsistent" not in str(stopped.value)
    assert [FLAC(tmp_path / name)["title"] for name in ("01.flac", "02.flac")] == [["Old"], ["Old"]]


def test_a_retag_of_a_file_mutagen_cannot_read_stops_before_calling_it_changed(tmp_path, capsys) -> None:
    _write_flac(tmp_path / "01.flac", title="Old")
    # No format claims it, so mutagen gives None rather than raising.
    (tmp_path / "02.xyz").write_bytes(b"not audio at all")
    changes = {name: [Change("title", "Old", "New")] for name in ("01.flac", "02.xyz")}

    with pytest.raises(UploadError, match="Retagging 02.xyz failed \\(not an audio file mutagen can read\\)"):
        retagger.retag_files(str(tmp_path), {}, changes)

    out = capsys.readouterr().out
    assert "Already changed: 01.flac" in out
    assert "Not changed: 02.xyz" in out
    assert "May be partially changed" not in out


def test_standardizing_tags_that_fails_partway_names_what_it_changed_and_stops(tmp_path, monkeypatch, capsys) -> None:
    for name in ("01.flac", "02.flac", "03.flac"):
        _write_flac(tmp_path / name, year="2001")
    monkeypatch.setattr(tags, "MutagenFile", _fails_to_save(tags.MutagenFile, "02.flac"))

    with pytest.raises(UploadError, match="inconsistent"):
        tags.standardize_tags(str(tmp_path))

    out = capsys.readouterr().out
    assert "Already changed: 01.flac" in out
    assert "May be partially changed: 02.flac" in out
    assert "Not changed: 03.flac" in out
    assert "date" in FLAC(tmp_path / "01.flac") and "year" in FLAC(tmp_path / "03.flac")


def test_standardizing_tags_that_cannot_open_a_later_file_names_what_it_changed(tmp_path, monkeypatch, capsys) -> None:
    for name in ("01.flac", "02.flac", "03.flac"):
        _write_flac(tmp_path / name, year="2001")
    opened = tags.MutagenFile

    def unreadable_second(path, *args, **kwargs):
        if os.path.basename(path) == "02.flac":
            raise OSError(13, "Permission denied")
        return opened(path, *args, **kwargs)

    monkeypatch.setattr(tags, "MutagenFile", unreadable_second)

    with pytest.raises(UploadError, match="inconsistent"):
        tags.standardize_tags(str(tmp_path))

    out = capsys.readouterr().out
    assert "Already changed: 01.flac" in out
    assert "Not changed: 02.flac, 03.flac" in out
    assert "May be partially changed" not in out
    assert "date" in FLAC(tmp_path / "01.flac") and "year" in FLAC(tmp_path / "03.flac")
