"""An MP3's comment is its COMM frame with no description; other programs' comment frames are left alone (#864)."""

from helpers.tagger_multi_value_tags import _write_mp3
from mutagen.id3 import COMM, ID3, TRCK

from salmon.tagger.tagfile import TagFile

ITUNES = (
    COMM(encoding=3, lang="eng", desc="iTunSMPB", text="0000 0210"),
    COMM(encoding=3, lang="eng", desc="iTunNORM", text="0000 1111"),
)


def _mp3(tmp_path, *frames):
    path = tmp_path / "01.mp3"
    _write_mp3(path, TRCK(encoding=3, text="1"), *frames)
    return path


def _comments(path) -> dict[str, str]:
    return {f.desc: "; ".join(f.text) for f in ID3(path).getall("COMM")}


def test_reads_the_comment_with_no_description(tmp_path) -> None:
    for lang in ("XXX", "eng"):
        path = _mp3(tmp_path, COMM(encoding=3, lang=lang, desc="", text="Review text"))
        assert TagFile(str(path)).comment == "Review text"


def test_a_described_comment_is_not_the_comment(tmp_path) -> None:
    path = _mp3(tmp_path, ITUNES[1])

    assert TagFile(str(path)).comment is None


def test_writing_the_comment_keeps_other_comment_frames(tmp_path) -> None:
    path = _mp3(tmp_path, *ITUNES)

    tf = TagFile(str(path))
    tf.comment = "Review text"
    tf.save()

    assert _comments(path) == {"": "Review text", "iTunSMPB": "0000 0210", "iTunNORM": "0000 1111"}


def test_writing_the_comment_twice_leaves_one_frame(tmp_path) -> None:
    path = _mp3(tmp_path, COMM(encoding=3, lang="eng", desc="", text="Old"))

    for text in ("First", "Second"):
        tf = TagFile(str(path))
        tf.comment = text
        tf.save()

    assert [f.text for f in ID3(path).getall("COMM") if f.desc == ""] == [["Second"]]


def test_removing_the_comment_removes_only_the_empty_description_frame(tmp_path) -> None:
    path = _mp3(tmp_path, COMM(encoding=3, lang="XXX", desc="", text="Review text"), *ITUNES)

    tf = TagFile(str(path))
    tf.remove("comment")
    tf.save()

    assert _comments(path) == {"iTunSMPB": "0000 0210", "iTunNORM": "0000 1111"}
