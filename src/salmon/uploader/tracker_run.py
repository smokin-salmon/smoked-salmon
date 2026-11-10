"""One tracker's turn in an upload run, and what the turns of a run share."""

from dataclasses import dataclass, field
from typing import Any

from salmon.uploader.seedbox import UploadManager
from salmon.uploader.spectrals import SpectralUploads


@dataclass(frozen=True, kw_only=True)
class ReleaseToUpload:
    """What the review settled, the same for every tracker of the run."""

    path: str
    metadata: dict[str, Any]
    rls_data: dict[str, Any]
    track_data: dict[str, Any]
    hybrid: bool
    searchstrs: list[str]
    source: str
    source_url: str | None
    conversion: dict[str, Any] | None
    conversions: tuple[str, ...] | None
    max_path_length: int | None
    source_flac: dict[str, Any] | None
    flac_url: str | None
    flac_group: dict[str, Any] | None
    given_cover_url: str | None
    remove_downloaded_cover_image: bool


@dataclass(kw_only=True)
class TrackerRun:
    """What one tracker's turn leaves for the next, changed in place: an abort mid-turn still finds what is up."""

    seedbox_uploader: UploadManager
    spectral_uploads: SpectralUploads
    request_id: int | str | None
    spectrals_after: bool
    lossy_master: bool
    lossy_comment: str | None
    spectral_urls: dict[int, list[str]] | None
    spectral_ids: dict[int, str] | None
    uploaded: list[str] = field(default_factory=list)  # The URL of each torrent uploaded, for an abort to list
    # Trackers whose upload failed or was skipped, or that are missing a conversion the run wanted: the folder is
    # needed to retry them. A first tracker refused before the loop is one of them.
    failed_trackers: list[str] = field(default_factory=list)
    cover_urls: dict[str, str | None] = field(default_factory=dict)  # Uploaded cover URL per image host
