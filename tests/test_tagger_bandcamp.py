import asyncio
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


def test_bracketed_catno_prefix_sets_catno_and_cleans_release_title() -> None:
    scraper = Scraper()
    soup = make_soup("""
        <div id="name-section">
          <span>Various Artists</span>
          <div class="trackTitle">[NTVA03] AFTER HOURS</div>
        </div>
        <div id="band-name-location">
          <span class="title">Native Television</span>
        </div>
    """)

    assert scraper.parse_release_title(soup) == "AFTER HOURS"
    assert scraper.parse_release_catno(soup) == "NTVA03"


def test_parenthesized_catno_prefix_sets_catno_and_cleans_release_title() -> None:
    scraper = Scraper()
    soup = make_soup("""
        <div id="name-section">
          <span>The Magic Movement</span>
          <div class="trackTitle">(ST03) Balearic Shaketown</div>
        </div>
        <div id="band-name-location">
          <span class="title">The Magic Movement</span>
        </div>
    """)

    assert scraper.parse_release_title(soup) == "Balearic Shaketown"
    assert scraper.parse_release_catno(soup) == "ST03"


def test_label_hosted_bandcamp_release_title_can_recover_release_artist_and_label() -> None:
    scraper = Scraper()
    soup = make_soup("""
        <div id="name-section">
          <h2 class="trackTitle">Success - Tripwire</h2>
          <h3>by <span><a href="https://ozonerecordings1.bandcamp.com">Ozone Recordings</a></span></h3>
        </div>
        <p id="band-name-location">
          <span class="title">Ozone Recordings</span>
        </p>
        <table id="track_table">
          <tr class="track_row_view linked" rel="tracknum=1">
            <td class="track-number-col"><div class="track_number">1.</div></td>
            <td class="title-col"><span class="track-title">Tripwire (Deep Bass Strip Down Acid)</span></td>
          </tr>
          <tr class="track_row_view linked" rel="tracknum=4">
            <td class="track-number-col"><div class="track_number">4.</div></td>
            <td class="title-col"><span class="track-title">Tripwire - (Deep Space Dub)</span></td>
          </tr>
        </table>
    """)

    tracks = asyncio.run(scraper.parse_tracks(soup))

    assert scraper.parse_release_title(soup) == "Tripwire"
    assert scraper.parse_release_label(soup) == "Ozone Recordings"
    assert tracks["1"]["1"]["artists"] == [("Success", "main")]
    assert tracks["1"]["1"]["title"] == "Tripwire (Deep Bass Strip Down Acid)"
    assert tracks["1"]["4"]["artists"] == [("Success", "main")]
    assert tracks["1"]["4"]["title"] == "Tripwire - (Deep Space Dub)"


def test_hyphenated_release_title_is_not_reinterpreted_without_tracklist_support() -> None:
    scraper = Scraper()
    soup = make_soup("""
        <div id="name-section">
          <h2 class="trackTitle">Club Cuts - Remixes</h2>
          <h3>by <span><a href="https://example.bandcamp.com">Example Label</a></span></h3>
        </div>
        <p id="band-name-location">
          <span class="title">Example Label</span>
        </p>
        <table id="track_table">
          <tr class="track_row_view linked" rel="tracknum=1">
            <td class="track-number-col"><div class="track_number">1.</div></td>
            <td class="title-col"><span class="track-title">Sunrise Mix</span></td>
          </tr>
          <tr class="track_row_view linked" rel="tracknum=2">
            <td class="track-number-col"><div class="track_number">2.</div></td>
            <td class="title-col"><span class="track-title">Moonlight Dub</span></td>
          </tr>
        </table>
    """)

    assert scraper.parse_release_title(soup) == "Club Cuts - Remixes"
    assert scraper.parse_release_label(soup) is None


def test_various_artist_track_side_prefixes_are_removed_from_track_artists() -> None:
    scraper = Scraper()
    soup = make_soup("""
        <div id="name-section">
          <span>Various Artists</span>
          <div class="trackTitle">[NTVA03] AFTER HOURS</div>
        </div>
        <div id="band-name-location">
          <span class="title">Native Television</span>
        </div>
        <table id="track_table">
          <tr class="track_row_view linked" rel="tracknum=1">
            <td class="track-number-col"><div class="track_number">1.</div></td>
            <td class="title-col"><span class="track-title">A1 TUFF TRAX - DEEPER LOVE</span></td>
          </tr>
          <tr class="track_row_view linked" rel="tracknum=8">
            <td class="track-number-col"><div class="track_number">8.</div></td>
            <td class="title-col"><span class="track-title">B4 TABZ, NICKOLAI - BACK 2 BUSINESS</span></td>
          </tr>
        </table>
    """)

    tracks = asyncio.run(scraper.parse_tracks(soup))

    assert tracks["1"]["1"]["artists"] == [("TUFF TRAX", "main")]
    assert tracks["1"]["1"]["title"] == "DEEPER LOVE"
    assert tracks["1"]["8"]["artists"] == [("TABZ", "main"), ("NICKOLAI", "main")]
    assert tracks["1"]["8"]["title"] == "BACK 2 BUSINESS"


def test_bandcamp_tracks_can_parse_unlinked_track_rows() -> None:
    scraper = Scraper()
    soup = make_soup("""
        <div id="name-section">
          <span>Ladytron</span>
          <div class="trackTitle">Paradies</div>
        </div>
        <table id="track_table">
          <tr class="track_row_view linked" rel="tracknum=1">
            <td class="track-number-col"><div class="track_number">1.</div></td>
            <td class="title-col">
              <div class="title">
                <a href="/track/i-believe-in-you-2"><span class="track-title">I Believe In You</span></a>
                <span class="time secondaryText">05:02</span>
              </div>
            </td>
          </tr>
          <tr class="track_row_view" rel="tracknum=2">
            <td class="track-number-col"><div class="track_number">2.</div></td>
            <td class="title-col">
              <div class="title">
                <span>In Blood</span>
              </div>
            </td>
          </tr>
        </table>
    """)

    tracks = asyncio.run(scraper.parse_tracks(soup))

    assert tracks["1"]["1"]["title"] == "I Believe In You"
    assert tracks["1"]["2"]["title"] == "In Blood"
