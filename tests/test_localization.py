import json
from pathlib import Path

import pytest

from salmon.localization import text


def test_default_english_is_unchanged(monkeypatch):
    monkeypatch.delenv("SALMON_LANGUAGE", raising=False)
    assert text("Tracker connections") == "Tracker connections"


def test_chinese_labels_and_unknown_metadata(monkeypatch):
    monkeypatch.setenv("SALMON_LANGUAGE", "zh-CN")
    assert text("Tracker connections") == "站点连接"
    assert text("My album / Artist 2026") == "My album / Artist 2026"


def test_interpolated_message_preserves_paths_and_error(monkeypatch):
    monkeypatch.setenv("SALMON_LANGUAGE", "zh-CN")
    original = "Could not fetch request 123 from RED: certificate has expired"
    translated = text(original)
    assert "求种" in translated
    assert "123" in translated and "RED" in translated
    assert "certificate has expired" in translated


def test_translation_catalog_preserves_all_format_tokens():
    import re

    path = Path(__file__).parents[1] / "src/salmon/data/zh-CN.json"
    for original, translated in json.loads(path.read_text(encoding="utf-8")).items():
        assert sorted(re.findall(r"ZXQ\d+QXZ", original)) == sorted(re.findall(r"ZXQ\d+QXZ", translated)), original


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_whitespace_and_terminal_colors_remain_intact(monkeypatch, locale):
    monkeypatch.setenv("SALMON_LANGUAGE", locale)
    assert text("\n  Tracker connections\n").startswith("\n  ")
    assert text("\n  Tracker connections\n").endswith("\n")


def test_task_title_translates_without_changing_upload_parameters(monkeypatch):
    from salmon.webui.kinds import UploadParams, _upload_title

    params = UploadParams(path="D:/Music/Artist - Album [FLAC]", source="WEB", trackers=["RED", "DIC"])
    monkeypatch.setenv("SALMON_LANGUAGE", "zh-CN")
    assert _upload_title(params) == "上传 至 RED, DIC: Artist - Album [FLAC]"
    assert params.trackers == ["RED", "DIC"]
    assert params.path == "D:/Music/Artist - Album [FLAC]"
    monkeypatch.setenv("SALMON_LANGUAGE", "en")
    assert _upload_title(params) == "Upload to RED, DIC: Artist - Album [FLAC]"


def test_terminal_check_rows_localize_only_the_display(monkeypatch, capsys):
    from salmon.checks import print_album_checks
    from salmon.checks.album import AlbumChecks
    from salmon.checks.verdicts import Row

    row = Row("OK", "Integrity", "All 3 file(s) decode.")
    checks = AlbumChecks("Artist - Album", [row], {}, {}, [])
    monkeypatch.setenv("SALMON_LANGUAGE", "zh-CN")
    print_album_checks(checks)
    shown = capsys.readouterr().out
    assert "通过" in shown and "完整性" in shown and "3" in shown
    assert row.verdict == "OK" and row.detail == "All 3 file(s) decode."


def test_runtime_and_browser_catalogs_match():
    root = Path(__file__).parents[1]
    assert (root / "src/salmon/data/zh-CN.json").read_bytes() == (root / "webui/src/lib/zh-CN.json").read_bytes()


def test_connection_and_path_errors_keep_machine_values(monkeypatch):
    from salmon.errors import TLSCertificateError
    from salmon.webui.paths import PathRefused

    monkeypatch.setenv("SALMON_LANGUAGE", "zh-CN")
    error = TLSCertificateError("example.invalid", "certificate has expired", "RED")
    assert "证书已过期" in str(error) and "example.invalid" in str(error)
    assert error.reason == "certificate has expired" and error.tracker == "RED"
    refused = PathRefused(422, "No audio file (FLAC, MP3, M4A) in this folder or its disc folders.")
    assert "没有音频文件" in refused.detail and refused.status == 422
