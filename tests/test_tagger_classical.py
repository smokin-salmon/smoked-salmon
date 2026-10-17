from types import SimpleNamespace

from salmon.tagger.pre_data import construct_track_artists
from salmon.tagger.retagger import create_artist_str, create_composer_str, create_track_changes
from salmon.tagger.tags import check_required_tags


def test_create_artist_str_includes_conductor_and_excludes_composer() -> None:
    artists = [
        ("Ensemble Carminé", "main"),
        ("Claire Chevalier", "conductor"),
        ("Alfonso X el Sabio", "composer"),
    ]

    assert create_artist_str(artists) == "Ensemble Carminé, Claire Chevalier"


def test_create_composer_str_preserves_input_order() -> None:
    artists = [
        ("Guillaume de Machaut", "composer"),
        ("Hildegard von Bingen", "composer"),
        ("Guillaume de Machaut", "composer"),
    ]

    assert create_composer_str(artists) == "Guillaume de Machaut, Hildegard von Bingen"


def test_create_track_changes_adds_composer_change_for_classical_track() -> None:
    tags = {
        "01. Example.flac": SimpleNamespace(
            artist=["Ensemble Carminé"],
            composer="Various composers",
            conductor=None,
            title="Alfonso X el Sabio - CSM 422. Madre de Deus, ora por nos",
            comment=None,
            isrc=None,
            tracknumber="01",
            discnumber="1",
            tracktotal="15",
            disctotal="1",
        )
    }
    metadata = {
        "tracks": {
            "1": {
                "1": {
                    "artists": [
                        ("Ensemble Carminé", "main"),
                        ("Claire Chevalier", "conductor"),
                        ("Alfonso X el Sabio", "composer"),
                    ],
                    "title": "CSM 422. Madre de Deus, ora por nos",
                    "track#": "01",
                    "disc#": "1",
                    "tracktotal": "15",
                    "disctotal": "1",
                    "isrc": None,
                }
            }
        }
    }

    changes = create_track_changes(tags, metadata)

    assert [change.tag for change in changes["01. Example.flac"]] == [
        "artist",
        "artists",
        "composer",
        "conductor",
        "title",
    ]


def test_construct_track_artists_recovers_conductor_and_composer_roles_from_tags() -> None:
    track = SimpleNamespace(
        artist=["Ensemble Carminé, Claire Chevalier"],
        conductor="Claire Chevalier",
        composer="Alfonso X el Sabio",
        genre="Classical",
    )

    assert construct_track_artists(track) == [
        ("Ensemble Carminé", "main"),
        ("Claire Chevalier", "conductor"),
        ("Alfonso X el Sabio", "composer"),
    ]


def test_construct_artists_li_preserves_first_seen_role_order() -> None:
    tags = {
        "01. Example.flac": SimpleNamespace(
            artist=["Ensemble Carminé, Claire Chevalier"],
            conductor="Claire Chevalier",
            composer="Alfonso X el Sabio",
            genre=["Classical"],
        ),
        "02. Example.flac": SimpleNamespace(
            artist=["Ensemble Carminé, Claire Chevalier"],
            conductor="Claire Chevalier",
            composer="Guillaume de Machaut",
            genre=["Classical"],
        ),
    }

    from salmon.tagger.pre_data import construct_artists_li

    assert construct_artists_li(tags) == [
        ("Ensemble Carminé", "main"),
        ("Claire Chevalier", "conductor"),
        ("Alfonso X el Sabio", "composer"),
        ("Guillaume de Machaut", "composer"),
    ]


def test_construct_track_artists_ignores_composer_outside_classical() -> None:
    track = SimpleNamespace(artist="Adele", composer=["Adele", "Paul Epworth"], genre=["Pop"])

    assert construct_track_artists(track) == [("Adele", "main")]


def test_construct_track_artists_ignores_conductor_outside_classical() -> None:
    track = SimpleNamespace(artist="Some Band", conductor="Some Band", composer=None, genre="Rock")

    assert construct_track_artists(track) == [("Some Band", "main")]


def test_construct_track_artists_without_genre_is_not_classical() -> None:
    track = SimpleNamespace(artist="Some Band", conductor="Maestro", composer="Writer")

    assert construct_track_artists(track) == [("Some Band", "main")]


def test_construct_track_artists_classical_list_genre_keeps_roles() -> None:
    track = SimpleNamespace(artist="Orchestra", conductor="Maestro", composer="Writer", genre=["Classical", "Baroque"])

    assert construct_track_artists(track) == [
        ("Orchestra", "main"),
        ("Maestro", "conductor"),
        ("Writer", "composer"),
    ]


def test_construct_artists_li_keeps_main_artist_named_in_composer_on_pop_album() -> None:
    from salmon.tagger.pre_data import construct_artists_li

    tags = {
        "01.flac": SimpleNamespace(artist="Adele", composer=["Adele", "Paul Epworth"], genre=["Pop"]),
        "02.flac": SimpleNamespace(artist="Adele", composer="Adele", genre=["Pop"]),
    }

    assert construct_artists_li(tags) == [("Adele", "main")]


def test_check_required_tags_flags_missing_composer_for_classical_release(capsys) -> None:
    tags = {
        "01. Example.flac": SimpleNamespace(
            title="Example Title",
            artist=["Ensemble Carminé"],
            album="Fragments Médiévaux",
            tracknumber="01",
            genre=["Classical"],
            composer=None,
        )
    }

    check_required_tags(tags)

    captured = capsys.readouterr()
    assert "composer" in captured.out
