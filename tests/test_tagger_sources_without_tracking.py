"""without_tracking keeps a link whose store needs its query, and a link it cannot split."""

import re
from types import SimpleNamespace

import pytest

from salmon.tagger import sources
from salmon.tagger.sources import without_tracking


def test_a_link_its_store_reads_only_with_its_query_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    scraper = SimpleNamespace(regex=re.compile(r"^https://store\.test/album\?id=\d+$"))
    monkeypatch.setattr(sources, "METASOURCES", {"Store": SimpleNamespace(Scraper=scraper)})

    assert without_tracking("https://store.test/album?id=5") == "https://store.test/album?id=5"


def test_a_link_whose_host_cannot_be_split_is_kept_as_given() -> None:
    # Bandcamp's pattern takes any host, so this is a store link, but urlsplit refuses the unclosed "[".
    url = "https://[bad/album/some-album?from=x"

    assert without_tracking(url) == url
