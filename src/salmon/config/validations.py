import os
from typing import Annotated, Literal

import msgspec


class BaseStruct(msgspec.Struct, forbid_unknown_fields=False):
    pass


class Directory(BaseStruct):
    dottorrents_dir: str
    download_directory: str
    hardlinks: bool = True
    tmp_dir: str | None = None
    clean_tmp_dir: bool = False

    def __post_init__(self):
        if not os.path.isdir(self.dottorrents_dir):
            raise ValueError("dottorrents_dir is not a valid directory")
        if not os.path.isdir(self.download_directory):
            raise ValueError("download_directory is not a valid directory")
        if self.tmp_dir and not os.path.isdir(self.tmp_dir):
            raise ValueError("tmp_dir is not a valid directory")


ImgUploaderLiteral = Literal["ptscreens", "oeimg", "catbox", "imgbb", "imgbox", "red"]
SpectralSelectionLiteral = Literal["*", "+", "0"]

_TRACKER_CODES = ("red", "ops", "dic")
# Image hosts run by a tracker, mapped to the trackers whose pages can display their
# images: RED's host serves only logged-in RED members, and OPS accepts RED-hosted images.
# Each is valid solely as a cover host for those trackers.
_TRACKER_ONLY_HOSTS = {"red": ("red", "ops")}


class TrackerImageSettings(BaseStruct):
    """Per-tracker image settings, overriding [image] for uploads to that tracker."""

    cover_uploader: ImgUploaderLiteral | None = None


class ImageUploader(BaseStruct):
    image_uploader: ImgUploaderLiteral = "catbox"
    cover_uploader: ImgUploaderLiteral = "catbox"
    specs_uploader: ImgUploaderLiteral = "catbox"
    ptscreens_key: str | None = None
    oeimg_key: str | None = None
    imgbb_key: str | None = None
    remove_auto_downloaded_cover_image: bool = False
    auto_compress_cover: bool = False
    default_spectral_ids: SpectralSelectionLiteral | None = None
    red: TrackerImageSettings | None = None
    ops: TrackerImageSettings | None = None
    dic: TrackerImageSettings | None = None

    def cover_uploader_for(self, site_code: str) -> str:
        """Get the cover host for a tracker: its [image.<tracker>] override, else cover_uploader."""
        override = getattr(self, site_code.lower(), None)
        if override is not None and override.cover_uploader is not None:
            return override.cover_uploader
        return self.cover_uploader

    def _selections(self) -> list[tuple[str, str]]:
        """Get every configured (setting name, host) pair, per-tracker overrides included."""
        selections = [
            ("image_uploader", self.image_uploader),
            ("cover_uploader", self.cover_uploader),
            ("specs_uploader", self.specs_uploader),
        ]
        for code in _TRACKER_CODES:
            override = getattr(self, code)
            if override is not None and override.cover_uploader is not None:
                selections.append((f"{code}.cover_uploader", override.cover_uploader))
        return selections

    def __post_init__(self):
        selections = self._selections()
        uploader_selections = {host for _, host in selections}
        if "ptscreens" in uploader_selections and self.ptscreens_key is None:
            raise ValueError("PTScreens key not specified")
        if "oeimg" in uploader_selections and self.oeimg_key is None:
            raise ValueError("oeimage key not specified")
        if "imgbb" in uploader_selections and self.imgbb_key is None:
            raise ValueError("imgbb key not specified")
        # Covers, description images and spectrals set in [image] are shared by every
        # tracker, and RED also forbids spectrals on its host.
        for setting, host in selections:
            trackers = _TRACKER_ONLY_HOSTS.get(host)
            if trackers is not None and setting not in {f"{code}.cover_uploader" for code in trackers}:
                sections = " or ".join(f"[image.{code}]" for code in trackers)
                raise ValueError(
                    f'image.{setting} = "{host}": the {host} image host only displays on '
                    f"{'/'.join(code.upper() for code in trackers)}, so it can only be set as "
                    f"cover_uploader under {sections}"
                )


class TidalSettings(BaseStruct):
    client_id: str | None = None
    client_secret: str | None = None
    regions: list[str] = msgspec.field(default_factory=lambda: ["de", "nz", "us", "gb"])


# TODO: Add validations here
class QobuzSettings(BaseStruct):
    app_id: str | None = None
    user_auth_token: str | None = None
    no_genres_from_qobuz: bool = False


class AppleMusicSettings(BaseStruct):
    storefronts: list[str] = msgspec.field(default_factory=lambda: ["us:en-US", "jp:ja", "cn:zh-Hans-CN"])


class BeatportSettings(BaseStruct):
    username: str | None = None
    password: str | None = None


class Metadata(BaseStruct):
    discogs_token: str | None = None
    qobuz: QobuzSettings = msgspec.field(default_factory=QobuzSettings)
    tidal: TidalSettings = msgspec.field(default_factory=TidalSettings)
    apple_music: AppleMusicSettings = msgspec.field(default_factory=AppleMusicSettings)
    beatport: BeatportSettings = msgspec.field(default_factory=BeatportSettings)


class GazelleTrackerSettings(BaseStruct):
    session: str
    api_key: str | None = None
    # TODO: validate this
    dottorrents_dir: str | None = None


class Tracker(BaseStruct):
    red: GazelleTrackerSettings | None = None
    ops: GazelleTrackerSettings | None = None
    dic: GazelleTrackerSettings | None = None
    default_tracker: Literal["RED", "OPS", "DIC"] | None = None

    def __post_init__(self):
        if (self.red is None) and (self.ops is None) and (self.dic is None):
            raise ValueError("You need a tracker session cookie in your config!")

        if self.ops is None and self.default_tracker == "OPS":
            raise ValueError("Default tracker is invalid!")
        if self.red is None and self.default_tracker == "RED":
            raise ValueError("Default tracker is invalid!")
        if self.dic is None and self.default_tracker == "DIC":
            raise ValueError("Default tracker is invalid!")


class Seedbox(BaseStruct):
    name: str = ""
    enabled: bool = False
    url: str = ""  # Name of remote in rclone
    type: Literal["local", "rclone"] = "local"
    directory: str = ""  # Directory when adding torrent to download client
    flac_only: bool = False  # if true, only upload FLAC files
    extra_args: list[str] = msgspec.field(default_factory=list)  # pass these arguments to rclone
    torrent_client: str = ""
    label: str = ""  # Label to apply to torrents in download client
    add_paused: bool = False  # If true, add torrents to client in paused state

    def __post_init__(self):
        if self.type not in ("local", "rclone"):
            raise ValueError("Invalid seedbox type specified")


class UploadSearch(BaseStruct):
    limit: int = 3
    # TODO: are these reasonable defaults?
    excluded_labels: list[str] = msgspec.field(default_factory=lambda: ["edm comps"])
    blacklisted_genres: list[str] = msgspec.field(default_factory=lambda: ["Soundtrack", "Asian Music"])


class UploadFormatting(BaseStruct):
    folder_template: str = "{artists} - {title} ({year}) [{source} {format}]"
    file_template: str = "{tracknumber}. {artist} - {title}"
    remove_source_dir: bool = False

    # formatting options
    no_artist_in_filename_if_only_one_album_artist: bool = True
    one_album_artist_file_template: str = "{tracknumber}. {title}"
    lowercase_cover: bool = True
    various_artist_threshold: int = 4
    blacklisted_substitution: str = "_"
    guests_in_track_title: bool = False
    various_artist_word: str = "Various"
    strip_useless_versions: bool = True
    add_edition_title_to_album_tag: bool = True


class UploadDescription(BaseStruct):
    bitrates_in_t_desc: bool = False
    include_tracklist_in_t_desc: bool = False
    copy_uploaded_url_to_clipboard: bool = False
    # TODO: should this be in description?
    review_as_comment_tag: bool = True
    icons_in_descriptions: bool = True
    # TODO: should this be in description?
    fullwidth_replacements: bool = False
    # TODO: should this be in description?
    empty_track_comment_tag: bool = True


class UploadWebInterface(BaseStruct):
    host: str = "0.0.0.0"
    port: int = 55110
    display_host: str = "localhost"
    static_root_url: str = "/static"

    def __post_init__(self):
        if self.port < 1 or self.port > 65535:
            raise ValueError("Port number is invalid")

    @property
    def effective_host(self) -> str:
        """Host used for generating user-facing URLs.

        Falls back to ``host`` when ``display_host`` is not set.
        """
        return self.display_host if self.display_host else self.host


class UploadRequests(BaseStruct):
    always_ask_for_request_fill: bool = False
    check_recent_uploads: bool = True
    check_requests: bool = True
    last_minute_dupe_check: bool = False


class UploadCompression(BaseStruct):
    flac_compression_level: Annotated[int, msgspec.Meta(ge=0, le=8)] = 8
    compress_spectrals: bool = True
    # TODO: this probably should be in description
    lma_comment_in_t_desc: bool = False
    use_upc_as_catno: bool = True


class UploadAiReview(BaseStruct):
    enabled: bool = False
    api_key: str | None = None
    base_url: str | None = None
    model: str = "gpt-5.4-2026-03-05"
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] = "medium"
    use_web_search: bool = True
    timeout_seconds: Annotated[int, msgspec.Meta(ge=5, le=1800)] = 45
    background: bool = False

    def __post_init__(self):
        if self.enabled and not self.api_key:
            raise ValueError("upload.ai_review.api_key must be set when AI review is enabled")


class Upload(BaseStruct):
    simultaneous_threads: int = 3
    user_agent: str = "salmon uploading tools"

    # Default text editor for click.edit operations
    # Can be "nano", "vim", "emacs", or any command available in PATH
    default_editor: str | None = None

    native_spectrals_viewer: bool = False
    feh_fullscreen: bool = True
    prompt_puddletag: bool = False
    # must be within 0-1
    log_dupe_tolerance: Annotated[float, msgspec.Meta(ge=0.0, le=1.0)] = 0.5
    windows_use_recycle_bin: bool = True

    multi_tracker_upload: bool = True
    # TODO: should this be in tracker?
    debug_tracker_connection: bool = False

    update_notification: bool = True
    update_notification_verbose: bool = True

    yes_all: bool = False

    upload_to_seedbox: bool = True

    # TODO: take these out of the upload struct!
    search: UploadSearch = msgspec.field(default_factory=UploadSearch)
    formatting: UploadFormatting = msgspec.field(default_factory=UploadFormatting)
    description: UploadDescription = msgspec.field(default_factory=UploadDescription)
    web_interface: UploadWebInterface = msgspec.field(default_factory=UploadWebInterface)
    requests: UploadRequests = msgspec.field(default_factory=UploadRequests)
    compression: UploadCompression = msgspec.field(default_factory=UploadCompression)
    ai_review: UploadAiReview = msgspec.field(default_factory=UploadAiReview)


class Cfg(BaseStruct):
    "This class defines the schema that msgspec uses to parse the config"

    directory: Directory
    metadata: Metadata = msgspec.field(default_factory=Metadata)
    image: ImageUploader = msgspec.field(default_factory=ImageUploader)
    tracker: Tracker = msgspec.field(default_factory=Tracker)
    seedbox: list[Seedbox] = msgspec.field(default_factory=list)
    upload: Upload = msgspec.field(default_factory=Upload)

    def __post_init__(self):
        # Uploads to RED's image host authenticate with the RED API key, whichever tracker the cover is for.
        red_host_users = [code for code in ("RED", "OPS", "DIC") if self.image.cover_uploader_for(code) == "red"]
        if red_host_users and not (self.tracker.red and self.tracker.red.api_key):
            raise ValueError(
                f'image.{red_host_users[0].lower()}.cover_uploader = "red" needs tracker.red.api_key to be set'
            )
