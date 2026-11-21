from unittest.mock import AsyncMock

import anyio
import pytest

from salmon import dryrun
from salmon.webui import kinds, paths
from salmon.webui.reports import LossyReportParams, lossy_report


def test_report_job_prints_without_tracker_requests(capsys):
    async def scenario():
        with dryrun.mode(True):
            return await lossy_report(LossyReportParams(target="123", tracker="OPS", comment="Evidence", source_url=""))

    result = anyio.run(scenario)
    assert "Evidence" in result["text"]
    assert "Evidence" in capsys.readouterr().out
    assert kinds is not None


def test_report_job_uses_browser_confirmation(monkeypatch):
    from salmon import interaction

    asked = []

    async def decline(text, **kwargs):
        asked.append((text, kwargs))
        return False

    monkeypatch.setattr(interaction, "confirm", decline)
    result = anyio.run(
        lossy_report,
        LossyReportParams(target="123", tracker="RED", comment="Evidence", source_url=""),
    )
    assert "Evidence" in result["text"]
    assert asked == [("Verify this FLAC belongs to your authenticated account?", {"default": False})]


def test_optional_source_does_not_prompt_again(capsys):
    async def scenario():
        with dryrun.mode(True):
            return await lossy_report(LossyReportParams(target="123", tracker="OPS", comment="Evidence"))

    result = anyio.run(scenario)
    assert "Evidence" in result["text"]
    assert "Source URL (optional)" not in capsys.readouterr().out


def test_folder_job_previews_links_before_declined_submission(tmp_path, monkeypatch, capsys):
    from salmon import interaction, reports

    monkeypatch.setattr(paths, "roots", lambda: [paths.Root(str(tmp_path.parent), False)])
    (tmp_path / "song.flac").touch()
    client = AsyncMock()
    monkeypatch.setattr(reports.trackers, "get_class", lambda _: lambda: client)
    monkeypatch.setattr(reports, "verified_report_media", AsyncMock(return_value="WEB"))
    monkeypatch.setattr(reports, "gather_audio_info", lambda _: {})
    generate = AsyncMock(return_value={1: "song.flac"})
    monkeypatch.setattr(reports.spectrals, "generate_spectrals_all", generate)
    monkeypatch.setattr(reports.spectrals, "compress_chosen_spectrals", AsyncMock())
    monkeypatch.setattr(reports.spectrals, "upload_spectrals", AsyncMock(return_value={1: ["full", "zoom"]}))

    async def confirm(text, **kwargs):
        if text.startswith("Submit"):
            assert "[img=zoom]" in capsys.readouterr().out
            return False
        return True

    monkeypatch.setattr(interaction, "confirm", confirm)
    result = anyio.run(lossy_report, LossyReportParams("123", tracker="OPS", comment="Evidence", folder=str(tmp_path)))
    assert "[img=full]" in result["text"]
    assert generate.await_args is not None and generate.await_args.args[0] == str(tmp_path)
    client.report_lossy_master.assert_not_awaited()


def test_report_job_refuses_outside_folder(tmp_path, monkeypatch):
    from salmon.webui.jobs import KINDS

    monkeypatch.setattr(paths, "roots", lambda: [paths.Root(str(tmp_path / "downloads"), False)])
    params = LossyReportParams("123", folder=str(tmp_path))
    with pytest.raises(paths.PathRefused, match="outside download_directory and library_dirs"):
        KINDS["lossy_report"].check(params, True)
