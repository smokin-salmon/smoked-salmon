"""Every MusicBrainz answer is recorded in both formats, fetched together (#1065)."""

import json
from pathlib import Path
from typing import Any

import pytest
from musicbrainzngs import mbxml

RECORDINGS = Path(__file__).parent / "fixtures" / "stores" / "musicbrainz"


@pytest.mark.parametrize("kind", ["release", "search"])
def test_every_answer_has_xml_and_json(kind: str) -> None:
    folder = RECORDINGS / kind
    xml = {p.stem for p in folder.glob("*.xml")}
    assert xml
    assert xml == {p.stem for p in folder.glob("*.json")}


def _pair(kind: str, stem: str) -> tuple[dict[str, Any], dict[str, Any]]:
    folder = RECORDINGS / kind
    return (
        mbxml.parse_message((folder / f"{stem}.xml").read_bytes()),
        json.loads((folder / f"{stem}.json").read_text(encoding="utf-8")),
    )


@pytest.mark.parametrize("stem", sorted(p.stem for p in (RECORDINGS / "search").glob("*.xml")))
def test_a_search_answers_the_same_releases_in_both_formats(stem: str) -> None:
    xml, js = _pair("search", stem)
    assert [r["id"] for r in xml["release-list"]] == [r["id"] for r in js["releases"]]


@pytest.mark.parametrize("stem", sorted(p.stem for p in (RECORDINGS / "release").glob("*.xml")))
def test_a_release_has_the_same_tracks_in_both_formats(stem: str) -> None:
    xml, js = _pair("release", stem)
    release = xml["release"]
    assert release["id"] == js["id"] == stem
    assert [[t["id"] for t in m["track-list"]] for m in release["medium-list"]] == [
        [t["id"] for t in m["tracks"]] for m in js["media"]
    ]
