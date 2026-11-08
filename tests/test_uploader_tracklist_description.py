"""What ``generate_description`` writes from the files, byte for byte: it goes to the trackers, and cross-upload calls
it with no album artists, comment or URLs."""

from types import SimpleNamespace
from typing import Any

import pytest

from salmon import cfg
from salmon.uploader.upload import generate_description, generate_source_links

HEADING = "[b][size=4]Tracklist[/size][/b]\n"


def _file(duration: int, **tags: Any) -> dict[str, Any]:
    return {
        "duration": duration,
        "t": SimpleNamespace(
            **{
                "discnumber": "1",
                "tracknumber": "1",
                "disctotal": None,
                "title": "Song",
                "artist": ["Artist"],
                "artists": None,
                "remixer": None,
                **tags,
            }
        ),
    }


def _files(*files: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f"{n:02d}.flac": file for n, file in enumerate(files, 1)}


NO_ARTISTS: dict[str, Any] = {"comment": None, "urls": []}
ONE_ARTIST: dict[str, Any] = {"comment": None, "urls": [], "rls_type": "Album", "artists": [("Artist", "main")]}


def test_as_before_one_track_has_no_total() -> None:
    assert generate_description(_files(_file(200)), NO_ARTISTS) == f"{HEADING}[b]01.[/b] Artist - Song [i](3:20)[/i]\n"


def test_as_before_the_total_adds_up_past_an_hour() -> None:
    files = _files(_file(3599, tracknumber="1"), _file(62, tracknumber="2"))

    assert generate_description(files, NO_ARTISTS) == (
        f"{HEADING}[b]01.[/b] Artist - Song [i](59:59)[/i]\n[b]02.[/b] Artist - Song [i](1:02)[/i]\n"
        "\n[b]Total length: [/b]61:01\n"
    )


def test_as_before_a_file_under_a_second_still_shows_a_length() -> None:
    files = _files(_file(0, tracknumber="1"), _file(0, tracknumber="2"))

    assert generate_description(files, NO_ARTISTS) == (
        f"{HEADING}[b]01.[/b] Artist - Song [i](0:00)[/i]\n[b]02.[/b] Artist - Song [i](0:00)[/i]\n"
        "\n[b]Total length: [/b]0:00\n"
    )


def test_as_before_a_file_with_no_artist_keeps_the_separator() -> None:
    assert generate_description(_files(_file(200, artist=[])), NO_ARTISTS) == (
        f"{HEADING}[b]01.[/b]  - Song [i](3:20)[/i]\n"
    )


def test_as_before_a_track_crediting_only_the_album_leaves_its_artist_out() -> None:
    assert generate_description(_files(_file(200)), ONE_ARTIST) == f"{HEADING}[b]01.[/b] Song [i](3:20)[/i]\n"


def test_as_before_several_discs_number_with_the_disc() -> None:
    files = _files(_file(60, discnumber="1", tracknumber="3/10"), _file(60, discnumber="2", tracknumber="1"))

    assert generate_description(files, NO_ARTISTS) == (
        f"{HEADING}[b]01-03.[/b] Artist - Song [i](1:00)[/i]\n[b]02-01.[/b] Artist - Song [i](1:00)[/i]\n"
        "\n[b]Total length: [/b]2:00\n"
    )


def test_as_before_a_disc_with_no_number_is_listed_as_the_first() -> None:
    files = _files(_file(60, discnumber=None, tracknumber="1"), _file(60, discnumber="2", tracknumber="1"))

    assert generate_description(files, NO_ARTISTS).startswith(f"{HEADING}[b]01-01.[/b] ")


def test_as_before_a_number_that_is_no_number_stays_as_written() -> None:
    assert generate_description(_files(_file(60, tracknumber="A1")), NO_ARTISTS) == (
        f"{HEADING}[b]A1.[/b] Artist - Song [i](1:00)[/i]\n"
    )


def test_as_before_artist_tags_wrap_each_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload.description, "artist_tags_in_tracklist", True)

    assert generate_description(_files(_file(60, artist=["A", "B"])), NO_ARTISTS) == (
        f"{HEADING}[b]01.[/b] [artist]A[/artist], [artist]B[/artist] - Song [i](1:00)[/i]\n"
    )


@pytest.mark.parametrize("urls", [None, []], ids=["None", "empty"])
def test_as_before_no_comment_and_no_links_end_at_the_total(urls: list[str] | None) -> None:
    files = _files(_file(60, tracknumber="1"), _file(60, tracknumber="2"))

    assert generate_description(files, {"comment": "", "urls": urls}).endswith("\n[b]Total length: [/b]2:00\n")


def test_as_before_the_comment_and_links_follow_the_total() -> None:
    url = "https://www.qobuz.com/album/-/x"
    files = _files(_file(60, tracknumber="1"), _file(60, tracknumber="2"))

    assert generate_description(files, {"comment": "A note", "urls": [url]}) == (
        f"{HEADING}[b]01.[/b] Artist - Song [i](1:00)[/i]\n[b]02.[/b] Artist - Song [i](1:00)[/i]\n"
        f"\n[b]Total length: [/b]2:00\n\nA note\n\n[b]More info:[/b] {generate_source_links([url])}"
    )
