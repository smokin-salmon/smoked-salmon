import anyio
import pytest

from salmon.uploader import spectrals

FOUR_TRACKS = {1: "01 a.flac", 2: "02 b.flac", 3: "03 c.flac", 4: "04 d.flac"}


@pytest.fixture(autouse=True)
def _interactive(monkeypatch) -> None:
    monkeypatch.setattr(spectrals.cfg.upload, "yes_all", False)


def _accept_default(monkeypatch) -> list[str]:
    """Answer the spectral IDs prompt with its default, and record the defaults offered."""
    offered: list[str] = []

    async def prompt(_text: str, default: str, **_kwargs) -> str:
        offered.append(default)
        if len(offered) > 1:
            raise AssertionError(f"asked again after the default was rejected: {offered}")
        return default

    monkeypatch.setattr(spectrals.click, "prompt", prompt)
    return offered


@pytest.mark.parametrize(
    ("configured", "offered", "picked"),
    [
        ("3", "3", [3]),
        ("1 3", "1 3", [1, 3]),
        # IDs the release does not have are dropped from the default.
        ("2 9", "2", [2]),
        ("03", "03", [3]),
    ],
)
def test_configured_track_ids_are_the_default(monkeypatch, configured: str, offered: str, picked: list[int]) -> None:
    monkeypatch.setattr(spectrals.cfg.image, "default_spectral_ids", configured)
    offers = _accept_default(monkeypatch)

    result = anyio.run(spectrals.prompt_spectrals, FOUR_TRACKS, False, True)

    assert offers == [offered]
    assert result == {i: FOUR_TRACKS[i] for i in picked}


@pytest.mark.parametrize(("lossy_master", "offered"), [(False, "+"), (True, "*")])
def test_configured_track_ids_beyond_the_last_track_fall_back(monkeypatch, lossy_master: bool, offered: str) -> None:
    monkeypatch.setattr(spectrals.cfg.image, "default_spectral_ids", "5 9")
    offers = _accept_default(monkeypatch)

    result = anyio.run(spectrals.prompt_spectrals, FOUR_TRACKS, lossy_master, True)

    # The context default, as if nothing were configured: never a default the prompt itself rejects.
    assert offers == [offered]
    assert result
    assert set(result) <= set(FOUR_TRACKS)


@pytest.mark.parametrize(("configured", "expected"), [("*", FOUR_TRACKS), ("0", None)])
def test_selections_behave_as_before(monkeypatch, configured: str, expected) -> None:
    monkeypatch.setattr(spectrals.cfg.image, "default_spectral_ids", configured)
    offers = _accept_default(monkeypatch)

    assert anyio.run(spectrals.prompt_spectrals, FOUR_TRACKS, False, True) == expected
    assert offers == [configured]
