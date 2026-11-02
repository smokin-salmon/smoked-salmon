"""Tags are never written to a fragmented M4A (mutagen#748): the file is left as it is and salmon says so."""

import av
import numpy as np
import pytest
from mutagen.mp4 import MP4, Atoms

from salmon.errors import UploadError
from salmon.tagger.retagger import Change, retag_files
from salmon.tagger.tagfile import TagFile, is_fragmented_mp4


def _write_m4a(path, fragmented: bool) -> None:
    options = {"movflags": "frag_keyframe+empty_moov", "frag_duration": "200000"} if fragmented else {}
    rng = np.random.default_rng(1)
    with av.open(str(path), "w", format="ipod", options=options) as container:
        container.metadata["album"] = "Old"
        stream = container.add_stream("aac", rate=44100, layout="stereo")
        for _ in range(40):
            samples = rng.uniform(-0.5, 0.5, (2, 4096)).astype(np.float32)
            frame = av.AudioFrame.from_ndarray(samples, format="fltp", layout="stereo")
            frame.sample_rate = 44100
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


def _moofs(path) -> int:
    with open(path, "rb") as fh:
        return sum(1 for atom in Atoms(fh).atoms if atom.name == b"moof")


def _fragmented(tmp_path, name="frag.m4a"):
    path = tmp_path / name
    _write_m4a(path, True)
    assert _moofs(path) > 1
    return path


def test_retag_leaves_a_fragmented_m4a_untouched_and_says_so(tmp_path, capsys):
    path = _fragmented(tmp_path)
    before = path.read_bytes()

    retag_files(str(tmp_path), {}, {path.name: [Change("album", "Old", "x" * 5000)]})

    assert path.read_bytes() == before
    out = capsys.readouterr().out
    assert "frag.m4a" in out and "fragmented M4A" in out


def test_clearing_a_tag_leaves_a_fragmented_m4a_untouched(tmp_path):
    path = _fragmented(tmp_path)
    before = path.read_bytes()

    tag_file = TagFile(str(path))
    assert tag_file.album == "Old"
    tag_file.remove("album")
    tag_file.save()

    assert path.read_bytes() == before


def test_an_unfragmented_m4a_beside_it_is_still_retagged(tmp_path):
    frag = _fragmented(tmp_path)
    plain = tmp_path / "plain.m4a"
    _write_m4a(plain, False)
    assert _moofs(plain) == 0

    retag_files(
        str(tmp_path),
        {},
        {frag.name: [Change("album", "Old", "New")], plain.name: [Change("album", "Old", "New")]},
    )

    assert MP4(plain)["\xa9alb"] == ["New"]


def test_is_fragmented_mp4(tmp_path):
    plain = tmp_path / "plain.m4a"
    _write_m4a(plain, False)
    garbage = tmp_path / "garbage.m4a"
    garbage.write_bytes(b"not an mp4 at all" * 10)

    assert not is_fragmented_mp4(str(plain))
    assert is_fragmented_mp4(str(_fragmented(tmp_path)))
    assert is_fragmented_mp4(str(garbage))


def test_a_failure_after_a_skipped_fragmented_m4a_lists_it_as_not_changed(tmp_path, capsys):
    frag = _fragmented(tmp_path)
    plain = tmp_path / "plain.m4a"
    _write_m4a(plain, False)
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"not audio" * 10)
    change = [Change("album", "Old", "New")]

    with pytest.raises(UploadError):
        retag_files(str(tmp_path), {}, {frag.name: change, plain.name: change, bad.name: change})

    lines = {line.split(":")[0]: line for line in capsys.readouterr().out.splitlines()}
    assert "plain.m4a" in lines["Already changed"] and "frag.m4a" not in lines["Already changed"]
    assert "frag.m4a" in lines["Not changed"]
