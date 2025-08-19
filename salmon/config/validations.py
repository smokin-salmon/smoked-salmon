import os
from typing import Annotated, Literal

import msgspec


class BaseStruct(msgspec.Struct, forbid_unknown_fields=False):
    pass


class Directory(BaseStruct):
    dottorrents_dir: str
    download_directory: str
    hardlinks: bool = True
    tmp_dir: str = None
    clean_tmp_dir: bool = False

    def __post_init__(self):
        if not os.path.isdir(self.dottorrents_dir):
            raise ValueError("dottorrents_dir is not a valid directory")
        if not os.path.isdir(self.download_directory):
            raise ValueError("download_directory is not a valid directory")
        if self.tmp_dir and not os.path.isdir(self.tmp_dir):
            raise ValueError("tmp_dir is not a valid directory")


ImgUploaderLiteral = Literal["ptpimg", "ptscreens", "oeimg", "catbox", "emp"]


class ImageUploader(BaseStruct):
    image_uploader: ImgUploaderLiteral = "catbox"
    cover_uploader: ImgUploaderLiteral = "catbox"
    specs_uploader: ImgUploaderLiteral = "catbox"
    ptpimg_key: str | None = None
    ptscreens_key: str | None = None
    oeimg_key: str | None = None
    remove_auto_downloaded_cover_image: bool = False
    auto_compress_cover: bool = False

    def __post_init__(self):
        uploader_selections = set({self.image_uploader, self.cover_uploader, self.specs_uploader})
        if ("ptpimg" in uploader_selections) and self.ptpimg_key is None:
            raise ValueError("ptpimg key not specified")
        if "ptscreens" in uploader_selections and self.ptscreens_key is None:
            raise ValueError("PTScreens key not specified")
        if "oeimg" in uploader_selections and self.oeimg_key is None:
            raise ValueError("oeimage key not specified")


class TidalSettings(BaseStruct):
    token: str | None = None
    search_regions: list[str] = ("de", "nz", "us", "gb")
    fetch_regions: list[str] = ("de", "nz", "us", "gb")


# TODO: Add validations here
class QobuzSettings(BaseStruct):
    app_id: str | None = None
    user_auth_token: str | None = None
    no_genres_from_qobuz: bool = False


class Metadata(BaseStruct):
    discogs_token: str | None = None
    qobuz: QobuzSettings = msgspec.field(default_factory=QobuzSettings)
    tidal: TidalSettings = msgspec.field(default_factory=TidalSettings)


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
    type: Literal["local", "rclone", "webdav"] = "local"  # "local" or "rclone"
    directory: str = ""  # Directory when adding torrent to download client
    flac_only: bool = False  # if true, only upload FLAC files
    extra_args: list[str] = msgspec.field(default_factory=list)  # pass these arguments to rclone
    torrent_client: str = ""
    label: str = ""  # Label to apply to torrents in download client

    def __post_init__(self):
        if self.type not in ("local", "rclone", "webdav"):
            raise ValueError("Invalid seedbox type specified")


class UploadSearch(BaseStruct):
    limit: int = 3
    # TODO: are these reasonable defaults?
    excluded_labels: list[str] = ("edm comps",)
    blacklisted_genres: list[str] = ("Soundtrack", "Asian Music")


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
    host: str = "127.0.0.1"
    port: int = 55110
    static_root_url: str = "/static"

    def __post_init__(self):
        if self.port < 1 or self.port > 65535:
            raise ValueError("Port number is invalid")


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


class Upload(BaseStruct):
    simultaneous_threads: int = 3
    user_agent: str = "salmon uploading tools"

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

    upload_to_seedbox: bool = False

    # TODO: take these out of the upload struct!
    search: UploadSearch = msgspec.field(default_factory=UploadSearch)
    formatting: UploadFormatting = msgspec.field(default_factory=UploadFormatting)
    description: UploadDescription = msgspec.field(default_factory=UploadDescription)
    web_interface: UploadWebInterface = msgspec.field(default_factory=UploadWebInterface)
    requests: UploadRequests = msgspec.field(default_factory=UploadRequests)
    compression: UploadCompression = msgspec.field(default_factory=UploadCompression)


class Cfg(BaseStruct):
    "This class defines the schema that msgspec uses to parse the config"

    directory: Directory
    metadata: Metadata = msgspec.field(default_factory=Metadata)
    image: ImageUploader = msgspec.field(default_factory=ImageUploader)
    tracker: Tracker = msgspec.field(default_factory=Tracker)
    seedbox: list[Seedbox] = msgspec.field(default_factory=list)
    upload: Upload = msgspec.field(default_factory=Upload)
