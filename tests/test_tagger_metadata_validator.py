from salmon.tagger import metadata_validator_base


def _valid_metadata(**overrides):
    metadata = {
        "artists": [("Main Artist", "main"), ("Some Arranger", "arranger")],
        "tracks": {"1": {"1": {"artists": [("Main Artist", "main")]}}},
        "year": 2020,
        "rls_type": "Album",
        "genres": ["Electronic"],
        "source": "WEB",
        "label": "Test Label",
        "catno": None,
    }
    metadata.update(overrides)
    return metadata


def test_metadata_validator_accepts_arranger() -> None:
    metadata = _valid_metadata()

    result = metadata_validator_base(metadata)

    assert result is metadata
    assert ("Some Arranger", "arranger") in metadata["artists"]
