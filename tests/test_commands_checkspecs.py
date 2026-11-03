"""`salmon checkspecs` reads the torrent id it's given, or says it isn't one (#910).

A fake tracker records the id asked for and stops there: no tracker is contacted.
"""

import anyio
import asyncclick as click
import pytest

import salmon.commands
import salmon.trackers


class _Asked(Exception):
    pass


@pytest.fixture
def asked(monkeypatch) -> list[tuple[str, int]]:
    """The (tracker, torrent id) checkspecs asks its tracker for."""
    calls: list[tuple[str, int]] = []

    async def validate_tracker(_ctx, _param, value):
        return value or "RED"

    def get_class(tracker):
        class FakeSite:
            async def api_call(self, action, params):
                calls.append((tracker, params["id"]))
                raise _Asked

        return FakeSite

    monkeypatch.setattr(salmon.trackers, "validate_tracker", validate_tracker)
    monkeypatch.setattr(salmon.trackers, "get_class", get_class)
    return calls


def _checkspecs(torrent_id: str, tmp_path) -> None:
    callback = salmon.commands.checkspecs.callback
    assert callback is not None
    anyio.run(lambda: callback(tracker=None, torrent_id=torrent_id, path=str(tmp_path)))


@pytest.mark.parametrize(
    "torrent_id",
    [
        pytest.param("https://redacted.sh/torrents.php?id=123", id="a group link, no torrent id"),
        pytest.param("https://redacted.sh/torrents.php?torrentid=", id="an empty torrent id"),
        pytest.param("https://redacted.sh/torrents.php?torrentid=abc", id="a torrent id that isn't a number"),
        pytest.param("12²", id="a superscript digit"),
    ],
)
def test_a_torrent_id_that_cant_be_read_is_refused(asked, tmp_path, capsys, torrent_id) -> None:
    # On master these raised KeyError or ValueError (#910 item 8).
    with pytest.raises(click.Abort):
        _checkspecs(torrent_id, tmp_path)

    assert "Not a valid torrent!" in capsys.readouterr().out
    assert asked == []


@pytest.mark.parametrize(
    ("torrent_id", "tracker_and_id"),
    [
        pytest.param("123", ("RED", 123), id="an id"),
        pytest.param(" 123 ", ("RED", 123), id="an id with spaces"),
        pytest.param("https://redacted.sh/torrents.php?torrentid=123", ("RED", 123), id="a RED torrent link"),
        pytest.param(
            "https://redacted.sh/torrents.php?id=5&torrentid=123", ("RED", 123), id="a group and torrent link"
        ),
        pytest.param("https://orpheus.network/torrents.php?torrentid=123", ("OPS", 123), id="an OPS torrent link"),
        pytest.param("https://redacted.sh/torrents.php?torrentid=123 ", ("RED", 123), id="a link with a space after"),
        pytest.param(
            "https://redacted.sh/torrents.php?torrentid=+123", ("RED", 123), id="a link with an encoded space"
        ),
    ],
)
def test_a_torrent_id_is_read_as_before(asked, tmp_path, torrent_id, tracker_and_id) -> None:
    with pytest.raises(_Asked):
        _checkspecs(torrent_id, tmp_path)

    assert asked == [tracker_and_id]


@pytest.mark.parametrize(
    ("torrent_id", "message"),
    [
        pytest.param("abc", "Not a valid torrent!", id="not an id"),
        pytest.param(
            "https://example.com/torrents.php?torrentid=123", "Unrecognised tracker!", id="an unknown tracker"
        ),
    ],
)
def test_other_input_is_refused_as_before(asked, tmp_path, capsys, torrent_id, message) -> None:
    with pytest.raises(click.Abort):
        _checkspecs(torrent_id, tmp_path)

    assert message in capsys.readouterr().out
    assert asked == []
