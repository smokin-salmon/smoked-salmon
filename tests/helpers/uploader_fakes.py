"""Stubbing what an upload run calls, in every module of the uploader that calls it."""

import importlib
from typing import Any

import salmon.uploader
from salmon.uploader import conversion_upload

# Not `from salmon.uploader import upload`: that is the function salmon.uploader defines, which hides the module.
upload = importlib.import_module("salmon.uploader.upload")

# The names the code moved out of salmon.uploader calls, by the modules that call them: a fake replaces each in all
# of them, as it did when they were all salmon.uploader's. Any other name is called from salmon.uploader only; a
# name salmon.uploader no longer holds fails to patch there, rather than missing the code that calls it.
CALLED_FROM = {
    "check_folder_structure": (salmon.uploader, conversion_upload),
    "conversion_of": (salmon.uploader, conversion_upload),
    "gather_audio_info": (salmon.uploader, conversion_upload),
    "upload_and_report": (salmon.uploader, conversion_upload),
    "convert_folder": (conversion_upload,),
    "downconversion_format": (conversion_upload,),
    "generate_conversion_description": (conversion_upload,),
    "generate_transcode_description": (conversion_upload,),
    "transcode_folder": (conversion_upload,),
    "prepare_and_upload": (upload,),
    "report_lossy_master": (upload,),
}


def stub_uploader(monkeypatch, fakes: dict[str, Any]) -> None:
    """Replace each name with its fake wherever the upload run calls it."""
    for name, fake in fakes.items():
        for module in CALLED_FROM.get(name, (salmon.uploader,)):
            monkeypatch.setattr(module, name, fake)
