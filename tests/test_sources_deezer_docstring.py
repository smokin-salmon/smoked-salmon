from salmon.sources.deezer import DeezerBase


def test_deezer_base_has_a_docstring():
    """The docstring must be the class body's first statement, or __doc__ is None."""
    assert DeezerBase.__doc__ is not None
    assert "Deezer" in DeezerBase.__doc__
