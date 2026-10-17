"""Renaming a batch without replacing a file: the plan before the first rename, and the check at each."""

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
