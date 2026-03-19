from pathlib import Path

import anyio

import salmon.tagger.foldername as foldername
import salmon.uploader as uploader
import salmon.uploader.spectrals as uploader_spectrals
from salmon import cfg


def test_prompt_downconversion_choice_all_flag_returns_all(monkeypatch) -> None:
    rls_data = {"encoding": "24bit Lossless"}
    track_data = {"01.flac": {"sample rate": 48000}}

    async def fail_prompt(*_args, **_kwargs):
        raise AssertionError("click.prompt should not run when all downconversions are selected")

    monkeypatch.setattr(uploader.click, "prompt", fail_prompt)

    result = anyio.run(uploader.prompt_downconversion_choice, rls_data, track_data, True)

    assert [task["name"] for task in result] == ["16bit 48.0 kHz", "MP3 320", "MP3 V0"]


def test_check_spectrals_all_spectrals_skips_prompt(monkeypatch) -> None:
    audio_info = {"01.flac": {"duration": 100}, "02.flac": {"duration": 120}}

    async def fake_generate_spectrals_all(*_args, **_kwargs):
        return {1: "01.flac", 2: "02.flac"}

    async def fake_view_spectrals(*_args, **_kwargs):
        return None

    async def fake_prompt_lossy_master(*_args, **_kwargs):
        return False

    async def fail_prompt(*_args, **_kwargs):
        raise AssertionError("click.prompt should not run when all spectrals are preselected")

    monkeypatch.setattr(uploader_spectrals, "create_specs_folder", lambda _path: "/tmp/specs")
    monkeypatch.setattr(uploader_spectrals, "generate_spectrals_all", fake_generate_spectrals_all)
    monkeypatch.setattr(uploader_spectrals, "view_spectrals", fake_view_spectrals)
    monkeypatch.setattr(uploader_spectrals, "prompt_lossy_master", fake_prompt_lossy_master)
    monkeypatch.setattr(uploader_spectrals.click, "prompt", fail_prompt)

    async def run_check():
        return await uploader_spectrals.check_spectrals(
            "/tmp/release",
            audio_info,
            all_spectrals=True,
        )

    lossy_master, spectral_ids = anyio.run(run_check)

    assert lossy_master is False
    assert spectral_ids == {1: "01.flac", 2: "02.flac"}


def test_rename_folder_auto_rename_skips_confirmation(monkeypatch, tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "track.flac").write_text("data")
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    original_download_directory = cfg.directory.download_directory
    original_tmp_dir = cfg.directory.tmp_dir
    original_hardlinks = cfg.directory.hardlinks
    original_remove_source_dir = cfg.upload.formatting.remove_source_dir

    def fail_confirm(*_args, **_kwargs):
        raise AssertionError("click.confirm should not run when auto rename is enabled")

    monkeypatch.setattr(foldername, "generate_folder_name", lambda _metadata: "Renamed Release")
    monkeypatch.setattr(foldername.click, "confirm", fail_confirm)

    try:
        cfg.directory.download_directory = str(download_dir)
        cfg.directory.tmp_dir = ""
        cfg.directory.hardlinks = False
        cfg.upload.formatting.remove_source_dir = False

        new_path = foldername.rename_folder(str(source_dir), {"scene": False}, auto_rename=True)
    finally:
        cfg.directory.download_directory = original_download_directory
        cfg.directory.tmp_dir = original_tmp_dir
        cfg.directory.hardlinks = original_hardlinks
        cfg.upload.formatting.remove_source_dir = original_remove_source_dir

    assert Path(new_path).name == "Renamed Release"
    assert Path(new_path).is_dir()


def test_edit_metadata_auto_upload_skips_confirmation(monkeypatch, tmp_path: Path) -> None:
    metadata = {
        "scene": False,
        "genres": ["Rock"],
    }
    tags = {}

    async def fake_review_metadata(current_metadata, _validator):
        return current_metadata

    async def fake_check_tags(_path):
        return tags

    async def fake_check_folder_structure(*_args, **_kwargs):
        return None

    def fail_confirm(*_args, **_kwargs):
        raise AssertionError("click.confirm should not run when auto upload is enabled")

    monkeypatch.setattr(uploader, "review_metadata", fake_review_metadata)
    monkeypatch.setattr(uploader, "tag_files", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(uploader, "check_tags", fake_check_tags)
    monkeypatch.setattr(uploader, "rename_folder", lambda path, *_args, **_kwargs: path)
    monkeypatch.setattr(uploader, "rename_files", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(uploader, "check_folder_structure", fake_check_folder_structure)
    monkeypatch.setattr(uploader, "gather_tags", lambda _path: tags)
    monkeypatch.setattr(uploader, "gather_audio_info", lambda _path: {})
    monkeypatch.setattr(uploader.click, "confirm", fail_confirm)

    async def run_edit():
        return await uploader.edit_metadata(
            str(tmp_path),
            tags,
            metadata,
            "WEB",
            {},
            False,
            False,
            None,
            True,
            True,
        )

    _path, updated_metadata, _tags, _audio_info = anyio.run(run_edit)

    assert updated_metadata["tags"] == "Rock"
