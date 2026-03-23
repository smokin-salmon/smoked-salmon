import salmon.release_notification as release_notification


class _DummyResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_get_version_prefers_display_version_override(monkeypatch) -> None:
    monkeypatch.setenv("SALMON_DISPLAY_VERSION", "0.10.1-personal-fork.42")
    monkeypatch.setattr(release_notification, "_cached_version", None)

    assert release_notification.get_version() == "0.10.1-personal-fork.42"


def test_get_remote_personal_fork_version_data_picks_latest_release(monkeypatch) -> None:
    payload = [
        {
            "tag_name": "0.10.1-personal-fork.8",
            "published_at": "2026-03-23T22:41:07Z",
            "body": "fork 8",
        },
        {
            "tag_name": "0.10.1-personal-fork.10",
            "published_at": "2026-03-24T01:00:00Z",
            "body": "fork 10",
        },
        {
            "tag_name": "0.10.0.post20260310",
            "published_at": "2026-03-10T12:06:12Z",
            "body": "ignore me",
        },
    ]

    monkeypatch.setattr(
        release_notification.requests,
        "get",
        lambda *_args, **_kwargs: _DummyResponse(200, payload),
    )

    data = release_notification._get_remote_personal_fork_version_data("https://example.invalid/releases")

    assert data is not None
    assert data.current == "0.10.1-personal-fork.10"
    assert [entry.version for entry in data.changelog] == [
        "0.10.1-personal-fork.10",
        "0.10.1-personal-fork.8",
    ]


def test_show_release_notification_uses_personal_fork_feed(monkeypatch) -> None:
    messages: list[tuple[str, dict]] = []

    def fake_secho(message: str, **kwargs) -> None:
        messages.append((message, kwargs))

    monkeypatch.setattr(release_notification, "get_version", lambda: "0.10.1-personal-fork.8")
    monkeypatch.setattr(
        release_notification,
        "_get_remote_personal_fork_version_data",
        lambda _url: release_notification.VersionData(current="0.10.1-personal-fork.9", changelog=[]),
    )
    monkeypatch.setattr(release_notification.cfg.upload, "update_notification", True)
    monkeypatch.setattr(release_notification.cfg.upload, "update_notification_verbose", False)
    monkeypatch.setattr(release_notification.click, "secho", fake_secho)

    release_notification.show_release_notification()

    assert messages[0] == ("Local Version: 0.10.1-personal-fork.8", {"fg": "yellow"})
    assert messages[1] == ("[NOTICE] Update available: v0.10.1-personal-fork.9\n", {"fg": "green", "bold": True})
