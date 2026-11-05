"""An MP3 transcode output name must not keep a stale {resolution} token from the source name."""

import pytest

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


def test_a_bare_bracketed_token_leaves_no_empty_brackets(monkeypatch) -> None:
    # "Album [24-192]" has nothing else in the brackets: token removal must take them too.
    monkeypatch.setattr(
        transcoding,
        "gather_audio_info",
        lambda path: {"01.flac": {"precision": 24, "sample rate": 192000}},
    )
    source = "/downloads/Album [24-192]"

    new_path = transcoding._build_output_path(source, "V0")

    assert "[]" not in new_path
    assert new_path == "/downloads/Album [MP3 V0]"


def test_only_the_measured_token_is_removed_not_a_look_alike_in_the_title(monkeypatch) -> None:
    # A "24-96" in the album title is not a resolution token unless the files say so.
    monkeypatch.setattr(
        transcoding,
        "gather_audio_info",
        lambda path: {"01.flac": {"precision": 16, "sample rate": 44100}},
    )
    source = "/downloads/Artist - 24-96 (2024) [WEB FLAC]"

    assert transcoding._build_output_path(source, "V0") == "/downloads/Artist - 24-96 (2024) [WEB MP3 V0]"


def test_numbers_in_the_title_are_not_taken_for_the_token(monkeypatch) -> None:
    monkeypatch.setattr(
        transcoding, "gather_audio_info", lambda path: {"01.flac": {"precision": 24, "sample rate": 48000}}
    )
    source = "/downloads/Artist - Complete Recordings 1924-48 (2021) [WEB 24bit FLAC]"

    new_path = transcoding._build_output_path(source, "320")

    assert new_path == "/downloads/Artist - Complete Recordings 1924-48 (2021) [WEB MP3 320]"


def test_a_token_first_in_its_brackets_leaves_no_stray_space(monkeypatch) -> None:
    _stub_24_96(monkeypatch)

    new_path = transcoding._build_output_path("/downloads/Artist - Title (2024) [24-96 WEB FLAC]", "V0")

    assert new_path == "/downloads/Artist - Title (2024) [WEB MP3 V0]"


def _template(monkeypatch, template: str) -> None:
    monkeypatch.setattr(transcoding.cfg.upload.formatting, "folder_template", template)


def test_a_title_reading_like_the_token_is_kept_when_the_template_has_no_resolution(monkeypatch) -> None:
    _stub_24_96(monkeypatch)
    _template(monkeypatch, "{artists} - {title} ({year}) [{source} {format}]")

    new_path = transcoding._build_output_path("/downloads/Artist - 24-96 (2024) [WEB FLAC]", "V0")

    assert new_path == "/downloads/Artist - 24-96 (2024) [WEB MP3 V0]"


def test_only_the_bracketed_token_goes_beside_a_title_that_reads_like_it(monkeypatch) -> None:
    _stub_24_96(monkeypatch)
    _template(monkeypatch, "[{source} FLAC {resolution}]")

    new_path = transcoding._build_output_path("/downloads/Artist - 24-96 (2024) [WEB FLAC 24-96]", "V0")

    assert new_path == "/downloads/Artist - 24-96 (2024) [WEB MP3 V0]"


def test_an_unbracketed_token_is_dropped_when_the_template_places_resolution(monkeypatch) -> None:
    _stub_24_96(monkeypatch)
    _template(monkeypatch, "{artists} - {title} {resolution}")

    new_path = transcoding._build_output_path("/downloads/Artist - Title 24-96", "V0")

    assert new_path == "/downloads/Artist - Title [MP3 V0]"


def _stub_16_44(monkeypatch) -> None:
    monkeypatch.setattr(
        transcoding,
        "gather_audio_info",
        lambda path: {"01.flac": {"precision": 16, "sample rate": 44100}},
    )


@pytest.mark.parametrize(
    ("source", "bitrate", "expected"),
    [
        ("Roberta Flack - Album (2020) [WEB FLAC]", "V0", "Roberta Flack - Album (2020) [WEB MP3 V0]"),
        ("Flacco - Album (2020) [WEB 24bit FLAC]", "V0", "Flacco - Album (2020) [WEB MP3 V0]"),
        ("Flaco Jimenez - Album (2020) [CD FLAC]", "320", "Flaco Jimenez - Album (2020) [CD MP3 320]"),
        ("Aflac (2020)", "V0", "Aflac (2020) [MP3 V0]"),
        ("Éflac (2020)", "V0", "Éflac (2020) [MP3 V0]"),
        ("Flossless (2020)", "V0", "Flossless (2020) [MP3 V0]"),
        ("A - FLAC (2020) [WEB FLAC]", "V0", "A - FLAC (2020) [WEB MP3 V0]"),
        ("A - 24bit FLAC (2020) [WEB FLAC]", "V0", "A - 24bit FLAC (2020) [WEB MP3 V0]"),
        ("A - Lossless Love (2020) [WEB FLAC]", "V0", "A - Lossless Love (2020) [WEB MP3 V0]"),
        ("A - Lossless Love (2020) [WEB FLAC Lossless]", "V0", "A - Lossless Love (2020) [WEB MP3 V0]"),
        ("A - Lossless Love (2020) [WEB Lossless]", "320", "A - Lossless Love (2020) [WEB 320] [MP3]"),
        ("A - Losslessness (2020) [WEB FLAC]", "V0", "A - Losslessness (2020) [WEB MP3 V0]"),
        ("A - Album (2020) [WEB FLAC] (Lossless Love)", "V0", "A - Album (2020) [WEB MP3 V0] (Lossless Love)"),
        ("A - [Lossless] FLAC", "V0", "A - [Lossless] MP3 V0"),
        ("A - (Lossless) FLAC", "V0", "A - (Lossless) MP3 V0"),
        ("A - {Lossless} FLAC", "V0", "A - {Lossless} MP3 V0"),
        ("A - Lossless [FLAC]", "V0", "A - Lossless [MP3 V0]"),
        ("A - Lossless (FLAC)", "V0", "A - Lossless (MP3 V0)"),
        ("A - Lossless {FLAC}", "V0", "A - Lossless {MP3 V0}"),
        ("Flacid (2020)", "V0", "Flacid (2020) [MP3 V0]"),
        ("Losslessly (2020)", "V0", "Losslessly (2020) [MP3 V0]"),
        ("[FLAC] A - Flac Attack (2020)", "V0", "[MP3 V0] A - Flac Attack (2020)"),
        ("A - Album (2020) [WEB FLAC] {Flac Records}", "V0", "A - Album (2020) [WEB MP3 V0] {Flac Records}"),
        ("[WEB FLAC Lossless] A - Lossless Love", "V0", "[WEB MP3 V0] A - Lossless Love"),
        ("A - [FLAC] (2020) [WEB FLAC]", "V0", "A - [FLAC] (2020) [WEB MP3 V0]"),
        ("A - FLAC - WEB FLAC", "V0", "A - FLAC - WEB MP3 V0"),
        ("A - Flac Love [WEB] FLAC", "V0", "A - Flac Love [WEB] MP3 V0"),
        ("[WEB FLAC] A - Flac Attack (2020) [Sony]", "V0", "[WEB MP3 V0] A - Flac Attack (2020) [Sony]"),
        ("A - B (2020) [WEB FLAC] [Flac Records]", "V0", "A - B (2020) [WEB MP3 V0] [Flac Records]"),
        ("A - B (2020) [WEB FLAC 24-96] [Flac Records]", "V0", "A - B (2020) [WEB MP3 V0 24-96] [Flac Records]"),
        ("A - B (2020) [WEB Lossless] [Lossless Records]", "V0", "A - B (2020) [WEB V0] [Lossless Records] [MP3]"),
        ("A - B (2020) [FLAC Remaster]", "V0", "A - B (2020) [MP3 V0 Remaster]"),
        ("A - B (2020) [WEB FLAC Remaster] Flac Records", "V0", "A - B (2020) [WEB MP3 V0 Remaster] Flac Records"),
    ],
)
def test_the_format_word_is_swapped_and_the_names_are_kept(monkeypatch, source, bitrate, expected) -> None:
    _stub_16_44(monkeypatch)

    assert transcoding._build_output_path(f"/downloads/{source}", bitrate) == f"/downloads/{expected}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("A - Album (2020) [WEB FLAC]", "A - Album (2020) [WEB MP3 V0]"),
        ("A - Album (2020) [web flac]", "A - Album (2020) [web MP3 V0]"),
        ("A - Album (2020) [WEB 24bit FLAC]", "A - Album (2020) [WEB MP3 V0]"),
        ("A - Album (2020) [WEB 24 bit FLAC]", "A - Album (2020) [WEB MP3 V0]"),
        ("A - Album (2020) [WEB FLAC Lossless]", "A - Album (2020) [WEB MP3 V0]"),
        ("A - Album (2020) [WEB Lossless FLAC]", "A - Album (2020) [WEB V0 MP3]"),
        ("A - Album (2020) [Lossless WEB FLAC]", "A - Album (2020) [V0 WEB MP3]"),
        ("A - Album (2020) [FLAC WEB Lossless]", "A - Album (2020) [MP3 WEB V0]"),
        ("A - Album (2020) [WEB Lossless]", "A - Album (2020) [WEB V0] [MP3]"),
        ("A - Album (2020) [WEB lossless]", "A - Album (2020) [WEB V0] [MP3]"),
        ("A - Album FLAC", "A - Album MP3 V0"),
        ("Artist.Album.2020.WEB.FLAC-GROUP", "Artist.Album.2020.WEB.MP3 V0-GROUP"),
        ("Artist_Album_2020_FLAC_GROUP", "Artist_Album_2020_MP3 V0_GROUP"),
        ("Artist_Album_2020_Lossless_GROUP", "Artist_Album_2020_V0_GROUP [MP3]"),
        ("A - Album (2020)", "A - Album (2020) [MP3 V0]"),
    ],
)
def test_names_without_the_letters_elsewhere_come_out_as_before(monkeypatch, source, expected) -> None:
    _stub_16_44(monkeypatch)

    assert transcoding._build_output_path(f"/downloads/{source}", "V0") == f"/downloads/{expected}"
