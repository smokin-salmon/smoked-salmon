import textwrap

from bs4 import BeautifulSoup

from salmon.tagger.sources.bandcamp import Scraper


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(textwrap.dedent(html), "lxml")


def test_label_hosted_bandcamp_title_prefix_sets_catno_and_strips_display_prefix() -> None:
    scraper = Scraper()
    soup = make_soup("""
        <div id="name-section">
          <span>Marius Acke</span>
          <div class="trackTitle">TOW014 / Marius Acke - Dirty &amp; Funky EP</div>
        </div>
        <div id="band-name-location">
          <span class="title">Theory Of Swing Records</span>
        </div>
    """)

    assert scraper.parse_release_title(soup) == "Dirty & Funky EP"
    assert scraper.parse_release_catno(soup) == "TOW014"


def test_artist_page_bandcamp_can_recover_catno_from_footer_label_release() -> None:
    scraper = Scraper()
    soup = make_soup("""
        <div id="name-section">
          <span>Marius Acke</span>
          <div class="trackTitle">Dirty &amp; Funky EP</div>
        </div>
        <ul>
          <li
            class="recommended-album footer-ar"
            data-albumtitle="TOW014 / Marius Acke - Dirty &amp; Funky EP"
            data-artist="Marius Acke"
          ></li>
        </ul>
    """)

    assert scraper.parse_release_title(soup) == "Dirty & Funky EP"
    assert scraper.parse_release_catno(soup) == "TOW014"


def test_bandcamp_slash_titles_without_catno_are_left_unchanged() -> None:
    scraper = Scraper()
    soup = make_soup("""
        <div id="name-section">
          <span>Example Artist</span>
          <div class="trackTitle">Love / Hate EP</div>
        </div>
    """)

    assert scraper.parse_release_title(soup) == "Love / Hate EP"
    assert scraper.parse_release_catno(soup) is None
