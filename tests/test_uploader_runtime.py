import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import salmon.uploader as uploader


def test_get_runtime_does_not_shadow_run_upload() -> None:
    before = uploader.run_upload

    assert inspect.iscoroutinefunction(before)

    uploader.get_runtime()

    assert uploader.run_upload is before
    assert inspect.iscoroutinefunction(uploader.run_upload)
