"""Renaming a batch without replacing a file: the plan before the first rename, and the check at each."""

import errno

import pytest

from salmon.tagger import renames
from salmon.tagger.renames import RenameStopped, plan_renames, rename_all


def _files(root, *names):
    for name in names:
        (root / name).write_text(name)
    return [str(root / name) for name in names]


def test_plan_puts_a_file_moving_away_before_the_one_taking_its_name(tmp_path) -> None:
    a, b = _files(tmp_path, "a", "b")
    c = str(tmp_path / "c")

    assert plan_renames([(a, b), (b, c)]) == ([(b, c), (a, b)], [])


def test_plan_names_a_new_name_given_twice_or_held_by_a_file_left_in_place(tmp_path) -> None:
    a, b, c = _files(tmp_path, "a", "b", "c")
    d = str(tmp_path / "d")

    assert plan_renames([(a, d), (b, d)])[1] == [d]
    assert plan_renames([(a, c)])[1] == [c]


def test_plan_names_a_swap(tmp_path) -> None:
    a, b = _files(tmp_path, "a", "b")

    assert plan_renames([(a, b), (b, a)])[1] == [b, a]


def test_plan_compares_names_without_case_on_a_volume_that_ignores_it(tmp_path, monkeypatch) -> None:
    a, b = _files(tmp_path, "a", "b")
    upper, lower = str(tmp_path / "X"), str(tmp_path / "x")

    monkeypatch.setattr(renames, "_folds_case", lambda _path: False)
    assert plan_renames([(a, upper), (b, lower)])[1] == []
    monkeypatch.setattr(renames, "_folds_case", lambda _path: True)
    assert plan_renames([(a, upper), (b, lower)])[1] == [upper, lower]


def test_rename_all_stops_at_a_name_in_use_and_reports_the_rest(tmp_path) -> None:
    a, b, c = _files(tmp_path, "a", "b", "c")
    d, e = str(tmp_path / "d"), str(tmp_path / "e")
    (tmp_path / "e").write_text("someone else's")

    with pytest.raises(RenameStopped) as stopped:
        rename_all([(a, d), (b, e), (c, str(tmp_path / "f"))])

    assert sorted(path.name for path in tmp_path.iterdir()) == ["b", "c", "d", "e"]
    assert (tmp_path / "e").read_text() == "someone else's"
    assert stopped.value.report(str(tmp_path)).splitlines()[1:] == ["Renamed: a >>> d", "Not renamed: b, c"]


def test_rename_all_never_replaces_a_file_made_after_the_check(tmp_path, monkeypatch) -> None:
    a, taken = _files(tmp_path, "a", "taken")
    monkeypatch.setattr(renames.os.path, "lexists", lambda _path: False)

    with pytest.raises(RenameStopped):
        rename_all([(a, taken)])

    assert (tmp_path / "taken").read_text() == "taken"
    assert (tmp_path / "a").read_text() == "a"


def test_a_file_replaced_during_its_rename_is_not_deleted(tmp_path, monkeypatch) -> None:
    (a,) = _files(tmp_path, "a")
    link = renames.os.link

    def link_then_swap(old, new):
        link(old, new)
        renames.os.rename(old, tmp_path / "moved away")
        (tmp_path / "a").write_text("someone else's")

    monkeypatch.setattr(renames.os, "link", link_then_swap)

    with pytest.raises(RenameStopped, match="changed during its rename"):
        rename_all([(a, str(tmp_path / "b"))])

    assert (tmp_path / "a").read_text() == "someone else's"
    assert (tmp_path / "b").read_text() == "a"


def test_rename_all_renames_where_the_volume_has_no_hard_links(tmp_path, monkeypatch) -> None:
    (a,) = _files(tmp_path, "a")

    def no_links(_old, _new):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(renames.os, "link", no_links)

    rename_all([(a, str(tmp_path / "b"))])

    assert sorted(path.name for path in tmp_path.iterdir()) == ["b"]


def test_a_link_refused_for_another_reason_renames_nothing(tmp_path, monkeypatch) -> None:
    (a,) = _files(tmp_path, "a")

    def denied(_old, _new):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(renames.os, "link", denied)

    with pytest.raises(RenameStopped, match="Permission denied"):
        rename_all([(a, str(tmp_path / "b"))])

    assert sorted(path.name for path in tmp_path.iterdir()) == ["a"]


def test_a_file_whose_old_name_cannot_go_keeps_only_that_name(tmp_path, monkeypatch) -> None:
    (a,) = _files(tmp_path, "a")
    unlink = renames.os.unlink

    def unlink_but_not_a(path):
        if path == a:
            raise PermissionError(13, "Permission denied", path)
        unlink(path)

    monkeypatch.setattr(renames.os, "unlink", unlink_but_not_a)

    with pytest.raises(RenameStopped):
        rename_all([(a, str(tmp_path / "b"))])

    assert sorted(path.name for path in tmp_path.iterdir()) == ["a"]


def test_rename_all_keeps_a_symlink_a_symlink(tmp_path) -> None:
    (target,) = _files(tmp_path, "target")
    (tmp_path / "a").symlink_to(target)

    rename_all([(str(tmp_path / "a"), str(tmp_path / "b"))])

    assert (tmp_path / "b").is_symlink()
    assert not (tmp_path / "a").is_symlink()


def test_rename_all_renames_a_folder_with_what_it_holds(tmp_path) -> None:
    (tmp_path / "Disc 1").mkdir()
    (tmp_path / "Disc 1" / "01.flac").write_text("audio")

    rename_all([(str(tmp_path / "Disc 1"), str(tmp_path / "CD1"))])

    assert (tmp_path / "CD1" / "01.flac").read_text() == "audio"
    assert not (tmp_path / "Disc 1").exists()


@pytest.mark.parametrize("taken", ["empty folder", "folder", "file"])
def test_a_folder_is_never_renamed_onto_a_name_in_use(tmp_path, taken: str) -> None:
    (tmp_path / "Disc 1").mkdir()
    (tmp_path / "Disc 1" / "01.flac").write_text("audio")
    if taken == "file":
        (tmp_path / "CD1").write_text("theirs")
    else:
        (tmp_path / "CD1").mkdir()
    if taken == "folder":
        (tmp_path / "CD1" / "notes.txt").write_text("theirs")

    with pytest.raises(RenameStopped):
        rename_all([(str(tmp_path / "Disc 1"), str(tmp_path / "CD1"))])

    assert (tmp_path / "Disc 1" / "01.flac").read_text() == "audio"
    assert not (tmp_path / "CD1" / "01.flac").exists()


def test_rename_all_changes_only_the_case_of_a_name_on_a_volume_that_ignores_it(tmp_path) -> None:
    (a,) = _files(tmp_path, "a.FLAC")
    if not renames._folds_case(a):
        pytest.skip("this volume keeps names apart by case")

    rename_all([(a, str(tmp_path / "a.flac"))])

    assert [path.name for path in tmp_path.iterdir()] == ["a.flac"]


def test_a_second_hard_link_to_the_file_is_not_the_file_itself(tmp_path) -> None:
    # os.rename onto another link to the same file does nothing and leaves both names.
    (a,) = _files(tmp_path, "a")
    b = str(tmp_path / "b")
    renames.os.link(a, b)

    assert plan_renames([(a, b)])[1] == [b]
    with pytest.raises(RenameStopped):
        rename_all([(a, b)])

    assert sorted(path.name for path in tmp_path.iterdir()) == ["a", "b"]


def test_a_file_left_under_both_names_is_reported_so(tmp_path, monkeypatch) -> None:
    (a,) = _files(tmp_path, "a")

    def no_unlink(path):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(renames.os, "unlink", no_unlink)

    with pytest.raises(RenameStopped, match="left under both names"):
        rename_all([(a, str(tmp_path / "b"))])
