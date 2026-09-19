from salmon.checks.flac_compliance import get_flac_warnings


def test_clean_flac24_passes() -> None:
    info = {
        "01 - T.flac": {"precision": 24, "sample rate": 96000},
        "02 - T.flac": {"precision": 24, "sample rate": 96000},
    }
    files = ["01 - T.flac", "02 - T.flac"]
    assert get_flac_warnings(files, files, info, "WEB") == []


def test_16bit_high_rate_warns() -> None:
    w = get_flac_warnings(["a.flac"], ["a.flac"], {"a.flac": {"precision": 16, "sample rate": 96000}}, "WEB")
    assert any("16-bit above" in x for x in w)


def test_vinyl_needs_lineage() -> None:
    w = get_flac_warnings(["a.flac"], ["a.flac"], {"a.flac": {"precision": 24, "sample rate": 96000}}, "Vinyl")
    assert any("lineage" in x for x in w)
