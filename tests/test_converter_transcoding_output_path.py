"""An MP3 transcode output name must not keep a stale {resolution} token from the source name."""

from salmon.converter import transcoding

# What the tag/foldername step would produce with a template combining {format} and {resolution}:
# a 24-bit/96kHz FLAC folder.
SOURCE_24_96 = "/downloads/Artist - Title (2024) [WEB 24bit FLAC 24-96]"
SOURCE_NO_TOKEN = "/downloads/Artist - Title (2024) [WEB 24bit FLAC]"


def _stub_24_96(monkeypatch) -> None:
    monkeypatch.setattr(
        transcoding,
        "gather_audio_info",
        lambda path: {"01.flac": {"precision": 24, "sample rate": 96000}},
    )


def test_transcode_to_v0_drops_the_resolution_token(monkeypatch) -> None:
    _stub_24_96(monkeypatch)

    new_path = transcoding._build_output_path(SOURCE_24_96, "V0")

    assert new_path == "/downloads/Artist - Title (2024) [WEB MP3 V0]"


def test_transcode_to_320_drops_the_resolution_token(monkeypatch) -> None:
    _stub_24_96(monkeypatch)

    new_path = transcoding._build_output_path(SOURCE_24_96, "320")

    assert new_path == "/downloads/Artist - Title (2024) [WEB MP3 320]"


def test_a_name_without_a_resolution_token_is_unaffected(monkeypatch) -> None:
    # Regression: the default template (no {resolution}) must transcode exactly as before.
    _stub_24_96(monkeypatch)

    assert transcoding._build_output_path(SOURCE_NO_TOKEN, "V0") == "/downloads/Artist - Title (2024) [WEB MP3 V0]"
    assert transcoding._build_output_path(SOURCE_NO_TOKEN, "320") == "/downloads/Artist - Title (2024) [WEB MP3 320]"


def test_a_resolution_only_name_transcodes_with_no_token(monkeypatch) -> None:
    # Same "[{source} FLAC {resolution}]" style source used in the downconverting tests.
    monkeypatch.setattr(
        transcoding,
        "gather_audio_info",
        lambda path: {"01.flac": {"precision": 24, "sample rate": 192000}},
    )
    source = "/downloads/Artist - Album (2020) [WEB FLAC 24-192]"

    assert transcoding._build_output_path(source, "V0") == "/downloads/Artist - Album (2020) [WEB MP3 V0]"


def test_only_the_measured_token_is_removed_not_a_look_alike_in_the_title(monkeypatch) -> None:
    # A "24-96" in the album title is not a resolution token unless the files say so.
    monkeypatch.setattr(
        transcoding,
        "gather_audio_info",
        lambda path: {"01.flac": {"precision": 16, "sample rate": 44100}},
    )
    source = "/downloads/Artist - 24-96 (2024) [WEB FLAC]"

    assert transcoding._build_output_path(source, "V0") == "/downloads/Artist - 24-96 (2024) [WEB MP3 V0]"
