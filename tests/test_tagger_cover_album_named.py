"""A root image named after the album is its cover (#1075): found, offered a rename, and never second to a download."""

import io
import struct
from pathlib import Path

import anyio
import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

from salmon import cfg, interaction, uploader
from salmon.tagger import cover
from salmon.trackers.red import RedApi
from salmon.uploader import cover_upload

KIB = 1024
AUDIO = b"\xff\xf8 not really audio " * 1000


def _write_flac(path: Path, *, pictures: tuple[bytes, ...] = (), **tags: str) -> None:
    """Write a FLAC with fake audio, the given tags, and front-cover pictures."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo + AUDIO)
    audio = FLAC(path)
    for key, value in tags.items():
        audio[key] = value
    for data in pictures:
        picture = Picture()
        picture.type = PictureType.COVER_FRONT
        picture.mime = "image/jpeg"
        picture.data = data
        audio.add_picture(picture)
    audio.save(padding=lambda _info: 8 * KIB)


def _image(size: int = 0) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "green").save(buffer, "jpeg")
    return buffer.getvalue().ljust(size, b"\0")


JPEG = _image()
STORE = "https://store/cover.jpg"


@pytest.fixture
def album(tmp_path: Path) -> Path:
    """A folder with one FLAC tagged as Artist's Tin Cup."""
    _write_flac(tmp_path / "01.flac", album="Tin Cup", albumartist="Artist")
    return tmp_path


@pytest.fixture
def downloads(monkeypatch) -> list[str]:
    """Stands in for the store: a download writes Cover.jpg, and is recorded."""
    urls: list[str] = []

    async def fake_download(path: str, url: str) -> str:
        urls.append(url)
        downloaded = Path(path) / "Cover.jpg"
        downloaded.write_bytes(JPEG)
        return str(downloaded)

    monkeypatch.setattr(cover, "_download_cover", fake_download)
    return urls


@pytest.fixture
def asked(monkeypatch) -> list[str]:
    """Answers every question with yes, and records what was asked."""
    questions: list[str] = []

    async def confirm(text: str, **_kwargs) -> bool:
        questions.append(text)
        return True

    monkeypatch.setattr(cover.interaction, "confirm", confirm)
    return questions


def _use(folder: Path, url: str | None = STORE) -> tuple[str | None, bool | None]:
    return anyio.run(cover.download_cover_if_nonexistent, str(folder), url)


def _two_images(folder: Path, name: str) -> None:
    """The named image and another one, so that it is the title that matches, not the lone image."""
    (folder / name).write_bytes(JPEG)
    (folder / "other.png").write_bytes(JPEG)


# --- The lookup -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["Tin Cup.jpg", "Artist - Tin Cup.jpg", "tin cup.JPG", "ARTIST - TIN CUP.png", "Tin Cup.jpeg"]
)
def test_an_image_named_after_the_album_is_its_cover(album, downloads, name) -> None:
    _two_images(album, name)

    found = _use(album)
    assert found == (str(album / name), False)
    assert downloads == []


@pytest.mark.parametrize("stand_in", ["_", "："])
def test_a_forbidden_character_its_fullwidth_form_and_underscore_compare_equal(tmp_path, downloads, stand_in) -> None:
    _write_flac(tmp_path / "01.flac", album="Tin Cup: The Movie", albumartist="Artist")
    name = f"Tin Cup{stand_in} The Movie.jpg"
    _two_images(tmp_path, name)

    found = _use(tmp_path)
    assert found == (str(tmp_path / name), False)
    assert downloads == []


def test_the_configured_substitution_compares_equal(tmp_path, downloads, monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "blacklisted_substitution", "~")
    _write_flac(tmp_path / "01.flac", album="Tin Cup: The Movie", albumartist="Artist")
    _two_images(tmp_path, "Tin Cup~ The Movie.jpg")

    found = _use(tmp_path)
    assert found == (str(tmp_path / "Tin Cup~ The Movie.jpg"), False)


def test_colon_as_dash_names_the_subtitle_with_a_dash(tmp_path, downloads, monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "colon_as_dash", True)
    monkeypatch.setattr(cfg.upload.formatting, "fullwidth_replacements", False)
    _write_flac(tmp_path / "01.flac", album="Tin Cup: The Movie", albumartist="Artist")
    _two_images(tmp_path, "Tin Cup - The Movie.jpg")

    found = _use(tmp_path)
    assert found == (str(tmp_path / "Tin Cup - The Movie.jpg"), False)


def test_the_artist_comes_from_the_artist_tag_without_an_album_artist(tmp_path, downloads) -> None:
    _write_flac(tmp_path / "01.flac", album="Tin Cup", artist="Solo")
    _two_images(tmp_path, "Solo - Tin Cup.jpg")

    found = _use(tmp_path)
    assert found == (str(tmp_path / "Solo - Tin Cup.jpg"), False)


def test_the_artist_names_can_be_joined(tmp_path, downloads) -> None:
    _write_flac(tmp_path / "01.flac", album="Tin Cup")
    audio = FLAC(tmp_path / "01.flac")
    audio["artist"] = ["One", "Two"]
    audio.save()
    _two_images(tmp_path, "One & Two - Tin Cup.jpg")

    found = _use(tmp_path)
    assert found == (str(tmp_path / "One & Two - Tin Cup.jpg"), False)


def test_a_lone_image_is_the_cover(album, downloads) -> None:
    (album / "scan0001.jpg").write_bytes(JPEG)

    found = _use(album)
    assert found == (str(album / "scan0001.jpg"), False)
    assert downloads == []


def test_a_lone_image_is_the_cover_of_a_folder_with_no_readable_tag(tmp_path, downloads) -> None:
    (tmp_path / "01.flac").write_bytes(b"not a flac")
    (tmp_path / "Whatever.png").write_bytes(JPEG)

    found = _use(tmp_path)
    assert found == (str(tmp_path / "Whatever.png"), False)


@pytest.mark.parametrize("name", ["back.jpg", "Tin Cup (back).jpg", "Disc 1.png", "inlay-tray.jpg", "CD.JPG"])
def test_a_lone_back_or_disc_image_is_not_a_cover(album, downloads, name) -> None:
    (album / name).write_bytes(JPEG)

    found = _use(album)
    assert found == (str(album / "Cover.jpg"), True)
    assert downloads == [STORE]


def test_a_word_that_only_starts_with_a_marker_word_does_not_count(album, downloads) -> None:
    (album / "Backstreet.jpg").write_bytes(JPEG)

    found = _use(album)
    assert found == (str(album / "Backstreet.jpg"), False)
    assert downloads == []


def test_two_images_that_are_not_named_after_the_album_are_no_cover(album, downloads) -> None:
    (album / "a.jpg").write_bytes(JPEG)
    (album / "b.png").write_bytes(JPEG)

    found = _use(album)
    assert found == (str(album / "Cover.jpg"), True)
    assert downloads == [STORE]


def test_two_images_one_named_after_the_album_is_that_one(album, downloads) -> None:
    (album / "a.jpg").write_bytes(JPEG)
    (album / "Tin Cup.png").write_bytes(JPEG)

    found = _use(album)
    assert found == (str(album / "Tin Cup.png"), False)
    assert downloads == []


def test_a_named_cover_wins_over_the_albums_name_and_nothing_is_asked(album, downloads, asked) -> None:
    (album / "Tin Cup.jpg").write_bytes(JPEG)
    (album / "cover.jpg").write_bytes(JPEG)

    found = _use(album)
    assert found == (str(album / "cover.jpg"), False)
    anyio.run(cover.offer_cover_rename, str(album))
    assert asked == []
    assert (album / "Tin Cup.jpg").exists()


def test_an_image_in_a_subfolder_does_not_count(album, downloads) -> None:
    (album / "Scans").mkdir()
    (album / "Scans" / "Tin Cup.jpg").write_bytes(JPEG)

    found = _use(album)
    assert found == (str(album / "Cover.jpg"), True)


def test_an_unreadable_audio_file_is_skipped(tmp_path, downloads) -> None:
    (tmp_path / "00.flac").write_bytes(b"fLaC broken")
    _write_flac(tmp_path / "01.flac", album="Tin Cup")
    _two_images(tmp_path, "Tin Cup.jpg")

    found = _use(tmp_path)
    assert found == (str(tmp_path / "Tin Cup.jpg"), False)


# --- The rename offer -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Tin Cup.jpg", "scan0001.PNG"])
def test_yes_renames_the_image_to_cover(album, downloads, asked, name) -> None:
    (album / name).write_bytes(JPEG)
    extension = Path(name).suffix

    anyio.run(cover.offer_cover_rename, str(album))

    assert len(asked) == 1
    assert name in asked[0]
    assert (album / f"cover{extension}").read_bytes() == JPEG
    assert not (album / name).exists()
    found = _use(album)
    assert found == (str(album / f"cover{extension}"), False)
    assert downloads == []


def test_assume_defaults_renames_without_asking(album, asked) -> None:
    (album / "Tin Cup.jpg").write_bytes(JPEG)

    async def run() -> None:
        with interaction.assuming_defaults():
            await cover.offer_cover_rename(str(album))

    anyio.run(run)

    assert asked == []
    assert (album / "cover.jpg").exists()
    assert not (album / "Tin Cup.jpg").exists()


def test_no_keeps_the_name_and_the_file_is_used(album, downloads, monkeypatch) -> None:
    (album / "Tin Cup.jpg").write_bytes(JPEG)
    questions: list[str] = []

    async def no(text: str, **_kwargs) -> bool:
        questions.append(text)
        return False

    monkeypatch.setattr(cover.interaction, "confirm", no)

    anyio.run(cover.offer_cover_rename, str(album))

    assert len(questions) == 1
    assert (album / "Tin Cup.jpg").exists()
    assert not (album / "cover.jpg").exists()
    found = _use(album)
    assert found == (str(album / "Tin Cup.jpg"), False)
    assert downloads == []


def test_without_lowercase_cover_it_is_renamed_to_capital_cover(album, asked, monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "lowercase_cover", False)
    (album / "Tin Cup.jpg").write_bytes(JPEG)

    anyio.run(cover.offer_cover_rename, str(album))

    assert sorted(p.name for p in album.iterdir()) == ["01.flac", "Cover.jpg"]


def test_a_failed_rename_is_said_and_the_file_is_used(album, asked, monkeypatch, capsys) -> None:
    (album / "Tin Cup.jpg").write_bytes(JPEG)

    def refuse(_src: str, _dest: str) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(cover.os, "rename", refuse)

    anyio.run(cover.offer_cover_rename, str(album))

    assert "Could not rename Tin Cup.jpg to cover.jpg" in capsys.readouterr().out
    found = _use(album, None)
    assert found == (str(album / "Tin Cup.jpg"), False)


def test_a_folder_that_may_hold_a_library_album_is_left_as_it_is(album, downloads, asked, monkeypatch) -> None:
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(album.parent)])
    (album / "Tin Cup.jpg").write_bytes(JPEG)

    anyio.run(cover.offer_cover_rename, str(album))

    assert asked == []
    assert (album / "Tin Cup.jpg").exists()
    found = _use(album)
    assert found == (str(album / "Tin Cup.jpg"), False)
    assert downloads == []


def test_a_folder_with_no_match_is_not_offered_anything(album, asked) -> None:
    (album / "a.jpg").write_bytes(JPEG)
    (album / "b.jpg").write_bytes(JPEG)

    anyio.run(cover.offer_cover_rename, str(album))

    assert asked == []


def test_download_cover_if_nonexistent_never_renames(album, asked) -> None:
    (album / "Tin Cup.jpg").write_bytes(JPEG)

    _use(album)

    assert asked == []
    assert (album / "Tin Cup.jpg").exists()


def test_the_per_tracker_cover_step_never_renames(album, asked, monkeypatch) -> None:
    (album / "Tin Cup.jpg").write_bytes(JPEG)

    async def fake_upload_cover(cover_path: str | None, host: str | None = None, red_api: object = None) -> str:
        assert cover_path == str(album / "Tin Cup.jpg")
        return "https://catbox/uploaded.jpg"

    monkeypatch.setattr(cover_upload, "upload_cover", fake_upload_cover)

    result = anyio.run(cover_upload.resolve_cover_url, RedApi(), None, {}, str(album), STORE, False)

    assert result == (True, "https://catbox/uploaded.jpg")
    assert asked == []
    assert (album / "Tin Cup.jpg").exists()


def test_prepare_pictures_offers_the_rename_before_checking_the_pictures(album, monkeypatch) -> None:
    calls: list[str] = []

    async def offer(path: str) -> None:
        calls.append("offer")

    monkeypatch.setattr(uploader, "offer_cover_rename", offer)
    monkeypatch.setattr(uploader, "check_embedded_pictures", lambda path: calls.append("check"))
    monkeypatch.setattr(cfg.image, "embed_missing_cover", False)

    anyio.run(uploader._prepare_pictures, str(album), {"cover": None}, False)

    assert calls == ["offer", "check"]


# --- What may cost the embedded artwork -----------------------------------------------------------------


def _oversized(folder: Path) -> None:
    _write_flac(folder / "01.flac", pictures=(_image(1500 * KIB),), album="Tin Cup", albumartist="Artist")


def test_a_lone_image_does_not_stand_for_oversized_embedded_artwork(tmp_path) -> None:
    _oversized(tmp_path)
    (tmp_path / "photo.jpg").write_bytes(JPEG)

    cover.check_embedded_pictures(str(tmp_path))

    # The artwork is kept as a cover file before the FLAC is stripped: the photo may not be the front cover.
    assert (tmp_path / "cover.jpg").exists()
    assert (tmp_path / "photo.jpg").read_bytes() == JPEG
    assert FLAC(tmp_path / "01.flac").pictures == []


def test_a_title_match_stands_for_oversized_embedded_artwork(tmp_path) -> None:
    _oversized(tmp_path)
    _two_images(tmp_path, "Tin Cup.jpg")

    cover.check_embedded_pictures(str(tmp_path))

    assert not (tmp_path / "cover.jpg").exists()
    assert FLAC(tmp_path / "01.flac").pictures == []


def test_a_lone_image_does_not_stop_the_embedded_cover_being_extracted(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", pictures=(_image(),), album="Tin Cup")
    (tmp_path / "photo.jpg").write_bytes(JPEG)

    extracted = cover.extract_embedded_cover(str(tmp_path))
    assert extracted == str(tmp_path / "cover.jpg")


def test_a_title_match_is_the_cover_extract_embedded_cover_returns(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", pictures=(_image(),), album="Tin Cup")
    _two_images(tmp_path, "Tin Cup.jpg")

    extracted = cover.extract_embedded_cover(str(tmp_path))
    assert extracted == str(tmp_path / "Tin Cup.jpg")
    assert not (tmp_path / "cover.jpg").exists()


def test_embed_missing_cover_embeds_a_lone_image(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", album="Tin Cup")
    (tmp_path / "scan.jpg").write_bytes(JPEG)

    cover.embed_missing_cover(str(tmp_path))

    assert len(FLAC(tmp_path / "01.flac").pictures) == 1
