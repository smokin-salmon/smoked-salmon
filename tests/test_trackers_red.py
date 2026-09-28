from bs4 import BeautifulSoup

from salmon.trackers.red import _parse_upload_form


def test_parse_upload_form_round_trips_arranger_importance() -> None:
    html = """
    <form>
        <input name="artists[]" value="Main Artist">
        <select name="importance[]"><option value="1" selected>Main</option></select>
        <input name="artists[]" value="Some Arranger">
        <select name="importance[]"><option value="8" selected>Arranger</option></select>
    </form>
    """
    soup = BeautifulSoup(html, "html.parser")
    data: dict = {}

    _parse_upload_form(data, soup)

    assert data["artists[]"] == ["Main Artist", "Some Arranger"]
    assert data["importance[]"] == [1, 8]
