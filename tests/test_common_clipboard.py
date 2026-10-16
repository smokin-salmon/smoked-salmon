"""A machine with no clipboard (a server, the Docker image) makes pyperclip raise: nothing salmon copies fails over it.

No image host or store is contacted: both are stubbed.
"""

from types import SimpleNamespace

import anyio
import pyperclip
import pytest

import salmon.commands
from salmon import cfg, images
from salmon.commands import descgen
from salmon.common import clipboard_copy
from salmon.images.base import BaseImageUploader

NO_CLIPBOARD = "Pyperclip could not find a copy/paste mechanism for your system."


@pytest.fixture(autouse=True)
def no_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    def copy(_text: str) -> None:
        raise pyperclip.PyperclipException(NO_CLIPBOARD)

    monkeypatch.setattr(pyperclip, "copy", copy)
    monkeypatch.setattr(cfg.upload.description, "copy_uploaded_url_to_clipboard", True)


def test_a_copy_that_fails_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    clipboard_copy("https://tracker.test/torrents.php?torrentid=1")

    assert f"Could not copy to the clipboard: {NO_CLIPBOARD}" in capsys.readouterr().out


def test_uploaded_images_are_still_given(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    picture = tmp_path / "front.png"
    picture.write_bytes(b"png")

    class ImageUploader(BaseImageUploader):
        async def upload_file(self, filename: str) -> tuple[str, None]:
            return "https://images.test/front.png", None

    urls = anyio.run(images.upload_images, (str(picture),), SimpleNamespace(ImageUploader=ImageUploader))

    assert urls == ["https://images.test/front.png"]
    out = capsys.readouterr().out
    assert "https://images.test/front.png\n" in out
    assert f"Could not copy to the clipboard: {NO_CLIPBOARD}" in out


def test_descgen_still_prints_the_description(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    metadata = {
        "tracks": {"1": {"1": {"artists": [("Artist", "main")], "title": "First"}}},
        "comment": None,
        "urls": [],
    }

    async def run_metadata(url, return_source_name=False):
        return metadata, "Qobuz"

    monkeypatch.setattr(salmon.commands, "run_metadata", run_metadata)
    monkeypatch.setattr(salmon.commands, "combine_metadatas", lambda *_: metadata)
    monkeypatch.setattr(salmon.commands, "clean_metadata", lambda found: found)
    assert descgen.callback is not None

    anyio.run(descgen.callback, ("https://www.qobuz.com/album/x",))

    out = capsys.readouterr().out
    assert "[b][size=4]Tracklist[/size][/b]" in out
    assert f"Could not copy to the clipboard: {NO_CLIPBOARD}" in out
