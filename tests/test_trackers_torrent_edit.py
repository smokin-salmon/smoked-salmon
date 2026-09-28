"""Adding text to a torrent's description must leave the rest of the torrent as it was (#357).

The edit page of each tracker is in tests/fixtures/torrent_edit: RED's and OPS's come from real pages,
DIC's is reconstructed. Each test's expected POST is what a browser submits for that page, written
out by hand, with only the description changed.
"""

import sys
from pathlib import Path

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter
from tenacity import wait_none

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.errors import LoginError, RequestError
from salmon.trackers.base import BaseGazelleApi

FIXTURES = Path(__file__).parent / "fixtures" / "torrent_edit"
ADDITION = "[hide=Spectrals][b]01 Full[/b]\n[img=https://img.example/1.png]\n[/hide]\n"

RED_DESC = (
    "[quote]Source [url=https://redacted.sh/torrents.php?id=1000&torrentid=2000#torrent2000]FLAC 24bit Lossless"
    "[/url][/quote][hide][pre]    comment[18]: album=Title One & Title Two\n[/pre][/hide]"
)
RED_FORM = {
    "submit": "true",
    "auth": "XXXX",
    "action": "takeedit",
    "torrentid": "2001",
    "type": "1",
    # No option is selected, so a browser sends the first one's.
    "groupremasters": "",
    "remaster_year": "2021",
    "remaster_title": "",
    "remaster_record_label": "Label",
    "remaster_catalogue_number": "CAT001",
    "format": "FLAC",
    "bitrate": "Lossless",
    "other_bitrate": "",
    "media": "WEB",
    "release_desc": RED_DESC,
}

OPS_DESC = (
    "[quote]Source [url=https://orpheus.network/torrents.php?id=1000&torrentid=2000#torrent2000]FLAC 24bit Lossless"
    "[/url][/quote][hide][pre]    comment[18]: album=Title One & Title Two\n[/pre][/hide]"
)
OPS_FORM = {
    "auth": "XXXX",
    "action": "takeedit",
    "torrentid": "2001",
    "groupremasters": "0",
    "remaster_year": "2021",
    "remaster_title": "",
    "remaster_record_label": "Label",
    "remaster_catalogue_number": "CAT001",
    "media": "WEB",
    "format": "FLAC",
    "bitrate": "Lossless",
    "other_bitrate": "",
    "release_desc": OPS_DESC,
    "workaround_broken_html_entities": "0",
}

DIC_DESC = "[size=4]Ripped by me & checked with EAC[/size]\nLabel's notes"
DIC_FORM = {
    "submit": "true",
    "auth": "XXXX",
    "action": "takeedit",
    "torrentid": "3001",
    "type": "1",
    "remaster": "on",
    "groupremasters": "",
    "remaster_year": "2000",
    "remaster_title": "Deluxe & Collector's Edition",
    "remaster_record_label": "Rock 'n' Roll Records",
    "remaster_catalogue_number": "XX-456",
    "buy": "on",
    "format": "FLAC",
    "bitrate": "Lossless",
    "other_bitrate": "",
    "sample_rate": "44100",
    "media": "CD",
    "release_desc": DIC_DESC,
}

LOGIN_PAGE = """<html><body><form name="loginform" id="loginform" method="post" action="login.php">
<input type="text" name="username" /><input type="password" name="password" />
<input type="submit" name="login" value="Log in" /></form></body></html>"""
ERROR_PAGE = """<html><body><div id="content"><div class="thin"><h2>Error</h2>
<div class="box pad"><p>You do not have permission to edit this torrent.</p></div></div></div></body></html>"""


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without waiting, so a test never sits through a backoff."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


class FakeTracker:
    """A local tracker serving one edit page, and the API answer for the same torrent."""

    def __init__(
        self,
        edit_page: str,
        torrent_json: str = '{"status": "failure"}',
        edit_status: int = 200,
        edit_answer: str | None = None,
    ) -> None:
        self.edit_page = edit_page
        self.torrent_json = torrent_json
        self.edit_status = edit_status
        # What the tracker answers the POST with instead of its redirect to the group, if set.
        self.edit_answer = edit_answer
        self.requests: list[tuple[str, str]] = []
        self.posts: list[dict[str, str]] = []
        self._runner: web.AppRunner | None = None

    async def _torrents(self, request: web.Request) -> web.StreamResponse:
        self.requests.append((request.method, request.path_qs))
        if request.method == "POST":
            form = await request.post()
            fields = [(name, str(value)) for name, value in form.items()]
            posted = dict(fields)
            assert len(posted) == len(fields), f"a field was sent twice: {fields}"
            self.posts.append(posted)
            if self.edit_answer is not None:
                return web.Response(text=self.edit_answer, content_type="text/html")
            # As Gazelle does once the edit is saved.
            raise web.HTTPFound("/torrents.php?id=1000")
        if request.query.get("action") == "edit":
            if self.edit_status == 302:
                raise web.HTTPFound("/login.php")
            return web.Response(text=self.edit_page, content_type="text/html")
        return web.Response(text="<html><body><h2>Group page</h2></body></html>", content_type="text/html")

    async def _ajax(self, request: web.Request) -> web.Response:
        self.requests.append((request.method, request.path_qs))
        if request.query.get("action") == "index":
            return web.json_response({"status": "success", "response": {"authkey": "XXXX", "passkey": "p"}})
        return web.Response(text=self.torrent_json, content_type="application/json")

    async def __aenter__(self) -> "FakeTracker":
        app = web.Application()
        app.router.add_route("*", "/torrents.php", self._torrents)
        app.router.add_route("*", "/ajax.php", self._ajax)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._runner is not None
        await self._runner.cleanup()

    @property
    def url(self) -> str:
        assert self._runner is not None
        return f"http://127.0.0.1:{self._runner.addresses[0][1]}"


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    cookie = "fake-cookie"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.site_string = "Fake"
        super().__init__()
        # Per instance, as each test runs on its own event loop.
        self._rate_limiter = AsyncLimiter(100, 1)


async def _edit(tracker: FakeTracker, torrent_id: int) -> None:
    async with tracker:
        api = FakeApi(tracker.url)
        try:
            await api.append_to_torrent_description(torrent_id, ADDITION)
        finally:
            await api.close()


def _run_edit(tracker: FakeTracker, torrent_id: int = 2001) -> None:
    anyio.run(_edit, tracker, torrent_id)


def _with_checked(page: str, *names: str) -> str:
    """The page with the named checkboxes checked, as the tracker shows a torrent that has those flags."""
    for name in names:
        tag = f'name="{name}"'
        assert page.count(tag) == 1, name
        page = page.replace(tag, f'{tag} checked="checked"')
    return page


@pytest.mark.parametrize(
    ("tracker", "form"),
    [pytest.param("red", RED_FORM, id="red"), pytest.param("ops", OPS_FORM, id="ops")],
)
def test_edit_sends_back_the_real_form_with_only_the_description_changed(tracker: str, form: dict) -> None:
    fake = FakeTracker(_fixture(f"{tracker}_edit.html"), _fixture(f"{tracker}_torrent.json"))

    _run_edit(fake)

    assert fake.posts == [{**form, "release_desc": ADDITION + form["release_desc"]}]


@pytest.mark.parametrize(
    ("tracker", "form", "flags"),
    [
        pytest.param("red", RED_FORM, ("scene",), id="red"),
        pytest.param("ops", OPS_FORM, ("scene", "vanity_house", "missing_lineage"), id="ops"),
    ],
)
def test_edit_keeps_the_flags_set_on_the_torrent(tracker: str, form: dict, flags: tuple[str, ...]) -> None:
    page = _with_checked(_fixture(f"{tracker}_edit.html"), *flags)
    fake = FakeTracker(page, _fixture(f"{tracker}_torrent.json").replace('"scene":false', '"scene":true'))

    _run_edit(fake)

    assert fake.posts == [{**form, **dict.fromkeys(flags, "on"), "release_desc": ADDITION + form["release_desc"]}]


def test_edit_does_not_escape_the_description_again_on_red() -> None:
    # RED's API answers the description HTML-escaped; its edit form holds the text itself.
    fake = FakeTracker(_fixture("red_edit.html"), _fixture("red_torrent.json"))

    _run_edit(fake)

    sent = fake.posts[0]["release_desc"]
    assert "&amp;" not in sent
    assert sent == ADDITION + RED_DESC


def test_dic_edit_keeps_the_edition_and_the_marks() -> None:
    # The reported bug: the edition checkbox, never sent, made DIC turn the torrent into an "Unknown release".
    fake = FakeTracker(_fixture("dic_edit.html"), _fixture("dic_torrent.json"))

    _run_edit(fake, torrent_id=3001)

    assert fake.posts == [{**DIC_FORM, "release_desc": ADDITION + DIC_DESC}]


def test_edit_sends_edition_text_with_ampersands_and_apostrophes_unchanged() -> None:
    # The API answer is assumed escaped as RED's description is; the edit form decodes to the text itself.
    fake = FakeTracker(_fixture("dic_edit.html"), _fixture("dic_torrent.json"))

    _run_edit(fake, torrent_id=3001)

    posted = fake.posts[0]
    assert posted["remaster_title"] == "Deluxe & Collector's Edition"
    assert posted["remaster_record_label"] == "Rock 'n' Roll Records"


def _red_group_forms_only() -> str:
    page = _fixture("red_edit.html")
    start, end = page.index('<form class="create_form"'), page.index("</form>") + len("</form>")
    return page[:start] + page[end:]


def _red_edit_form_twice() -> str:
    page = _fixture("red_edit.html")
    start, end = page.index('<form class="create_form"'), page.index("</form>") + len("</form>")
    return page[:end] + page[start:end] + page[end:]


def _red_without_description() -> str:
    page = _fixture("red_edit.html")
    assert page.count('name="release_desc"') == 1
    return page.replace('name="release_desc"', 'name="other_desc"')


@pytest.mark.parametrize(
    ("page", "torrent_id"),
    [
        pytest.param(LOGIN_PAGE, 2001, id="login page"),
        pytest.param(ERROR_PAGE, 2001, id="error page"),
        pytest.param(_red_group_forms_only(), 2001, id="only the group-changing forms"),
        pytest.param(_red_edit_form_twice(), 2001, id="two edit forms"),
        pytest.param(_red_without_description(), 2001, id="no description field"),
        pytest.param(_fixture("red_edit.html"), 2002, id="another torrent's form"),
    ],
)
def test_unreadable_edit_page_sends_nothing(page: str, torrent_id: int) -> None:
    fake = FakeTracker(page, _fixture("red_torrent.json"))

    with pytest.raises(RequestError, match="edit form"):
        _run_edit(fake, torrent_id=torrent_id)

    assert fake.posts == []
    assert fake.requests == [("GET", f"/torrents.php?action=edit&id={torrent_id}")]


def test_edit_page_redirected_to_login_sends_nothing() -> None:
    fake = FakeTracker("", _fixture("red_torrent.json"), edit_status=302)

    with pytest.raises(LoginError):
        _run_edit(fake)

    assert fake.posts == []
    assert fake.requests == [("GET", "/torrents.php?action=edit&id=2001")]


def test_edit_costs_one_get_and_one_post(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    request = BaseGazelleApi._request

    async def counting_request(self, method, url, params=None, *args, **kwargs):
        calls.append((method, url.removeprefix(self.base_url), params))
        return await request(self, method, url, params, *args, **kwargs)

    monkeypatch.setattr(BaseGazelleApi, "_request", counting_request)
    # A fresh client: the edit needs no index call, as it takes its auth from the form.
    fake = FakeTracker(_fixture("red_edit.html"), _fixture("red_torrent.json"))

    _run_edit(fake)

    assert calls == [
        ("GET", "/torrents.php", {"action": "edit", "id": 2001}),
        ("POST", "/torrents.php", None),
    ]
    # The tracker redirects the POST to the torrent's group, a hop _request follows.
    assert fake.requests == [
        ("GET", "/torrents.php?action=edit&id=2001"),
        ("POST", "/torrents.php"),
        ("GET", "/torrents.php?id=1000"),
    ]


def test_edit_refused_by_the_tracker_raises() -> None:
    fake = FakeTracker(_fixture("red_edit.html"), _fixture("red_torrent.json"), edit_answer=ERROR_PAGE)

    with pytest.raises(RequestError, match="You do not have permission"):
        _run_edit(fake)

    assert len(fake.posts) == 1


BROWSER_RULES_FORM = """<form name="torrent" method="post">
<input type="hidden" name="hidden" value="a &amp;amp; b">
<input name="untyped" value="x">
<input type="text" name="empty">
<input type="checkbox" name="unchecked">
<input type="checkbox" name="checked" checked>
<input type="checkbox" name="valued" value="1" checked>
<input type="radio" name="choice" value="one">
<input type="radio" name="choice" value="two" checked>
<input type="text" name="disabled" value="kept" disabled>
<input type="file" name="file">
<input type="submit" name="submit_button" value="Go">
<input type="button" name="button" value="Preview">
<input type="reset" name="reset">
<input type="image" name="image" src="x.png">
<input type="text" value="no name">
<select name="none_selected"><option disabled>Pick</option><option>  First
  option </option><option value="2">Second</option></select>
<select name="two_selected"><option value="1" selected>1</option><option value="2" selected>2</option></select>
<select name="multiple" multiple><option value="a" selected>a</option><option value="b">b</option>
<option value="c" selected>c</option></select>
<select name="multiple_none" multiple><option value="a">a</option></select>
<textarea name="text">
first line &lt;kept&gt;
second line</textarea>
</form>"""


def test_form_fields_are_the_ones_a_browser_submits() -> None:
    from bs4 import BeautifulSoup, Tag

    from salmon.trackers.base import _submitted_fields

    form = BeautifulSoup(BROWSER_RULES_FORM, "lxml").find("form")
    assert isinstance(form, Tag)

    assert _submitted_fields(form) == [
        ("hidden", "a &amp; b"),
        ("untyped", "x"),
        ("empty", ""),
        ("checked", "on"),
        ("valued", "1"),
        ("choice", "two"),
        ("disabled", "kept"),
        ("none_selected", "First option"),
        ("two_selected", "2"),
        ("multiple", "a"),
        ("multiple", "c"),
        ("text", "first line <kept>\nsecond line"),
    ]
