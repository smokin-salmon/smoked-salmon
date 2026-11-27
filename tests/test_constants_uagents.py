"""No User-Agent salmon sends is one Deezer refuses with its "Unsupported Browser" page (#1184)."""

import pytest

from salmon.constants import UAGENTS
from salmon.images import catbox
from salmon.sources import base, deezer


def test_uagents_has_no_internet_explorer() -> None:
    # Deezer answers MSIE 7.0 with an Unsupported Browser page that has no __DZR_APP_STATE__, so a search
    # that happened to pick it failed; the other agents work.
    assert [agent for agent in UAGENTS if "MSIE" in agent] == []


@pytest.mark.parametrize("headers", [deezer.HEADERS, base.HEADERS, catbox.HEADERS], ids=["deezer", "base", "catbox"])
def test_a_scraper_sends_one_of_the_uagents(headers) -> None:
    assert headers["User-Agent"] in UAGENTS
