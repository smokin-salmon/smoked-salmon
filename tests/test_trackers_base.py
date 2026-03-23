import asyncio

import aiohttp
from yarl import URL

from salmon.trackers.base import (
    _build_tracker_cookies,
    _normalize_session_cookie,
)


def test_normalize_session_cookie_encodes_decoded_red_style_values() -> None:
    decoded = (
        "NYzc/MwZxhjA8VKzFuyi0HcVN/TiaCiwgOAx+4rKhpWQsNLtfImjNY1xJzForZ3UUP+uo66wrrv7J6V5OrXnsb+Q"
        "kvCP6H6bXMJ2jqElXZJf+xMfryUSE4pocW8IaeRk:Jcc5R/l9nvCJpY8hI7uKpA=="
    )

    assert _normalize_session_cookie(decoded) == (
        "NYzc%2FMwZxhjA8VKzFuyi0HcVN%2FTiaCiwgOAx%2B4rKhpWQsNLtfImjNY1xJzForZ3UUP%2Buo66wrrv7J6V5"
        "OrXnsb%2BQkvCP6H6bXMJ2jqElXZJf%2BxMfryUSE4pocW8IaeRk%3AJcc5R%2Fl9nvCJpY8hI7uKpA%3D%3D"
    )


def test_normalize_session_cookie_is_idempotent_for_encoded_values() -> None:
    encoded = (
        "NYzc%2FMwZxhjA8VKzFuyi0HcVN%2FTiaCiwgOAx%2B4rKhpWQsNLtfImjNY1xJzForZ3UUP%2Buo66wrrv7J6V5"
        "OrXnsb%2BQkvCP6H6bXMJ2jqElXZJf%2BxMfryUSE4pocW8IaeRk%3AJcc5R%2Fl9nvCJpY8hI7uKpA%3D%3D"
    )

    assert _normalize_session_cookie(encoded) == encoded


def test_normalized_cookie_header_is_not_quoted() -> None:
    loop = asyncio.new_event_loop()
    try:
        jar = aiohttp.CookieJar(loop=loop)
        url = URL("https://redacted.sh/ajax.php?action=index")
        jar.update_cookies(
            {"session": _normalize_session_cookie("abc/def+ghi:jkl==")},
            response_url=url,
        )

        assert jar.filter_cookies(url).output(header="Cookie:") == "Cookie: session=abc%2Fdef%2Bghi%3Ajkl%3D%3D"
    finally:
        loop.close()


def test_build_tracker_cookies_includes_optional_keeplogged() -> None:
    assert _build_tracker_cookies("abc/def", "keep-me") == {
        "session": "abc%2Fdef",
        "keeplogged": "keep-me",
    }
