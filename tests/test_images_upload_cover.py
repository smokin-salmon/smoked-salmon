"""upload_cover: a host that returns without raising must still fail if it hands back no URL."""

from types import SimpleNamespace

import anyio

from salmon.images import HOSTS, upload_cover


class _FakeUploader:
    def __init__(self, url: str):
        self._url = url

    async def upload_file(self, _filename: str):
        return self._url, None


def _stub_host(monkeypatch, url: str) -> None:
    monkeypatch.setitem(HOSTS, "catbox", SimpleNamespace(ImageUploader=lambda: _FakeUploader(url)))


def test_no_path_reports_failure_without_touching_any_host() -> None:
    assert anyio.run(upload_cover, None, "catbox") is None


def test_a_real_url_is_returned(monkeypatch, tmp_path) -> None:
    _stub_host(monkeypatch, "https://files.catbox.moe/abc.jpg")
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"data")
    assert anyio.run(upload_cover, str(image), "catbox") == "https://files.catbox.moe/abc.jpg"


def test_an_empty_url_is_treated_as_a_failure_not_a_success(monkeypatch, tmp_path) -> None:
    # A host can return 200 with nothing usable; upload_cover must not cache/report that as done.
    _stub_host(monkeypatch, "")
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"data")
    assert anyio.run(upload_cover, str(image), "catbox") is None
