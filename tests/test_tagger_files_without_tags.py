"""A FLAC or M4A with no tags at all gets the tags a retag writes (#847)."""

import av
import numpy as np
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from test_tagger_multi_value_tags import _write_flac  # pyright: ignore[reportMissingImports]

from salmon.tagger.retagger import retag_files


def _write_tagless_m4a(path) -> None:
    """An M4A muxed as QuickTime ("mov"), which leaves it with no tags at all, unlike the "ipod" muxer."""
    with av.open(str(path), "w", format="mov") as container:
        stream = container.add_stream("aac", rate=44100, layout="stereo")
        frame = av.AudioFrame.from_ndarray(np.zeros((2, 2048), dtype=np.float32), format="fltp", layout="stereo")
        frame.sample_rate = 44100
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    assert MP4(path).tags is None


def test_a_flac_with_no_vorbis_comment_block_is_tagged(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac")
    assert FLAC(tmp_path / "01.flac").tags is None

    retag_files(str(tmp_path), {"album": "Album", "catno": "CAT1"}, {"01.flac": []})

    tags = FLAC(tmp_path / "01.flac")
    assert (tags.get("album"), tags.get("catalognumber")) == (["Album"], ["CAT1"])


def test_an_m4a_with_no_tags_is_tagged(tmp_path) -> None:
    _write_tagless_m4a(tmp_path / "01.m4a")

    retag_files(str(tmp_path), {"album": "Album"}, {"01.m4a": []})

    assert (MP4(tmp_path / "01.m4a").tags or {}).get("\xa9alb") == ["Album"]


def test_an_m4a_given_only_a_tag_it_has_no_field_for_is_left_as_it_was(tmp_path) -> None:
    # An M4A has no catalogue number field.
    _write_tagless_m4a(tmp_path / "01.m4a")

    retag_files(str(tmp_path), {"catno": "CAT1"}, {"01.m4a": []})

    assert MP4(tmp_path / "01.m4a").tags is None
