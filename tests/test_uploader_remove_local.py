"""remove_local_after_copy: salmon up removes a folder from download_directory only when every seedbox has it."""

from pathlib import Path

import anyio
import pytest

from salmon import dryrun
from salmon.config.validations import Seedbox
from salmon.uploader import seedbox as sb


def _rclone(name: str, **kwargs) -> Seedbox:
    kwargs.setdefault("remove_local_after_copy", True)
    return Seedbox(name=name, type="rclone", url=name, torrent_client=f"fake://{name}", **kwargs)


def _local(name: str = "home") -> Seedbox:
    return Seedbox(name=name, type="local", torrent_client=f"fake://{name}")


class _Run:
    """One `salmon up` seedbox phase on a fake rclone and fake torrent clients."""

    def __init__(self, monkeypatch, tmp_path: Path, seedboxes: list[Seedbox]) -> None:
        self.download_directory = tmp_path / "downloads"
        self.download_directory.mkdir()
        self.torrents = tmp_path / "torrents"
        self.torrents.mkdir()
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.copy_fails: set[str] = set()  # seedbox names whose rclone fails
        self.add_fails: set[tuple[str, str]] = set()  # (seedbox name, torrent file name) the client refuses
        self.unconfigured: set[str] = set()  # seedbox names whose client cannot be set up
        self.copied: list[tuple[str, str]] = []
        self.added: list[tuple[str, str]] = []

        monkeypatch.setattr(sb.cfg.directory, "download_directory", str(self.download_directory))
        monkeypatch.setattr(sb.cfg.directory, "library_dirs", [])
        monkeypatch.setattr(sb.cfg, "seedbox", seedboxes)
        monkeypatch.setattr(sb.click, "secho", self._secho)
        self.lines: list[str] = []

        def parse(url: str) -> object:
            if url.removeprefix("fake://") in self.unconfigured:
                raise RuntimeError("cannot log in")
            return object()

        monkeypatch.setattr(sb.TorrentClientGenerator, "parse_libtc_url", staticmethod(parse))

        async def rclone(seedbox: Seedbox, remote_folder: str, path: str) -> bool:
            self.copied.append((seedbox.name, path))
            return seedbox.name not in self.copy_fails

        async def add(client, shell_path, torrent_path, label, add_paused, secrets, name) -> bool:
            file_name = Path(torrent_path).name
            if (name, file_name) in self.add_fails:
                return False
            self.added.append((name, file_name))
            return True

        monkeypatch.setattr(sb, "_rclone_upload_folder", rclone)
        monkeypatch.setattr(sb, "_add_to_downloader", add)
        self.manager = sb.UploadManager()

    def _secho(self, message: str = "", **kwargs) -> None:
        self.lines.append(str(message))

    def release(self, name: str = "Artist - Album [WEB FLAC]", *, inside: bool = True) -> Path:
        parent = self.download_directory if inside else self.tmp_path / "elsewhere"
        folder = parent / name
        folder.mkdir(parents=True)
        (folder / "01. Track.flac").write_bytes(b"audio")
        return folder

    def queue(self, folder: Path, site_code: str = "RED", *, is_flac: bool = True, copy_folder: bool = True) -> Path:
        torrent = self.torrents / f"{folder.name} {site_code}.torrent"
        torrent.write_bytes(b"d4:infod4:name5:Albumee")
        if copy_folder:
            self.manager.add_upload_task(str(folder), task_type="folder", is_flac=is_flac, site_code=site_code)
        self.manager.add_upload_task(
            str(torrent), task_type="seed", is_flac=is_flac, folder=str(folder), site_code=site_code
        )
        return torrent

    def finish(self) -> str:
        """What salmon up does once the uploads are done: run the tasks, then remove what may go."""
        anyio.run(self.manager.execute_upload)
        self.manager.remove_local_copies()
        return "\n".join(self.lines)


def test_a_folder_is_removed_when_an_opted_in_seedbox_copied_it_and_added_everything(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    folder = run.release()
    run.queue(folder, "RED")
    run.queue(folder, "OPS")
    report = run.finish()
    assert not folder.exists()
    assert (
        f"Removed {folder} from download_directory: copied to and seeding on nas (remove_local_after_copy)." in report
    )
    assert run.download_directory.is_dir()


def test_a_folder_is_removed_when_a_second_seedbox_without_the_key_also_has_it(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas"), _rclone("vps", remove_local_after_copy=False)])
    folder = run.release()
    run.queue(folder)
    run.finish()
    assert not folder.exists()


def _kept(run: _Run, folder: Path, reason: str) -> None:
    report = run.finish()
    assert folder.is_dir()
    assert (folder / "01. Track.flac").read_bytes() == b"audio"
    assert f"remove_local_after_copy is on, but {folder} is kept: {reason}." in report


def test_a_folder_outside_download_directory_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    folder = run.release(inside=False)
    run.queue(folder)
    _kept(run, folder, "it is not a folder inside download_directory")


def test_download_directory_itself_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    (run.download_directory / "other album").mkdir()
    run.queue(run.download_directory)
    run.finish()
    assert (run.download_directory / "other album").is_dir()


def test_a_symlink_to_a_folder_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    real = run.release(inside=False)
    link = run.download_directory / "link"
    link.symlink_to(real, target_is_directory=True)
    run.queue(link)
    run.finish()
    assert real.is_dir()
    assert (real / "01. Track.flac").exists()


def test_a_protected_folder_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    folder = run.release()
    library = folder / "Library"
    library.mkdir()
    monkeypatch.setattr(sb.cfg.directory, "library_dirs", [str(library)])
    run.queue(folder)
    _kept(run, folder, "it is a protected folder")


def test_a_folder_in_a_library_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    folder = run.release()
    monkeypatch.setattr(sb.cfg.directory, "library_dirs", [str(run.download_directory)])
    run.queue(folder)
    _kept(run, folder, "it is a protected folder")


def test_a_folder_no_opted_in_seedbox_took_because_of_flac_only_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas", flac_only=True), _rclone("vps", remove_local_after_copy=False)])
    folder = run.release("Artist - Album [WEB MP3 320]")
    run.queue(folder, is_flac=False)
    _kept(run, folder, "no seedbox set to remove the local copy took it")
    assert [name for name, _ in run.copied] == ["vps"]


def test_a_folder_no_opted_in_seedbox_took_because_of_trackers_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas", trackers=["RED"]), _rclone("vps", remove_local_after_copy=False)])
    folder = run.release()
    run.queue(folder, "OPS")
    _kept(run, folder, "no seedbox set to remove the local copy took it")


def test_a_folder_whose_copy_failed_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    run.copy_fails = {"nas"}
    folder = run.release()
    run.queue(folder)
    _kept(run, folder, "the copy to nas failed")


def test_a_folder_whose_torrent_the_client_refused_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    folder = run.release()
    torrent = run.queue(folder)
    run.add_fails = {("nas", torrent.name)}
    _kept(run, folder, f"nas could not add {torrent.name}")


def test_a_folder_a_non_opted_in_seedbox_failed_to_copy_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas"), _rclone("vps", remove_local_after_copy=False)])
    run.copy_fails = {"vps"}
    folder = run.release()
    run.queue(folder)
    _kept(run, folder, "the copy to vps failed")


def test_a_folder_two_opted_in_seedboxes_took_is_kept_when_one_failed(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas"), _rclone("vps")])
    run.copy_fails = {"vps"}
    folder = run.release()
    run.queue(folder)
    _kept(run, folder, "the copy to vps failed")


def test_a_folder_a_local_seedbox_seeds_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas"), _local("home")])
    folder = run.release()
    run.queue(folder)
    _kept(run, folder, "home seeds it from download_directory")


def test_a_folder_with_a_torrent_added_on_no_seedbox_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas", trackers=["RED"])])
    folder = run.release()
    run.queue(folder, "RED")
    torrent = run.queue(folder, "OPS")  # no seedbox serves OPS: this torrent has no task at all
    _kept(run, folder, f"{torrent.name} seeds on no seedbox")


def test_a_folder_is_kept_when_a_seedbox_client_was_not_configured(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    run.unconfigured = {"vps"}
    sb.cfg.seedbox.append(_rclone("vps", remove_local_after_copy=False))
    run.manager = sb.UploadManager()  # set up again, now that the second seedbox's client fails
    folder = run.release()
    run.queue(folder)
    _kept(run, folder, "vps was not configured")


def test_a_folder_is_kept_when_the_run_stopped_before_its_tasks_ran(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    folder = run.release()
    run.queue(folder)
    run.manager.remove_local_copies()  # execute_upload never ran
    assert folder.is_dir()


def test_one_folder_is_removed_and_another_kept_in_the_same_run(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas", trackers=["RED"])])
    good = run.release("Good [WEB FLAC]")
    bad = run.release("Bad [WEB FLAC]")
    run.queue(good, "RED")
    run.queue(bad, "RED")
    run.queue(bad, "OPS")
    run.finish()
    assert not good.exists()
    assert bad.is_dir()


def test_a_dry_run_removes_nothing(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    folder = run.release()
    run.queue(folder)
    anyio.run(run.manager.execute_upload)
    with dryrun.mode():
        run.manager.remove_local_copies()
    assert folder.is_dir()


def test_without_the_key_nothing_is_removed(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas", remove_local_after_copy=False)])
    folder = run.release()
    run.queue(folder)
    report = run.finish()
    assert folder.is_dir()
    assert "remove_local_after_copy" not in report


def test_a_cross_upload_never_removes(monkeypatch, tmp_path) -> None:
    # cross-upload only calls execute_upload: removing is a separate step that only salmon up takes.
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    folder = run.release()
    run.queue(folder, copy_folder=False)
    anyio.run(run.manager.execute_upload)
    assert folder.is_dir()
    source = (Path(sb.__file__).parent.parent / "cross_upload.py").read_text()
    assert "remove_local_copies" not in source


def test_a_local_seedbox_with_the_key_is_refused() -> None:
    with pytest.raises(ValueError, match="seedbox 'home': remove_local_after_copy only applies to rclone seedboxes"):
        Seedbox(name="home", type="local", remove_local_after_copy=True)


def test_the_key_is_off_by_default() -> None:
    assert Seedbox(type="rclone").remove_local_after_copy is False
    assert Seedbox(type="local").remove_local_after_copy is False


def test_a_run_that_did_not_end_well_keeps_every_folder_and_says_why(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    folder = run.release()
    run.queue(folder)
    anyio.run(run.manager.execute_upload)
    run.manager.remove_local_copies("the upload to OPS failed or was skipped")
    assert folder.is_dir()
    assert (
        f"remove_local_after_copy is on, but {folder} is kept: the upload to OPS failed or was skipped."
        in "\n".join(run.lines)
    )


def test_a_removal_that_fails_part_way_does_not_say_the_folder_is_kept(monkeypatch, tmp_path) -> None:
    run = _Run(monkeypatch, tmp_path, [_rclone("nas")])
    folder = run.release()
    run.queue(folder)

    def half_removed(path: str) -> None:
        (Path(path) / "01. Track.flac").unlink()
        raise PermissionError("busy")

    monkeypatch.setattr(sb.shutil, "rmtree", half_removed)
    report = run.finish()
    assert f"{folder} could not be fully removed and may be incomplete: busy." in report
    assert "is kept" not in report
