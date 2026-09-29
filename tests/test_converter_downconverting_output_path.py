"""A downconversion output name must not keep a stale {resolution} token from the source name."""

from salmon.converter.downconverting import _build_output_path

# What the tag/foldername step would produce with a template combining {format} and {resolution}
# ("24bit FLAC 24-96"): a 24-bit/96kHz FLAC folder.
SOURCE_24_96 = "/downloads/Artist - Title (2024) [WEB 24bit FLAC 24-96]"


def test_downconvert_to_16_44_drops_the_resolution_token() -> None:
    new_path = _build_output_path(SOURCE_24_96, 16, None)

    assert new_path == "/downloads/Artist - Title (2024) [WEB FLAC]"


def test_downconvert_to_24_48_replaces_the_resolution_token() -> None:
    new_path = _build_output_path(SOURCE_24_96, 24, 48000)

    assert new_path == "/downloads/Artist - Title (2024) [WEB 24-48]"


def test_a_name_without_a_resolution_token_is_unaffected() -> None:
    # Regression: the default template (no {resolution}) must convert exactly as before.
    source = "/downloads/Artist - Title (2024) [WEB 24bit FLAC]"

    assert _build_output_path(source, 16, None) == "/downloads/Artist - Title (2024) [WEB FLAC]"
    assert _build_output_path(source, 24, 48000) == "/downloads/Artist - Title (2024) [WEB 24-48]"
