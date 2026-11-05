"""`salmon descgen` closes every BBCode tag it opens, turns away what is not a store URL, and keeps a track's guest.

No source is contacted.
"""

import re

import anyio
import pytest
from asyncclick.testing import CliRunner

import salmon.commands
from salmon import cfg
from salmon.commands import build_description, descgen
from salmon.tagger.sources import METASOURCES

METADATA = {
    "artists": [],
    "tracks": {
        "1": {
            "1": {"artists": [("Artist", "main")], "title": "First"},
            "2": {"artists": [("Artist", "main")], "title": "Second"},
        }
    },
    "comment": None,
    "urls": [],
}


def test_descgen_closes_the_tracklist_heading_size(monkeypatch, capsys) -> None:
    async def fake_run_metadata(url, return_source_name=False):
        return METADATA, "Qobuz"

    monkeypatch.setattr(salmon.commands, "run_metadata", fake_run_metadata)
    monkeypatch.setattr(salmon.commands, "combine_metadatas", lambda *_: METADATA)
    monkeypatch.setattr(salmon.commands, "clean_metadata", lambda metadata: metadata)
    monkeypatch.setattr(cfg.upload.description, "copy_uploaded_url_to_clipboard", False)
    assert descgen.callback is not None

    anyio.run(descgen.callback, ("https://www.qobuz.com/album/x",))

    out = capsys.readouterr().out
    assert "[b][size=4]Tracklist[/size][/b]" in out
    assert len(re.findall(r"\[size=\d+\]", out)) == len(re.findall(r"\[/size\]", out))


def _run_descgen(monkeypatch, *args: str):
    async def no_scrape(url, return_source_name=False):
        raise AssertionError(f"descgen scraped {url}")

    monkeypatch.setattr(salmon.commands, "run_metadata", no_scrape)

    async def run():
        return await CliRunner().invoke(descgen, list(args))

    return anyio.run(run)


def test_descgen_given_a_folder_says_it_takes_release_urls(monkeypatch, tmp_path) -> None:
    album = tmp_path / "Artist - Album (2024) [FLAC]"
    album.mkdir()

    result = _run_descgen(monkeypatch, str(album))

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert f"{album} is a folder; descgen takes release URLs from " in lines[0]
    assert "`salmon up` writes the description itself" in lines[0]
    assert all(name in lines[0] for name in METASOURCES)


def test_descgen_given_an_unsupported_url_lists_the_sources(monkeypatch) -> None:
    url = "https://shop.example.com/releases/123"

    result = _run_descgen(monkeypatch, url, "https://www.qobuz.com/us-en/album/x/abc123")

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "Argument 1 (shop.example.com) is not a release URL descgen supports" in lines[0]
    assert all(name in lines[0] for name in METASOURCES)


@pytest.mark.parametrize(
    "url",
    [
        "https://tracker.example/12345678901234567890abcdef/announce",
        "https://tracker.example/torrents.php?action=download&id=1&authkey=SECRETKEY&torrent_pass=SECRETPASS",
        "not a url, but SECRETKEY",
    ],
)
def test_descgen_never_echoes_a_refused_url(monkeypatch, url: str) -> None:
    result = _run_descgen(monkeypatch, "https://www.qobuz.com/us-en/album/x/abc123", url)

    assert result.exit_code == 1
    assert "Argument 2 (" in result.output
    assert "SECRET" not in result.output
    assert "announce" not in result.output
    assert "12345678901234567890abcdef" not in result.output


MURPHY = ("Sullivan Murphy", "main")
SIMPSON = ("Dierdre Simpson", "guest")


def _release(*discs: list[tuple[list[tuple[str, str]], str]], rls_type: str = "Album") -> dict:
    """A store's release: each disc a list of a track's (credits, title)."""
    tracks = {
        str(dnum): {
            str(tnum): {"artists": list(credits), "title": title} for tnum, (credits, title) in enumerate(disc, 1)
        }
        for dnum, disc in enumerate(discs, 1)
    }
    return {
        "rls_type": rls_type,
        "tracks": tracks,
        "comment": None,
        "urls": [],
    }


def _description(monkeypatch, metadata: dict, guests_in_track_title: bool) -> str:
    async def fake_run_metadata(url, return_source_name=False):
        return metadata, "Qobuz"

    monkeypatch.setattr(cfg.upload.formatting, "guests_in_track_title", guests_in_track_title)
    monkeypatch.setattr(salmon.commands, "run_metadata", fake_run_metadata)
    monkeypatch.setattr(salmon.commands, "combine_metadatas", lambda *_: metadata)
    monkeypatch.setattr(salmon.commands, "clean_metadata", lambda metadata: metadata)
    return anyio.run(build_description, ["https://www.qobuz.com/album/x"])


HEADING = "[b][size=4]Tracklist[/size][/b]\n\n"


def test_guest_goes_in_the_title_on_a_line_that_names_the_artist(monkeypatch) -> None:
    # The artist string leaves the guest to the title, and only a retag put it there, so the guest was lost.
    release = _release([([MURPHY, SIMPSON], "Song 1")], rls_type="Compilation")

    description = _description(monkeypatch, release, guests_in_track_title=True)

    assert description == f"{HEADING}[b]01.[/b] Sullivan Murphy - Song 1 (feat. Dierdre Simpson)\n"


def test_guest_goes_in_the_title_of_a_track_by_the_album_and_a_guest(monkeypatch) -> None:
    release = _release([([MURPHY], "Song 1"), ([MURPHY, SIMPSON], "Song 2")])

    description = _description(monkeypatch, release, guests_in_track_title=True)

    assert description == f"{HEADING}[b]01.[/b] Song 1\n[b]02.[/b] Sullivan Murphy - Song 2 (feat. Dierdre Simpson)\n"


def test_guest_goes_in_the_title_on_each_disc(monkeypatch) -> None:
    release = _release([([MURPHY], "Song 1")], [([MURPHY, SIMPSON], "Song 2")])

    description = _description(monkeypatch, release, guests_in_track_title=True)

    assert description == (
        f"{HEADING}[b]01-01.[/b] Song 1\n[b]02-01.[/b] Sullivan Murphy - Song 2 (feat. Dierdre Simpson)\n"
    )


def test_guest_in_the_title_comes_before_a_remix_as_a_retag_puts_it(monkeypatch) -> None:
    release = _release([([MURPHY], "Song 1"), ([MURPHY, SIMPSON], "Song 2 (Remi Xer Remix)")])

    description = _description(monkeypatch, release, guests_in_track_title=True)

    assert "[b]02.[/b] Sullivan Murphy - Song 2 (feat. Dierdre Simpson) (Remi Xer Remix)\n" in description


@pytest.mark.parametrize(
    ("discs", "expected"),
    [
        (
            [[([MURPHY], "Song 1"), ([MURPHY, SIMPSON], "Song 2")]],
            "[b]01.[/b] Song 1\n[b]02.[/b] Sullivan Murphy (feat. Dierdre Simpson) - Song 2\n",
        ),
        (
            [[([MURPHY], "Song 1")], [([MURPHY, SIMPSON], "Song 2")]],
            "[b]01-01.[/b] Song 1\n[b]02-01.[/b] Sullivan Murphy (feat. Dierdre Simpson) - Song 2\n",
        ),
    ],
    ids=["one disc", "two discs"],
)
def test_as_before_with_the_setting_off_the_guest_follows_the_artist(monkeypatch, discs, expected: str) -> None:
    description = _description(monkeypatch, _release(*discs), guests_in_track_title=False)

    assert description == f"{HEADING}{expected}"


def test_as_before_with_the_setting_on_a_track_with_no_guest_is_unchanged(monkeypatch) -> None:
    other = ("Other Artist", "main")
    release = _release([([MURPHY], "Song 1"), ([MURPHY], "Song 2"), ([other], "Song 3")])

    description = _description(monkeypatch, release, guests_in_track_title=True)

    assert description.removeprefix(HEADING).splitlines() == [
        "[b]01.[/b] Sullivan Murphy - Song 1",
        "[b]02.[/b] Sullivan Murphy - Song 2",
        "[b]03.[/b] Other Artist - Song 3",
    ]


def test_as_before_with_the_setting_on_a_track_by_the_album_alone_names_no_artist(monkeypatch) -> None:
    release = _release([([MURPHY], "Song 1"), ([MURPHY], "Song 2")])

    description = _description(monkeypatch, release, guests_in_track_title=True)

    assert description == f"{HEADING}[b]01.[/b] Song 1\n[b]02.[/b] Song 2\n"


@pytest.mark.parametrize(
    "title", ["Song 2 (feat. Dierdre Simpson)", "Song 2 (Feat. Dierdre Simpson)", "Song 2 [ft. Dierdre Simpson]"]
)
def test_as_before_a_title_already_crediting_the_guest_gains_no_second_credit(monkeypatch, title: str) -> None:
    # Qobuz keeps the credit in its titles and reads the guest from it too.
    release = _release([([MURPHY], "Song 1"), ([MURPHY, SIMPSON], title)])

    description = _description(monkeypatch, release, guests_in_track_title=True)

    assert description == f"{HEADING}[b]01.[/b] Song 1\n[b]02.[/b] Sullivan Murphy - {title}\n"


def test_guest_named_in_a_with_bracket_gains_no_second_credit(monkeypatch) -> None:
    # Was "Sullivan Murphy - Song 1 (with Dierdre Simpson) (feat. Dierdre Simpson)"
    release = _release([([MURPHY, SIMPSON], "Song 1 (with Dierdre Simpson)")])

    description = _description(monkeypatch, release, guests_in_track_title=True)

    assert description == f"{HEADING}[b]01.[/b] Sullivan Murphy - Song 1 (with Dierdre Simpson)\n"


def test_as_before_a_with_bracket_naming_someone_else_gains_the_guest(monkeypatch) -> None:
    release = _release([([MURPHY, SIMPSON], "Song 1 (with Strings)")])

    description = _description(monkeypatch, release, guests_in_track_title=True)

    assert description == f"{HEADING}[b]01.[/b] Sullivan Murphy - Song 1 (with Strings) (feat. Dierdre Simpson)\n"
