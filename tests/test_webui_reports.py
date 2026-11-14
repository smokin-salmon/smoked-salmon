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
    assert asked == [("Look up this uploader's same-edition torrents for approval?", {"default": False})]


def test_optional_source_does_not_prompt_again(capsys):
    async def scenario():
        with dryrun.mode(True):
            return await lossy_report(LossyReportParams(target="123", tracker="OPS", comment="Evidence"))

    result = anyio.run(scenario)
    assert "Evidence" in result["text"]
    assert "Source URL (optional)" not in capsys.readouterr().out
