import os
from pathlib import Path

from salmon import cfg
from salmon.tagger import foldername


def test_hardlink_fallback_survives_a_partial_tree(tmp_path, monkeypatch) -> None:
    # Regression for #356: when os.link fails partway through copytree, the plain-copy
    # fallback used to hit SameFileError on the files that were already hardlinked.
    source = tmp_path / "source_folder"
    source.mkdir()
    file_names = ["a.flac", "b.flac", "c.flac"]
    for i, name in enumerate(file_names):
        (source / name).write_bytes(f"content-{i}".encode())

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    monkeypatch.setattr(cfg.directory, "download_directory", str(download_dir))
    monkeypatch.setattr(cfg.directory, "hardlinks", True)
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", False)

    real_link = os.link
    calls = {"n": 0}

    def flaky_link(src: str, dst: str, *args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("simulated hardlink failure")
        real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", flaky_link)

    metadata = {
        "scene": True,
        "artists": [("Artist", "main")],
        "title": "Title",
        "year": 2024,
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
    }

    new_path = foldername.rename_folder(str(source), metadata, auto_rename=True, check=False)

    assert new_path == str(download_dir / source.name)
    for i, name in enumerate(file_names):
        assert (Path(new_path) / name).read_bytes() == f"content-{i}".encode()
