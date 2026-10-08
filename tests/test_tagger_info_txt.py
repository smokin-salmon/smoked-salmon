import asyncio
from copy import deepcopy
from pathlib import Path

from salmon.tagger import metadata as metadata_mod
from salmon.tagger.combine import combine_metadatas
from salmon.tagger.info_txt import find_info_txt, parse_info_txt

SHINE_INFO_TXT = (
    "Title: SHINE\n"
    "Artist: Jess Gillam\n"
    "Label: Decca Music Group Ltd.\n"
    "Year: 2026\n"
    "Genre: Classical\n"
    "Type: Album\n"
    "\n"
    "01. Jess Gillam (main), Rune Tonsgaard S\u00f8rensen (composer) - Shine You No More\n"
    "02. Jess Gillam (main), Kate Bush (composer) - Jig of Life\n"
    "03. Jess Gillam (main), Barbara Strozzi (composer) - Che si pu\u00f2 fare\n"
    "04. Jess Gillam (main), Ruaidhr\u00ed Dall \u00d3 Cath\u00e1in (composer) - Tabhair dom do l\u00e1mh\n"
    "05. Jess Gillam (main), Carl Philipp Emanuel Bach (composer)"
    " - Flute Concerto in A Minor, H. 431: I. Allegro assai\n"
    "06. Jess Gillam (main), Sidney Bechet (composer) - Petite Fleur\n"
    "07. Jess Gillam (main), Barbara Thompson (composer) - A Tribute to Sidney Bechet\n"
    "08. Jess Gillam (main), Joni Mitchell (composer) - A Case of You\n"
    "09. Jess Gillam (main), Arcangelo Corelli (composer) - La Folia\n"
    "10. Jess Gillam (main), David Bowie (composer) - Life on Mars?\n"
    "11. Jess Gillam (main), Jess Gillam (composer), Sam Becker (composer),"
    " Leif Kaner-Lidstr\u00f6m (composer) - Prelude to Hildegard\n"
    "12. Jess Gillam (main), Hildegard of Bingen (composer) - Spiritus sanctus vivificans\n"
)


def _write(tmp_path: Path, content: str, name: str = "info.txt") -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_find_info_txt_requires_exact_lowercase_name(tmp_path: Path) -> None:
    assert find_info_txt(str(tmp_path)) is None
    _write(tmp_path, "Title: X\n", name="Info.txt")
    assert find_info_txt(str(tmp_path)) is None
    expected = _write(tmp_path, "Title: X\n")
    assert find_info_txt(str(tmp_path)) == expected


def test_header_styles_aliases_and_year_extraction(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "Release Title\n"
        "  Example Album\n"
        "ARTIST: Example Artist\n"
        "Record Label: Example Records\n"
        "Catalog number: EX-001\n"
        "Release Year: Recorded 2006\n"
        "Genre: Rock, Electronic\n"
        "Genre: Pop / Classical\n"
        "Barcode: n/a\n"
        "Version: Deluxe\n"
        "\n"
        "01. Example Artist - Opening Road\n",
    )
    metadata = parse_info_txt(path)
    assert metadata["title"] == "Example Album"
    assert metadata["label"] == "Example Records"
    assert metadata["catno"] == "EX-001"
    assert metadata["year"] == 2006
    assert metadata["group_year"] == 2006
    assert sorted(metadata["genres"]) == ["Classical", "Electronic", "Pop", "Rock"]
    assert metadata["upc"] is None
    assert metadata["edition_title"] == "Deluxe"
    assert metadata["rls_type"] == "Album"
    assert metadata["tracks"]["1"]["1"]["title"] == "Opening Road"


def test_year_variants_and_rls_type_and_empty_catno(tmp_path: Path) -> None:
    for value, expected in [("2006", 2006), ("2006-05-01", 2006), ("Recorded 2006", 2006)]:
        path = _write(tmp_path, f"Title: T\nYear: {value}\nType: EP\nCATNO: N/A\nUPC: EAN-NOT-SET\n")
        metadata = parse_info_txt(path)
        assert metadata["year"] == expected
        assert metadata["rls_type"] == "EP"
        assert metadata["catno"] is None
        assert metadata["upc"] == "EAN-NOT-SET"


def test_empty_key_with_indented_value_and_comments(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "# a comment\n// another comment\nTitle:\n  Indented Album\n\n01. Example Artist - Opening Road\n",
    )
    assert parse_info_txt(path)["title"] == "Indented Album"


def test_numbering_forms_and_auto_numbering(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "Title: T\n01. A - One\n02) A - Two\n03: A - Three\n04 - A - Four\nA - Five\n",
    )
    tracks = parse_info_txt(path)["tracks"]["1"]
    assert list(tracks) == ["1", "2", "3", "4", "5"]
    assert tracks["5"]["title"] == "Five"


def test_duplicate_numbers_fall_back_to_auto_counter(tmp_path: Path) -> None:
    path = _write(tmp_path, "Title: T\n01. A - One\n01. A - Two\n")
    tracks = parse_info_txt(path)["tracks"]["1"]
    assert list(tracks) == ["1", "2"]
    assert tracks["2"]["title"] == "Two"


def test_only_first_separator_splits_and_dash_variants(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "Title: T\n01. A - Title - Remix\n02. B--Double Dash\n03. C \u2014 Em Dash\n04. D \u2013 En Dash\n",
    )
    tracks = parse_info_txt(path)["tracks"]["1"]
    assert tracks["1"]["title"] == "Title - Remix"
    assert tracks["2"]["artists"] == [("B", "main")]
    assert tracks["2"]["title"] == "Double Dash"
    assert tracks["3"]["title"] == "Em Dash"
    assert tracks["4"]["title"] == "En Dash"


def test_bare_dash_and_empty_sides_are_not_tracks(tmp_path: Path) -> None:
    path = _write(tmp_path, "Title: T\nSelf-titled\nArtist-Title\n - Title\nArtist - \n")
    assert parse_info_txt(path)["tracks"] == {}


def test_classical_colon_title_and_unknown_colon_key(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "Title: SHINE\n"
        "05. Jess Gillam (main), Carl Philipp Emanuel Bach (composer)"
        " - Flute Concerto in A Minor, H. 431: I. Allegro assai\n"
        "K. 370: III. Allegro\n",
    )
    metadata = parse_info_txt(path)
    assert metadata["title"] == "SHINE"
    tracks = metadata["tracks"]["1"]
    assert list(tracks) == ["5"]
    assert tracks["5"]["title"] == "Flute Concerto in A Minor, H. 431: I. Allegro assai"


def test_roles_default_main_and_multi_artist_splits(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "Title: T\n"
        "01. Kyung-Wha Chung (main), Vienna Philharmonic (main), Simon Rattle (CONDUCTOR) - Concerto No. 1\n"
        "02. Plain Artist - Plain Title\n"
        "03. A & B - Amp Title\n"
        "04. C, D - Comma Title\n"
        "05. E / F - Slash Title\n"
        "06. G; H - Semi Title\n"
        "07. I \\ J - Backslash Title\n",
    )
    tracks = parse_info_txt(path)["tracks"]["1"]
    assert tracks["1"]["artists"] == [
        ("Kyung-Wha Chung", "main"),
        ("Vienna Philharmonic", "main"),
        ("Simon Rattle", "conductor"),
    ]
    assert tracks["2"]["artists"] == [("Plain Artist", "main")]
    assert tracks["3"]["artists"] == [("A", "main"), ("B", "main")]
    assert tracks["4"]["artists"] == [("C", "main"), ("D", "main")]
    assert tracks["5"]["artists"] == [("E", "main"), ("F", "main")]
    assert tracks["6"]["artists"] == [("G", "main"), ("H", "main")]
    assert tracks["7"]["artists"] == [("I", "main"), ("J", "main")]


def test_disc_headers_totals_and_pre_header_disc_one(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "Title: T\n01. A - Before Header\nDisc 1\n01. A - D1 One\nCD2\n01. A - D2 One\n02. A - D2 Two\n",
    )
    tracks = parse_info_txt(path)["tracks"]
    assert list(tracks) == ["1", "2"]
    assert list(tracks["1"]) == ["1", "2"]
    assert tracks["1"]["1"]["title"] == "Before Header"
    assert tracks["1"]["2"]["title"] == "D1 One"
    assert tracks["2"]["1"]["tracktotal"] == 2
    assert tracks["2"]["1"]["disctotal"] == 2


def test_global_composer_conductor_fallback(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "Title: T\n"
        "Composer: Ludwig van Beethoven\n"
        "Conductor: Simon Rattle\n"
        "01. Vienna Philharmonic, Simon Rattle (conductor) - Symphony No. 5 - I. Allegro\n"
        "02. Vienna Philharmonic - Symphony No. 5 - II. Andante\n"
        "03. Vienna Philharmonic, Someone Else (composer) - Other Piece\n",
    )
    tracks = parse_info_txt(path)["tracks"]["1"]
    assert ("Ludwig van Beethoven", "composer") in tracks["1"]["artists"]
    assert tracks["1"]["artists"].count(("Simon Rattle", "conductor")) == 1
    assert ("Ludwig van Beethoven", "composer") in tracks["2"]["artists"]
    assert ("Simon Rattle", "conductor") in tracks["2"]["artists"]
    assert ("Ludwig van Beethoven", "composer") not in tracks["3"]["artists"]
    assert ("Someone Else", "composer") in tracks["3"]["artists"]


def test_various_artists_stripped_and_regenerated(tmp_path: Path) -> None:
    path = _write(tmp_path, "Title: T\nArtist: Someone\n01. Various - Foo\n02. Real Artist - Bar\n")
    metadata = parse_info_txt(path)
    assert metadata["artists"] == [("Real Artist", "main")]
    assert metadata["tracks"]["1"]["1"]["artists"] == []


def test_album_artist_header_used_only_without_tracks(tmp_path: Path) -> None:
    path = _write(tmp_path, "Title: T\nArtist: Solo Artist\n")
    metadata = parse_info_txt(path)
    assert metadata["tracks"] == {}
    assert metadata["artists"] == [("Solo Artist", "main")]


def test_shine_album_fixture(tmp_path: Path) -> None:
    metadata = parse_info_txt(_write(tmp_path, SHINE_INFO_TXT))
    assert metadata["title"] == "SHINE"
    assert metadata["label"] == "Decca Music Group Ltd."
    assert metadata["year"] == 2026
    assert metadata["genres"] == ["Classical"]
    assert metadata["rls_type"] == "Album"
    assert metadata["catno"] is None
    assert "url" not in metadata

    expected = [
        ("Shine You No More", [("Jess Gillam", "main"), ("Rune Tonsgaard S\u00f8rensen", "composer")]),
        ("Jig of Life", [("Jess Gillam", "main"), ("Kate Bush", "composer")]),
        ("Che si pu\u00f2 fare", [("Jess Gillam", "main"), ("Barbara Strozzi", "composer")]),
        ("Tabhair dom do l\u00e1mh", [("Jess Gillam", "main"), ("Ruaidhr\u00ed Dall \u00d3 Cath\u00e1in", "composer")]),
        (
            "Flute Concerto in A Minor, H. 431: I. Allegro assai",
            [("Jess Gillam", "main"), ("Carl Philipp Emanuel Bach", "composer")],
        ),
        ("Petite Fleur", [("Jess Gillam", "main"), ("Sidney Bechet", "composer")]),
        ("A Tribute to Sidney Bechet", [("Jess Gillam", "main"), ("Barbara Thompson", "composer")]),
        ("A Case of You", [("Jess Gillam", "main"), ("Joni Mitchell", "composer")]),
        ("La Folia", [("Jess Gillam", "main"), ("Arcangelo Corelli", "composer")]),
        ("Life on Mars?", [("Jess Gillam", "main"), ("David Bowie", "composer")]),
        (
            "Prelude to Hildegard",
            [
                ("Jess Gillam", "main"),
                ("Jess Gillam", "composer"),
                ("Sam Becker", "composer"),
                ("Leif Kaner-Lidstr\u00f6m", "composer"),
            ],
        ),
        ("Spiritus sanctus vivificans", [("Jess Gillam", "main"), ("Hildegard of Bingen", "composer")]),
    ]
    tracks = metadata["tracks"]["1"]
    assert list(tracks) == [str(n) for n in range(1, 13)]
    for num, (title, artists) in enumerate(expected, start=1):
        assert tracks[str(num)]["title"] == title
        assert tracks[str(num)]["artists"] == artists
        assert tracks[str(num)]["tracktotal"] == 12
        assert tracks[str(num)]["disctotal"] == 1


def _make_base_track(title: str, artists: list[tuple[str, str]]) -> dict:
    return {
        "track#": "1",
        "disc#": "1",
        "tracktotal": "12",
        "disctotal": "1",
        "artists": artists,
        "title": title,
        "replay_gain": 1.5,
        "peak": 0.9,
        "isrc": None,
        "explicit": None,
        "format": None,
        "streamable": None,
    }


def _make_base(decorated_titles: list[str]) -> dict:
    return {
        "artists": [("Jess Gillam", "main")],
        "title": "SHINE",
        "group_year": 2026,
        "year": 2026,
        "date": None,
        "edition_title": None,
        "label": None,
        "catno": None,
        "rls_type": None,
        "genres": ["Classical"],
        "format": "FLAC",
        "encoding": "24bit Lossless",
        "encoding_vbr": False,
        "scene": False,
        "source": "WEB",
        "cover": None,
        "upc": None,
        "comment": None,
        "urls": [],
        "tracks": {
            "1": {
                str(n): _make_base_track(title, [("Jess Gillam", "main")]) | {"track#": str(n)}
                for n, title in enumerate(decorated_titles, start=1)
            }
        },
    }


def _make_discogs_track(title: str, artists: list[tuple[str, str]]) -> dict:
    return _make_base_track(title, artists)


def test_combine_prefers_info_txt_over_decorated_base_and_discogs(tmp_path: Path) -> None:
    info_metadata = parse_info_txt(_write(tmp_path, SHINE_INFO_TXT))
    decorated = [
        "Shine You No More (Arr. Parkin)",
        "Jig of Life (Arr. Vennart)",
        "Che si pu\u00f2 fare (Arr. Vennart)",
        "Tabhair dom do l\u00e1mh (Arr. Becker)",
        "Flute Concerto in A Minor, H. 431: I. Allegro assai (Arr. Parkin)",
        "Petite Fleur (Arr. Becker)",
        "A Tribute to Sidney Bechet",
        "A Case of You (Arr. Newell)",
        "La Folia (Arr. Parkin and Poxon)",
        "Life on Mars? (Arr. Vennart)",
        "Prelude to Hildegard",
        "Spiritus sanctus vivificans (Arr. Lawson)",
    ]
    base = _make_base(decorated)
    discogs = {
        "artists": [("Jess Gillam", "main")],
        "title": "Shine",
        "group_year": 2026,
        "year": 2026,
        "date": None,
        "edition_title": None,
        "label": "Decca",
        "catno": None,
        "rls_type": "Album",
        "genres": ["Classical"],
        "cover": None,
        "upc": None,
        "comment": None,
        "url": "https://www.discogs.com/release/12345678",
        "urls": ["https://www.discogs.com/release/12345678"],
        "tracks": {
            "1": {
                "1": _make_discogs_track("Shine You No More", [("Jess Gillam", "main"), ("Guest Singer", "guest")]),
                "2": _make_discogs_track("Completely Different Name", [("Jess Gillam", "main")]),
            }
        },
    }

    combined = combine_metadatas(("InfoTxt", info_metadata), ("Discogs", discogs), base=deepcopy(base))
    tracks = combined["tracks"]["1"]
    # Clean InfoTxt titles win over decorated tag titles.
    assert tracks["1"]["title"] == "Shine You No More"
    assert tracks["2"]["title"] == "Jig of Life"
    assert tracks["5"]["title"] == "Flute Concerto in A Minor, H. 431: I. Allegro assai"
    # Composers survive per track; Jess Gillam keeps both roles on track 11.
    assert ("Rune Tonsgaard S\u00f8rensen", "composer") in tracks["1"]["artists"]
    assert ("Jess Gillam", "main") in tracks["1"]["artists"]
    assert ("Kate Bush", "composer") in tracks["2"]["artists"]
    assert ("Jess Gillam", "main") in tracks["11"]["artists"]
    assert ("Jess Gillam", "composer") in tracks["11"]["artists"]
    assert ("Sam Becker", "composer") in tracks["11"]["artists"]
    # Discogs extras still merge, but InfoTxt preference order holds.
    assert ("Guest Singer", "guest") in tracks["1"]["artists"]
    assert "https://www.discogs.com/release/12345678" in combined["urls"]


def _make_release_data() -> dict:
    return {
        "artists": [("Example Artist", "main")],
        "title": "Example Release",
        "group_year": "2026",
        "year": "2026",
        "date": None,
        "edition_title": None,
        "label": None,
        "catno": None,
        "rls_type": None,
        "genres": ["Electronic"],
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "scene": False,
        "source": "WEB",
        "cover": None,
        "upc": None,
        "comment": None,
        "urls": [],
        "tracks": {
            "1": {
                "1": {
                    "track#": "1",
                    "disc#": "1",
                    "tracktotal": "1",
                    "disctotal": "1",
                    "artists": [("Example Artist", "main")],
                    "title": "Track One",
                    "replay_gain": None,
                    "peak": None,
                    "isrc": None,
                    "explicit": None,
                    "format": None,
                    "streamable": None,
                }
            }
        },
    }


class _ExplodingScraper:
    regex = type("Regex", (), {"match": staticmethod(lambda value: "qobuz" in value)})

    async def scrape_release(self, url: str):
        raise AssertionError("non-Discogs URL must not be scraped in info.txt mode")


class _ExplodingSource:
    Scraper = _ExplodingScraper


class _DiscogsScraper:
    regex = type("Regex", (), {"match": staticmethod(lambda value: "discogs" in value)})

    async def scrape_release(self, url: str):
        data = _make_release_data()
        data["title"] = "Discogs Title"
        data["url"] = url
        data["urls"] = [url]
        return data


class _DiscogsSource:
    Scraper = _DiscogsScraper


def test_select_choice_ignores_non_discogs_urls_in_info_mode(monkeypatch) -> None:
    async def prompt_url(*args, **kwargs):
        return "https://open.qobuz.com/album/0887396827479"

    monkeypatch.setattr(metadata_mod.click, "prompt", prompt_url)
    monkeypatch.setattr(metadata_mod.click, "edit", lambda *args, **kwargs: None)
    monkeypatch.setattr(metadata_mod, "METASOURCES", {"Discogs": _DiscogsSource, "Qobuz": _ExplodingSource})
    info_metadata = {
        **_make_release_data(),
        "title": "InfoTxt Title",
        "urls": [],
    }

    metadata, source_url = asyncio.run(metadata_mod._select_choice({}, _make_release_data(), info_metadata))

    assert metadata["title"] == "InfoTxt Title"
    assert metadata["urls"] == ["https://open.qobuz.com/album/0887396827479"]
    assert source_url is None


def test_select_choice_manual_edits_info_metadata(monkeypatch) -> None:
    async def prompt_manual(*args, **kwargs):
        return "m"

    monkeypatch.setattr(metadata_mod.click, "prompt", prompt_manual)
    monkeypatch.setattr(metadata_mod.click, "edit", lambda *args, **kwargs: None)
    info_metadata = {**_make_release_data(), "title": "InfoTxt Title"}

    metadata, source_url = asyncio.run(metadata_mod._select_choice({}, _make_release_data(), info_metadata))

    assert metadata["title"] == "InfoTxt Title"
    assert source_url is None


def test_select_choice_scrapes_discogs_urls_in_info_mode(monkeypatch) -> None:
    async def prompt_url(*args, **kwargs):
        return "https://www.discogs.com/release/12345678"

    monkeypatch.setattr(metadata_mod.click, "prompt", prompt_url)
    monkeypatch.setattr(metadata_mod, "METASOURCES", {"Discogs": _DiscogsSource, "Qobuz": _ExplodingSource})
    info_metadata = {**_make_release_data(), "title": "InfoTxt Title"}

    metadata, source_url = asyncio.run(metadata_mod._select_choice({}, _make_release_data(), info_metadata))

    assert metadata["title"] == "InfoTxt Title"
    assert "https://www.discogs.com/release/12345678" in metadata["urls"]


def test_maybe_load_info_txt_deletes_after_parse_and_keeps_on_decline(tmp_path: Path, monkeypatch) -> None:
    info_path = _write(tmp_path, "Title: T\n01. A - One\n")

    async def confirm_yes(*args, **kwargs):
        return True

    monkeypatch.setattr(metadata_mod.click, "confirm", confirm_yes)
    rls_data = _make_release_data()
    metadata = asyncio.run(metadata_mod._maybe_load_info_txt(str(tmp_path), rls_data))
    assert metadata is not None
    assert metadata["title"] == "T"
    assert metadata["source"] == "WEB"
    assert not Path(info_path).exists()

    info_path = _write(tmp_path, "Title: T2\n")

    async def confirm_no(*args, **kwargs):
        return False

    monkeypatch.setattr(metadata_mod.click, "confirm", confirm_no)
    assert asyncio.run(metadata_mod._maybe_load_info_txt(str(tmp_path), _make_release_data())) is None
    assert Path(info_path).exists()
