"""The verdict of each check in `salmon check all` (#541), on the results the checks give: no audio, no tracker."""

from salmon.checks import verdicts
from salmon.checks.integrity import IntegrityResult
from salmon.checks.source import DetectedSource


def _flac(rate: int, precision: int = 16) -> dict:
    return {"channels": 2, "sample rate": rate, "bit rate": 900_000, "precision": precision}


# Source


def test_an_undetectable_source_is_only_information():
    row = verdicts.source_row(None)
    assert row.verdict == "INFO"


def test_a_detected_source_says_why():
    row = verdicts.source_row(DetectedSource("WEB", "Qobuz URL in the tags"))
    assert (row.verdict, row.detail) == ("OK", "WEB: Qobuz URL in the tags.")


# Integrity


def test_a_file_that_does_not_decode_blocks():
    result = IntegrityResult(False, "/music/Album/02.flac: ERROR while decoding data", decode_failures=("02.flac",))
    row = verdicts.integrity_row(result)
    assert row.verdict == "BLOCK"
    assert "02.flac" in row.detail
    assert row.notes == ("/music/Album/02.flac: ERROR while decoding data",)


def test_a_missing_md5_warns():
    row = verdicts.integrity_row(IntegrityResult(False, md5_unset=("01.flac",), checked=2))
    assert row.verdict == "WARN"
    assert "1 of 2 file(s) have no MD5 signature stored" in row.detail


def test_an_mp3_with_damage_that_still_decodes_warns():
    row = verdicts.integrity_row(IntegrityResult(False, "01.mp3 (offset 0x10): MPEG stream error", checked=1))
    assert row.verdict == "WARN"
    assert row.notes == ("01.mp3 (offset 0x10): MPEG stream error",)


def test_checker_notes_on_files_that_pass_warn():
    row = verdicts.integrity_row(IntegrityResult(True, concerns=("01.mp3: No supported tags in the file",), checked=1))
    assert row.verdict == "WARN"


def test_clean_files_pass():
    assert verdicts.integrity_row(IntegrityResult(True, checked=3)).verdict == "OK"


# MQA


def test_mqa_blocks():
    row = verdicts.mqa_row([{"file": "01.flac", "detected": False}, {"file": "02.flac", "detected": True}])
    assert row.verdict == "BLOCK"
    assert "02.flac" in row.detail and "RED and OPS" in row.detail


def test_no_mqa_passes_and_no_flac_is_information():
    assert verdicts.mqa_row([{"file": "01.flac", "detected": False}]).verdict == "OK"
    assert verdicts.mqa_row([]).verdict == "INFO"


# Upconvert


def test_a_16bit_album_is_out_of_the_upconvert_checks_scope_not_a_failure():
    row = verdicts.upconvert_row([{"file": "01.flac", "not_applicable": "This is a 16bit FLAC file."}])
    assert row.verdict == "INFO"
    assert "out of scope" in row.detail


def test_an_upconverted_24bit_file_blocks():
    row = verdicts.upconvert_row([{"file": "01.flac", "upconverted": True, "wasted_bits": 8, "bitdepth": 24}])
    assert row.verdict == "BLOCK"
    assert row.notes == ("01.flac: 8 of 24 bits wasted",)


def test_a_24bit_file_that_could_not_be_checked_warns():
    row = verdicts.upconvert_row([{"file": "01.flac", "error": "flac binary not found"}])
    assert row.verdict == "WARN"


def test_genuine_24bit_files_pass():
    row = verdicts.upconvert_row(
        [
            {"file": "01.flac", "upconverted": False, "wasted_bits": 0, "bitdepth": 24},
            {"file": "02.flac", "not_applicable": "This is a 16bit FLAC file."},
        ]
    )
    assert row.verdict == "OK"


# Rip logs


def _log(**fields) -> dict:
    return {"file": "rip.log", "score": 100, "checksum": "Match", "crcs": "match", **fields}


def test_a_cd_with_no_log_warns_without_claiming_a_score_or_a_trump():
    (row,) = verdicts.log_rows("CD", [])
    assert row.verdict == "WARN"
    assert row.detail == "A CD with no rip log."


def test_an_edited_log_blocks():
    (row,) = verdicts.log_rows("CD", [_log(checksum="Mismatch", score=100)])
    assert row.verdict == "BLOCK"
    assert "edited" in row.detail


def test_a_score_under_100_warns_with_the_score():
    (row,) = verdicts.log_rows(None, [_log(score=95)])
    assert row.verdict == "WARN"
    assert "score 95/100" in row.detail


def test_a_log_without_a_checksum_warns():
    (row,) = verdicts.log_rows("CD", [_log(checksum="Unknown")])
    assert row.verdict == "WARN"
    assert "no checksum" in row.detail and "1.0 beta 3" in row.detail


def test_audio_that_does_not_match_the_logs_crcs_warns():
    (row,) = verdicts.log_rows("CD", [_log(crcs="mismatch")])
    assert row.verdict == "WARN"
    assert "does not match its CRCs" in row.detail


def test_a_log_that_cannot_be_read_warns():
    (row,) = verdicts.log_rows("CD", [{"file": "rip.log", "error": "Could not parse rip.log"}])
    assert row.verdict == "WARN"


def test_a_perfect_log_passes_and_each_log_has_its_row():
    rows = verdicts.log_rows("CD", [_log(file="CD1.log"), _log(file="CD2.log", score=90)])
    assert [row.verdict for row in rows] == ["OK", "WARN"]


def test_logs_do_not_apply_to_another_source():
    (row,) = verdicts.log_rows("WEB", [])
    assert row.verdict == "INFO"


# Tags, sample rate, 16bit above 48 kHz, path length


def test_tag_issues_warn():
    assert verdicts.tag_issues_row(["01.flac: FLAC file contains an ID3 tag"]).verdict == "WARN"
    assert verdicts.tag_issues_row([]).verdict == "OK"


def test_48khz_passes_and_32khz_may_be_rejected():
    assert verdicts.sample_rate_row({"01.flac": _flac(48000)}).verdict == "OK"
    row = verdicts.sample_rate_row({"01.flac": _flac(44100), "02.flac": _flac(32000)})
    assert row.verdict == "WARN"
    assert "32 kHz" in row.detail and "may be rejected" in row.detail
    assert row.notes == ("02.flac",)


def test_16bit_96khz_blocks_for_ops_and_warns_for_red():
    rows = verdicts.sixteen_bit_rows({"01.flac": _flac(96000)}, {"RED": "trumpable", "OPS": "refused"})
    assert [(row.check, row.verdict) for row in rows] == [
        ("16bit above 48 kHz (RED)", "WARN"),
        ("16bit above 48 kHz (OPS)", "BLOCK"),
    ]


def test_24bit_96khz_and_16bit_48khz_are_not_16bit_above_48khz():
    audio = {"01.flac": _flac(96000, precision=24), "02.flac": _flac(48000)}
    (row,) = verdicts.sixteen_bit_rows(audio, {"RED": "trumpable", "OPS": "refused"})
    assert row.verdict == "OK"


def test_16bit_above_48khz_on_a_tracker_with_no_known_rule_is_information():
    (row,) = verdicts.sixteen_bit_rows({"01.flac": _flac(88200)}, {"DIC": ""})
    assert row.verdict == "INFO"


def test_a_path_over_reds_limit_but_within_ops_warns_for_red_only():
    paths = ["Album/" + "a" * 190 + ".flac", "Album/short.flac"]
    rows = verdicts.path_rows(paths, {"RED": 180, "OPS": 255})
    assert [(row.check, row.verdict) for row in rows] == [("Path length (RED)", "WARN"), ("Path length (OPS)", "OK")]


def test_paths_within_every_limit_give_one_row():
    (row,) = verdicts.path_rows(["Album/01.flac"], {"RED": 180, "OPS": 255})
    assert row.verdict == "OK"
    assert "13 characters" in row.detail


# Provenance and frequency analysis


def test_each_provenance_contradiction_is_listed_in_a_warning():
    contradictions = [
        "01.flac: comment claims 24bit, the audio is 16bit",
        "02.flac: comment claims 24bit, the audio is 16bit",
    ]
    row = verdicts.provenance_row({"files": [{}], "vendors": [], "contradictions": contradictions})
    assert row.verdict == "WARN"
    assert row.notes == tuple(contradictions)


def test_no_contradiction_names_the_encoder():
    row = verdicts.provenance_row({"files": [{}], "vendors": ["reference libFLAC 1.4.3"], "contradictions": []})
    assert row.verdict == "OK"
    assert "libFLAC" in row.detail


def test_lossy_marks_warn_with_the_track_lines():
    row = verdicts.frequency_row("suspect", ["01.flac: brick-wall at 16.0 kHz", "Every track carries the marks"])
    assert row.verdict == "WARN"
    assert row.notes == ("01.flac: brick-wall at 16.0 kHz", "Every track carries the marks")
    assert verdicts.frequency_row("look", []).verdict == "WARN"
    assert verdicts.frequency_row("ok", ["Nothing here"]).verdict == "OK"
    assert verdicts.frequency_row("none", []).verdict == "INFO"
    assert verdicts.frequency_row(None, []).verdict == "INFO"


# Trackers


def test_a_do_not_upload_match_blocks_for_that_tracker():
    row = verdicts.do_not_upload_row("RED", "Artist (the whole discography) is on RED's Do-Not-Upload list")
    assert (row.check, row.verdict) == ("Do-Not-Upload (RED)", "BLOCK")
    assert verdicts.do_not_upload_row("OPS", None).verdict == "OK"


RELEASE = {"source": "WEB", "format": "FLAC", "encoding": "Lossless", "year": "2020", "edition_title": None}


def _group(*torrents: dict) -> dict:
    return {"groupId": 7, "groupName": "Album", "artist": "Artist", "groupYear": 2020, "torrents": list(torrents)}


def _torrent(**fields) -> dict:
    return {"media": "WEB", "format": "FLAC", "encoding": "Lossless", "remasterYear": 2020, **fields}


def test_a_group_holding_this_edition_warns_naming_the_torrent():
    row = verdicts.dupe_row("RED", RELEASE, [_group(_torrent(), _torrent(media="CD"))])
    assert row.verdict == "WARN"
    assert row.notes == ("Artist - Album (2020): 2020 / WEB / FLAC / Lossless",)


def test_a_group_with_another_edition_only_passes_and_names_the_group():
    row = verdicts.dupe_row("RED", RELEASE, [_group(_torrent(remasterYear=2015))])
    assert row.verdict == "OK"
    assert row.notes == ("Artist - Album (2020)",)


def test_groups_found_for_a_release_of_unknown_source_warn():
    row = verdicts.dupe_row("OPS", {**RELEASE, "source": None}, [_group(_torrent())])
    assert row.verdict == "WARN"
    assert "by hand" in row.detail


def test_no_group_found_passes():
    assert verdicts.dupe_row("OPS", RELEASE, []).verdict == "OK"
