"""catbox ImageUploader: a 200 with no usable URL must raise, not report success."""

from contextlib import asynccontextmanager

import aiohttp
import anyio
import pytest

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images import catbox


class _Response:
    def __init__(self, body: str):
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self) -> None:
        pass

    async def text(self) -> str:
        return self._body


class _Session:
    def __init__(self, body: str):
        self._body = body
        self.form: aiohttp.FormData | None = None

    def post(self, *_args, data: aiohttp.FormData | None = None, **_kwargs):
        self.form = data
        return _Response(self._body)


def _upload(monkeypatch, tmp_path, body: str, session: _Session | None = None):
    session = session or _Session(body)

    @asynccontextmanager
    async def fake_http_session(_self):
        yield session

    monkeypatch.setattr(catbox.ImageUploader, "_http_session", fake_http_session)
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"\xff\xd8\xff")
    return anyio.run(catbox.ImageUploader().upload_file, str(image))


def test_a_real_url_is_returned(monkeypatch, tmp_path) -> None:
    url, deletion_url = _upload(monkeypatch, tmp_path, "https://files.catbox.moe/abc123.jpg")
    assert url == "https://files.catbox.moe/abc123.jpg"
    assert deletion_url is None


def test_a_url_with_surrounding_whitespace_is_stripped(monkeypatch, tmp_path) -> None:
    url, _ = _upload(monkeypatch, tmp_path, "https://files.catbox.moe/abc123.jpg\n")
    assert url == "https://files.catbox.moe/abc123.jpg"


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "Something went wrong, please try again later.",
    ],
)
def test_a_response_without_a_url_raises_instead_of_reporting_success(monkeypatch, tmp_path, body) -> None:
    with pytest.raises(ImageUploadFailed):
        _upload(monkeypatch, tmp_path, body)


@pytest.mark.parametrize(("userhash", "sent"), [(None, ""), ("abc123", "abc123")])
def test_the_configured_userhash_is_sent(monkeypatch, tmp_path, userhash, sent) -> None:
    monkeypatch.setattr(cfg.image, "catbox_userhash", userhash)
    session = _Session("https://files.catbox.moe/abc123.jpg")
    _upload(monkeypatch, tmp_path, "", session)
    assert session.form is not None
    fields = {f[0]["name"]: f[2] for f in session.form._fields}
    assert fields["userhash"] == sent
