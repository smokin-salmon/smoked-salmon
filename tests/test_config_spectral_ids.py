"""default_spectral_ids takes "*", "+", "0" or a list of track IDs such as "3" or "1 5 9"."""

import msgspec
import pytest

from salmon.config.validations import ImageUploader


def _image(**settings: object) -> ImageUploader:
    return msgspec.convert(settings, ImageUploader)


def test_unset_stays_unset() -> None:
    assert _image().default_spectral_ids is None


@pytest.mark.parametrize("value", ["*", "+", "0", "3", "12", "03", "1 5 9", "1  5"])
def test_accepts_a_selection_or_track_ids(value: str) -> None:
    assert _image(default_spectral_ids=value).default_spectral_ids == value


@pytest.mark.parametrize(
    "value",
    ["", " ", "all", "1,5", "1, 5", "-1", "0 3", "3 0", "00", " 3", "3 ", "* 3", "+3", "1\t5"],
)
def test_rejects_anything_else(value: str) -> None:
    with pytest.raises(msgspec.ValidationError):
        _image(default_spectral_ids=value)
