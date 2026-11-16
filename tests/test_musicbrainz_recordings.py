"""Every MusicBrainz answer is recorded in both formats, fetched together (#1065)."""

from pathlib import Path

import pytest

RECORDINGS = Path(__file__).parent / "fixtures" / "stores" / "musicbrainz"


@pytest.mark.parametrize("kind", ["release", "search"])
def test_every_answer_has_xml_and_json(kind: str) -> None:
    folder = RECORDINGS / kind
    xml = {p.stem for p in folder.glob("*.xml")}
    assert xml
    assert xml == {p.stem for p in folder.glob("*.json")}
