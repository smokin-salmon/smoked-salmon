"""An MP3 transcode output name must not keep a stale {resolution} token from the source name."""

from salmon.converter.transcoding import _build_output_path

# What the tag/foldername step would produce with a template combining {format} and {resolution}:
# a 24-bit/96kHz FLAC folder.
SOURCE_24_96 = "/downloads/Artist - Title (2024) [WEB 24bit FLAC 24-96]"


def test_transcode_to_v0_drops_the_resolution_token() -> None:
    new_path = _build_output_path(SOURCE_24_96, "V0")

    assert new_path == "/downloads/Artist - Title (2024) [WEB MP3 V0]"


def test_transcode_to_320_drops_the_resolution_token() -> None:
    new_path = _build_output_path(SOURCE_24_96, "320")

    assert new_path == "/downloads/Artist - Title (2024) [WEB MP3 320]"


def test_a_name_without_a_resolution_token_is_unaffected() -> None:
    # Regression: the default template (no {resolution}) must transcode exactly as before.
    source = "/downloads/Artist - Title (2024) [WEB 24bit FLAC]"

    assert _build_output_path(source, "V0") == "/downloads/Artist - Title (2024) [WEB MP3 V0]"
    assert _build_output_path(source, "320") == "/downloads/Artist - Title (2024) [WEB MP3 320]"
