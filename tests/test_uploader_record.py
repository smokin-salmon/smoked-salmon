from salmon.uploader import record


def test_a_seedbox_failure_noted_twice_is_kept_once_and_makes_the_record_failed() -> None:
    with record.recording() as run:
        assert not run.seedbox_failed
        record.note_seedbox_failure("box", "copy", "Album")
        record.note_seedbox_failure("box", "copy", "Album")
        record.note_seedbox_failure("box", "client add", "Album.torrent")

    assert run.seedbox_failures == [
        {"seedbox": "box", "step": "copy", "item": "Album"},
        {"seedbox": "box", "step": "client add", "item": "Album.torrent"},
    ]
    assert run.seedbox_failed


def test_outside_a_recording_nothing_is_kept() -> None:
    record.note_seedbox_failure("box", "copy", "Album")
