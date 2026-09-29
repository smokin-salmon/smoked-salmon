"""A downconversion output name must not keep a stale {resolution} token from the source name."""

from salmon.converter import downconverting

# What the tag/foldername step would produce with a template combining {format} and {resolution}
# ("24bit FLAC 24-96"): a 24-bit/96kHz FLAC folder.
SOURCE_24_96 = "/downloads/Artist - Title (2024) [WEB 24bit FLAC 24-96]"
SOURCE_NO_TOKEN = "/downloads/Artist - Title (2024) [WEB 24bit FLAC]"


def _stub_24_96(monkeypatch) -> None:
    monkeypatch.setattr(
        downconverting,
        "gather_audio_info",
        lambda path: {"01.flac": {"precision": 24, "sample rate": 96000}},
    )


def test_downconvert_to_16_44_drops_the_resolution_token(monkeypatch) -> None:
    _stub_24_96(monkeypatch)

    new_path = downconverting._build_output_path(SOURCE_24_96, 16, None)

    assert new_path == "/downloads/Artist - Title (2024) [WEB FLAC]"


def test_downconvert_to_24_48_replaces_the_resolution_token(monkeypatch) -> None:
    _stub_24_96(monkeypatch)

    new_path = downconverting._build_output_path(SOURCE_24_96, 24, 48000)

    assert new_path == "/downloads/Artist - Title (2024) [WEB 24-48]"


def test_a_name_without_a_resolution_token_is_unaffected(monkeypatch) -> None:
    # Regression: the default template (no {resolution}) must convert exactly as before.
    _stub_24_96(monkeypatch)

    assert downconverting._build_output_path(SOURCE_NO_TOKEN, 16, None) == "/downloads/Artist - Title (2024) [WEB FLAC]"
    assert (
        downconverting._build_output_path(SOURCE_NO_TOKEN, 24, 48000) == "/downloads/Artist - Title (2024) [WEB 24-48]"
    )


def test_a_32_bit_source_token_is_also_dropped(monkeypatch) -> None:
    # The token comes from the measured bit depth, so it is not limited to 16/24-bit guesses.
    monkeypatch.setattr(
        downconverting,
        "gather_audio_info",
        lambda path: {"01.flac": {"precision": 32, "sample rate": 96000}},
    )
    source = "/downloads/Artist - Title (2024) [WEB 24bit FLAC 32-96]"

    assert downconverting._build_output_path(source, 16, None) == "/downloads/Artist - Title (2024) [WEB FLAC]"


def test_only_the_measured_token_is_removed_not_a_look_alike_in_the_title(monkeypatch) -> None:
    # A "24-96" in the album title is not a resolution token unless the files say so.
    monkeypatch.setattr(
        downconverting,
        "gather_audio_info",
        lambda path: {"01.flac": {"precision": 16, "sample rate": 44100}},
    )
    source = "/downloads/Artist - 24-96 (2024) [WEB FLAC]"

    assert downconverting._build_output_path(source, 16, None) == "/downloads/Artist - 24-96 (2024) [WEB 16bit FLAC]"
