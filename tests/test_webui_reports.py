from unittest.mock import AsyncMock

import anyio

from salmon import dryrun
from salmon.webui import kinds
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
        LossyReportParams(target="123", tracker="RED", report_type="web", comment="Evidence", source_url=""),
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

    (tmp_path / "song.flac").touch()
    client = AsyncMock()
    monkeypatch.setattr(reports.trackers, "get_class", lambda _: lambda: client)
    monkeypatch.setattr(reports, "related_report_targets", AsyncMock(return_value=[123]))
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


def test_uncertain_report_keeps_batch_recovery_details():
    import asyncclick as click

    from salmon.errors import UnknownOutcomeError
    from salmon.reports import submit_reports
    from salmon.trackers.red import RedApi
    from salmon.webui.egress import Redactor
    from salmon.webui.jobs import KINDS, Job, JobManager

    class Client(RedApi):
        async def report_lossy_master(self, torrent_id: int, comment: str, source: str) -> bool:
            if torrent_id == 124:
                raise UnknownOutcomeError("Connection closed")
            assert torrent_id == 123, "The batch must stop after the uncertain report"
            return True

    async def scenario():
        params = LossyReportParams(target="123")
        job = Job(KINDS["lossy_report"], params, {}, "Report", None, False, False)
        manager = JobManager(1, Redactor([]))
        try:
            await submit_reports(Client(), [123, 124, 125], "Evidence", "WEB")
        except (click.ClickException, UnknownOutcomeError) as error:
            status, message, result = manager._outcome(job, error)
        else:
            raise AssertionError("Expected an uncertain submission")
        assert status == "unknown_outcome"
        assert result is None
        assert message is not None
        assert "124 may have been submitted" in message
        assert "Already reported: 123" in message
        assert "Not attempted: 125" in message
        assert "Do not rerun the batch" in message

    anyio.run(scenario)
