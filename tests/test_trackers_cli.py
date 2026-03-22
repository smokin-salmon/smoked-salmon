from salmon.trackers import normalize_trackers


def test_normalize_trackers_accepts_multiple_values_and_short_names() -> None:
    assert normalize_trackers(("red", "OPS", "o")) == ["RED", "OPS"]
