from salmon.trackers.ops import OpsApi


def test_ops_set_split_choice_marks_prompt_as_handled() -> None:
    tracker = OpsApi()

    tracker.set_split_choice(True)

    assert tracker._use_split is True
    assert tracker._split_prompted is True
