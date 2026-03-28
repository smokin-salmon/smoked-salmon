import textwrap

from salmon.sources.bandcamp import (
    is_checkout_url,
    normalize_release_url,
    resolve_release_url_from_checkout_html,
)


def test_bandcamp_checkout_url_is_detected() -> None:
    assert is_checkout_url("https://bandcamp.com/download?cart_id=123&sig=abc")
    assert not is_checkout_url("https://artist.bandcamp.com/album/example")


def test_bandcamp_release_url_is_normalized() -> None:
    assert (
        normalize_release_url("https://artist.bandcamp.com/album/example/?foo=bar#frag")
        == "https://artist.bandcamp.com/album/example"
    )


def test_bandcamp_checkout_html_resolves_release_url() -> None:
    page_html = textwrap.dedent(
        """
        <div
          data-blob="{&quot;download_items&quot;:[{&quot;page_url&quot;:&quot;https://nativetelevision.bandcamp.com/album/ntva03-after-hours&quot;,&quot;downloads&quot;:{&quot;flac&quot;:{&quot;url&quot;:&quot;https://downloads.example/flac.zip&quot;}}}]}"
        ></div>
        """
    )

    assert (
        resolve_release_url_from_checkout_html(page_html)
        == "https://nativetelevision.bandcamp.com/album/ntva03-after-hours"
    )
