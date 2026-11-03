"""RED's client: reading its upload form, and the requests an upload with rip logs sends (#863).

An upload with logs takes the path of any other upload: the API with an API key, upload.php with a session cookie
only. Tests talk only to a local fake RED.
"""

from pathlib import Path

import anyio
from aiohttp import web
from aiolimiter import AsyncLimiter
from bs4 import BeautifulSoup
from torf import Torrent

from salmon.common import UploadFiles
from salmon.trackers.red import RedApi, _parse_upload_form
from salmon.uploader.upload import collect_logfiles

API_KEY = "red-api-key-0123456789"
SESSION = "red-session-cookie"
AUTHKEY = "authkey-0123456789abcdef"
PASSKEY = "passkey-0123456789abcdef"
TORRENT_ID = 700001
GROUP_ID = 5
# What RED's upload page fills in for an upload into GROUP_ID.
GROUP_FORM = (
    '<html><form><input name="artists[]" value="Group Artist">'
    '<select name="importance[]"><option value="1" selected>Main</option></select>'
    '<input name="title" value="Group Title"><input name="year" value="1999"></form></html>'
)


def test_parse_upload_form_round_trips_arranger_importance() -> None:
    html = """
    <form>
        <input name="artists[]" value="Main Artist">
        <select name="importance[]"><option value="1" selected>Main</option></select>
        <input name="artists[]" value="Some Arranger">
        <select name="importance[]"><option value="8" selected>Arranger</option></select>
    </form>
    """
    soup = BeautifulSoup(html, "html.parser")
    data: dict = {}

    _parse_upload_form(data, soup)

    assert data["artists[]"] == ["Main Artist", "Some Arranger"]
    assert data["importance[]"] == [1, 8]


class FakeRed:
    """A local RED answering uploads through its API and its upload page, recording each request."""

    def __init__(self) -> None:
        self.url = ""
        # (method, path with query, how it authenticated: "api key", "cookie", "both" or "none") of each request.
        self.hits: list[tuple[str, str, str]] = []
        # (name, file name, content) of each part of the last POST, in order; the file name is None for a field.
        self.parts: list[tuple[str, str | None, bytes]] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        api_key = request.headers.get("Authorization") == API_KEY
        cookie = f"session={SESSION}" in request.headers.get("Cookie", "")
        auth = {(True, False): "api key", (False, True): "cookie", (True, True): "both"}.get((api_key, cookie), "none")
        self.hits.append((request.method, request.path_qs, auth))
        if request.method == "POST":
            self.parts = []
            for name, value in (await request.post()).items():
                if isinstance(value, web.FileField):
                    self.parts.append((name, value.filename, value.file.read()))
                else:
                    self.parts.append((name, None, str(value).encode()))
        if request.path == "/ajax.php" and request.query.get("action") == "upload":
            group_id = int(dict(self.fields()).get("groupid", GROUP_ID))
            return web.json_response({"status": "success", "response": {"torrentid": TORRENT_ID, "groupid": group_id}})
        if request.path == "/upload.php" and request.method == "GET":
            return web.Response(text=GROUP_FORM, content_type="text/html")
        if request.path == "/upload.php":
            # Gazelle's site answers an upload with a redirect to the new torrent on its group page.
            group_id = int(dict(self.fields()).get("groupid", GROUP_ID))
            raise web.HTTPFound(f"/torrents.php?id={group_id}&torrentid={TORRENT_ID}")
        if request.path == "/torrents.php":
            group_id = request.query["id"]
            page = (
                f'<html><a class="tooltip" href="torrents.php?torrentid={TORRENT_ID}">DL</a>'
                f'<a class="brackets" href="upload.php?groupid={group_id}">Add format</a></html>'
            )
            return web.Response(text=page, content_type="text/html")
        return web.Response(status=404)

    def fields(self) -> list[tuple[str, str]]:
        """The parts of the last POST that are not files, as text."""
        return [(name, value.decode()) for name, filename, value in self.parts if filename is None]

    def logs(self) -> list[tuple[str, bytes]]:
        """The file name and content of each log the last POST sent as a file."""
        return [(filename, value) for name, filename, value in self.parts if name == "logfiles[]" and filename]


async def _upload(fake: FakeRed, files: UploadFiles, data: dict, api_key: str) -> tuple[int, int]:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    fake.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    api = RedApi()
    api.base_url = fake.url
    api.api_key = api_key
    api.cookie = SESSION
    # Authenticated already, as by the group search before an upload: only the upload's own requests are sent.
    api._authenticated = True
    api.authkey = AUTHKEY
    api.passkey = PASSKEY
    # Per instance, as each test runs on its own event loop.
    api._rate_limiter = AsyncLimiter(100, 1)
    try:
        return await api.upload(data, files)
    finally:
        await api.close()
        await runner.cleanup()


async def _three_disc_files(tmp_path: Path) -> tuple[UploadFiles, list[tuple[str, bytes]]]:
    """A 3-CD rip, each disc in its folder with its log, as salmon up sends it: the torrent and the logs it finds."""
    album = tmp_path / "Artist - Album (2000) [FLAC]"
    for disc in (1, 2, 3):
        folder = album / f"CD{disc}"
        folder.mkdir(parents=True)
        (folder / f"01 Track {disc}.flac").write_bytes(b"not really audio" * 64)
        (folder / f"CD{disc}.log").write_bytes(f"Exact Audio Copy V1.6 log of disc {disc}".encode())
    torrent = Torrent(album, trackers=["http://tracker.invalid/announce"], private=True, source="RED")
    torrent.generate()
    logs = await collect_logfiles(str(album))
    return UploadFiles(torrent_data=torrent.dump(), log_files=logs), logs


def _form(**fields: object) -> dict:
    return {"type": 0, "format": "FLAC", "bitrate": "Lossless", "media": "CD", **fields}


def test_an_upload_with_logs_into_a_new_group_goes_through_the_api_with_each_log(tmp_path: Path) -> None:
    files, logs = anyio.run(_three_disc_files, tmp_path)
    fake = FakeRed()

    ids = anyio.run(_upload, fake, files, _form(), API_KEY)

    assert ids == (TORRENT_ID, GROUP_ID)
    assert fake.hits == [("POST", "/ajax.php?action=upload", "api key")]
    assert sorted(name for name, _ in logs) == ["CD1.log", "CD2.log", "CD3.log"]
    assert sorted(fake.logs()) == sorted(logs)


def test_an_upload_with_logs_into_an_existing_group_goes_through_the_api_without_the_upload_page(
    tmp_path: Path,
) -> None:
    files, logs = anyio.run(_three_disc_files, tmp_path)
    fake = FakeRed()

    ids = anyio.run(_upload, fake, files, _form(groupid=GROUP_ID), API_KEY)

    assert ids == (TORRENT_ID, GROUP_ID)
    # No GET upload.php?groupid=: the API fills in the group's fields itself.
    assert fake.hits == [("POST", "/ajax.php?action=upload", "api key")]
    assert ("groupid", str(GROUP_ID)) in fake.fields()
    assert sorted(fake.logs()) == sorted(logs)


def test_with_a_session_cookie_only_an_upload_with_logs_into_a_group_still_fills_its_form_from_the_upload_page(
    tmp_path: Path,
) -> None:
    files, logs = anyio.run(_three_disc_files, tmp_path)
    fake = FakeRed()

    ids = anyio.run(_upload, fake, files, _form(groupid=GROUP_ID), "")

    assert ids == (TORRENT_ID, GROUP_ID)
    assert fake.hits == [
        ("GET", f"/upload.php?groupid={GROUP_ID}", "cookie"),
        ("POST", f"/upload.php?groupid={GROUP_ID}", "cookie"),
        ("GET", f"/torrents.php?id={GROUP_ID}&torrentid={TORRENT_ID}", "cookie"),  # The redirect to the new torrent
    ]
    # The fields the upload page filled in for the group went with the upload.
    assert ("title", "Group Title") in fake.fields()
    assert ("artists[]", "Group Artist") in fake.fields()
    assert sorted(fake.logs()) == sorted(logs)
