"""Stubbing what an upload run calls, in every module of the uploader that calls it."""

import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import salmon.uploader
from salmon.uploader import conversion_upload, cover_upload, metadata_review, spectrals, tracker_run, upload_checks

# Not `from salmon.uploader import upload`: that is the function salmon.uploader defines, which hides the module.
upload = importlib.import_module("salmon.uploader.upload")

# The names the code moved out of salmon.uploader calls, by the modules that call them: a fake replaces each in all
# of them, as it did when they were all salmon.uploader's. Any other name is called from salmon.uploader only; a
# name salmon.uploader no longer holds fails to patch there, rather than missing the code that calls it.
CALLED_FROM = {
    "check_folder_structure": (metadata_review, conversion_upload),
    "conversion_of": (salmon.uploader, conversion_upload),
    "gather_audio_info": (salmon.uploader, metadata_review, conversion_upload),
    "upload_and_report": (tracker_run, conversion_upload),
    "check_requests": (tracker_run,),
    "resolve_cover_url": (tracker_run,),
    "print_torrents": (tracker_run,),
    "post_upload_spectral_check": (tracker_run,),
    "execute_downconversion_tasks": (tracker_run,),
    "convert_folder": (conversion_upload,),
    "downconversion_format": (conversion_upload,),
    "generate_conversion_description": (conversion_upload,),
    "generate_transcode_description": (conversion_upload,),
    "transcode_folder": (conversion_upload,),
    "check_existing_group": (salmon.uploader, metadata_review),
    "download_cover_if_nonexistent": (salmon.uploader, cover_upload),
    "generate_dupe_check_searchstrs": (salmon.uploader, metadata_review),
    "check_log_cambia": (upload_checks,),
    "check_tags": (metadata_review,),
    "gather_tags": (salmon.uploader, metadata_review),
    "rename_files": (metadata_review,),
    "rename_folder": (metadata_review,),
    "review_metadata_with_ai": (metadata_review,),
    "tag_files": (metadata_review,),
    "prepare_and_upload": (upload,),
    "report_lossy_master": (upload,),
}

# Taken at import, before any test runs: a test may set spectrals.check_spectrals to a fake before stub_uploader.
_REAL_CHECK_SPECTRALS = spectrals.check_spectrals
_REAL_SPECTRALS_IN_BACKGROUND = spectrals.spectrals_in_background


def stub_uploader(monkeypatch, fakes: dict[str, Any]) -> None:
    """Replace each name with its fake wherever the upload run calls it.

    The question for notes on the torrent description is answered "no" unless a fake says otherwise: only
    test_uploader_torrent_notes asks it, and the other runs' answers do not make room for it.

    A fake check_spectrals also makes the run start no spectrals in the background, unless a fake
    spectrals_in_background is given: the real check_spectrals waits for them before it touches the spectrals folder.
    A fake does not, and sox, where it is installed, went on writing into the folder the fake replaced (#1199). A
    wrapper of the real check_spectrals is a fake here: a test of it with the background spectrals gives both.
    """

    async def no_notes() -> None:
        return None

    if "check_spectrals" in fakes:
        real = fakes["check_spectrals"] is _REAL_CHECK_SPECTRALS
        # The real one again with the real check_spectrals: a harness may stub both, then a test the real one.
        generation = _REAL_SPECTRALS_IN_BACKGROUND if real else _no_spectrals_in_background
        fakes = {"spectrals_in_background": generation, **fakes}
    for name, fake in {"ask_torrent_notes": no_notes, **fakes}.items():
        for module in CALLED_FROM.get(name, (salmon.uploader,)):
            monkeypatch.setattr(module, name, fake)


@asynccontextmanager
async def _no_spectrals_in_background(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
    """Stands in for spectrals_in_background: none are made, as for a format that has no spectrals."""
    yield None
