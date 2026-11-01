import os
from collections.abc import Iterable, Iterator
from typing import Annotated, Literal, get_args

import msgspec

from salmon import proxy

from .image_hosts import spectrals_refusal, tracker_only_hosts


class BaseStruct(msgspec.Struct, forbid_unknown_fields=False):
    pass


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _lexical(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _holds(folder: str, path: str) -> bool:
    """Whether path is folder or inside it, resolved or as given (commonpath: /music-old is not in /music)."""
    # As given too: an album symlinked into a library is in it, wherever the link points.
    for norm in (_real, _lexical):
        try:
            if os.path.commonpath([norm(folder), norm(path)]) == norm(folder):
                return True
        except ValueError:  # Different drives on Windows.
            continue
    # Strings miss ~/music on a case-insensitive volume when the folder is ~/Music, and a second bind mount.
    return _same_dir_above(folder, path)


def _same_dir_above(folder: str, path: str) -> bool:
    """Whether path or a parent of it, resolved or as given, is the directory folder (same device and inode)."""
    try:
        target = os.stat(_real(folder))
    except OSError:
        return False
    return any(_same_as(above, target) for norm in (_real, _lexical) for above in _ancestors(norm(path)))


def _ancestors(path: str) -> Iterator[str]:
    while True:
        yield path
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


def _same_as(path: str, target: os.stat_result) -> bool:
    try:
        return os.path.samestat(os.stat(path), target)
    except OSError:
        return False


class Directory(BaseStruct):
    dottorrents_dir: str
    download_directory: str
    hardlinks: bool = True
    tmp_dir: str | None = None
    clean_tmp_dir: bool = False
    # Curated music folders: salmon works on a copy of an album inside one, and never changes or deletes it.
    library_dirs: list[str] = msgspec.field(default_factory=list)

    def __post_init__(self):
        if not os.path.isdir(self.dottorrents_dir):
            raise ValueError("dottorrents_dir is not a valid directory")
        if not os.path.isdir(self.download_directory):
            raise ValueError("download_directory is not a valid directory")
        if self.tmp_dir and not os.path.isdir(self.tmp_dir):
            raise ValueError("tmp_dir is not a valid directory")
        for entry in self.library_dirs:
            if not os.path.isdir(entry):
                raise ValueError(f"library_dirs entry is not a valid directory: {entry}")
            # salmon writes and deletes in these, and a copy of a library album would land in the library.
            for name, folder in (
                ("download_directory", self.download_directory),
                ("dottorrents_dir", self.dottorrents_dir),
                ("tmp_dir", self.tmp_dir),
            ):
                if folder and _holds(entry, folder):
                    raise ValueError(f"library_dirs entry {entry} must not contain {name} ({folder})")
            # salmon replaces and deletes folders in these: renamed copies, conversions, spectrals, clean_tmp_dir.
            for name, folder in (("download_directory", self.download_directory), ("tmp_dir", self.tmp_dir)):
                if folder and _holds(folder, entry):
                    raise ValueError(f"library_dirs entry {entry} must not be inside {name} ({folder})")

    def library_of(self, path: str) -> str | None:
        """The library_dirs entry path is or is inside, compared resolved and as given, or None."""
        return next((entry for entry in self.library_dirs if _holds(entry, path)), None)

    def is_library_path(self, path: str) -> bool:
        """Whether path is a library_dirs entry or inside one, compared resolved and as given."""
        return self.library_of(path) is not None

    def library_inside(self, path: str) -> str | None:
        """A library_dirs entry that path is or holds, or None."""
        return next((entry for entry in self.library_dirs if _holds(path, entry)), None)

    def protects(self, path: str) -> bool:
        """Whether changing or deleting path could change a library album: it is in a library or holds one."""
        return self.is_library_path(path) or self.library_inside(path) is not None


ImgUploaderLiteral = Literal["ptscreens", "oeimg", "catbox", "imgbb", "imgbox", "ra", "red"]
# "*" (all), "+" (a random third), "0" (none), or track IDs separated by spaces, such as "3" or "1 5 9".
SpectralSelection = Annotated[str, msgspec.Meta(pattern=r"^(\*|\+|0|0*[1-9]\d*( +0*[1-9]\d*)*)$")]

_TRACKER_CODES = ("red", "ops", "dic")

# The settings that choose an image host: for description images (salmon images up), covers and spectrals.
ImageKind = Literal["image_uploader", "cover_uploader", "specs_uploader"]
_IMAGE_KINDS: tuple[ImageKind, ...] = get_args(ImageKind)


class TrackerImageSettings(BaseStruct):
    """Per-tracker image settings, overriding [image] for uploads to that tracker."""

    image_uploader: ImgUploaderLiteral | None = None
    cover_uploader: ImgUploaderLiteral | None = None
    specs_uploader: ImgUploaderLiteral | None = None


class ImageUploader(BaseStruct):
    image_uploader: ImgUploaderLiteral = "catbox"
    cover_uploader: ImgUploaderLiteral = "catbox"
    specs_uploader: ImgUploaderLiteral = "catbox"
    ptscreens_key: str | None = None
    oeimg_key: str | None = None
    catbox_userhash: str | None = None
    imgbb_key: str | None = None
    ra_key: str | None = None
    remove_auto_downloaded_cover_image: bool = False
    # Embed the folder cover, shrunk to fit, in every FLAC with no picture; the cover file is never changed.
    embed_missing_cover: bool = True
    strip_oversized_pictures: bool = True
    default_spectral_ids: SpectralSelection | None = None
    red: TrackerImageSettings | None = None
    ops: TrackerImageSettings | None = None
    dic: TrackerImageSettings | None = None

    def host_for(self, site_code: str | None, kind: ImageKind) -> str:
        """Get a tracker's image host for one kind of image: its [image.<tracker>] setting, else the [image] one.

        A site_code of None, for images that are not for one tracker, gets the [image] setting.
        """
        override: TrackerImageSettings | None = getattr(self, site_code.lower(), None) if site_code else None
        host = getattr(override, kind) if override is not None else None
        return host if host is not None else getattr(self, kind)

    def selections(self) -> list[tuple[str, ImageKind, str]]:
        """Get every configured (setting name, kind, host), per-tracker ones included (as "red.cover_uploader")."""
        selections: list[tuple[str, ImageKind, str]] = [(kind, kind, getattr(self, kind)) for kind in _IMAGE_KINDS]
        for code in _TRACKER_CODES:
            override: TrackerImageSettings | None = getattr(self, code)
            if override is not None:
                for kind in _IMAGE_KINDS:
                    if (host := getattr(override, kind)) is not None:
                        selections.append((f"{code}.{kind}", kind, host))
        return selections

    def __post_init__(self):
        selections = self.selections()
        uploader_selections = {host for _, _, host in selections}
        if "ptscreens" in uploader_selections and self.ptscreens_key is None:
            raise ValueError("PTScreens key not specified")
        if "oeimg" in uploader_selections and self.oeimg_key is None:
            raise ValueError("oeimage key not specified")
        if "imgbb" in uploader_selections and self.imgbb_key is None:
            raise ValueError("imgbb key not specified")
        if "ra" in uploader_selections and self.ra_key is None:
            raise ValueError("ra key not specified")
        tracker_only = tracker_only_hosts()
        for setting, kind, host in selections:
            # The tracker a per-tracker setting is for; None for the [image] ones, which every tracker shares.
            tracker = setting.rpartition(".")[0] or None
            if kind == "specs_uploader" and (refusal := spectrals_refusal(host, tracker)) is not None:
                raise ValueError(f'image.{setting} = "{host}": {refusal}; choose another specs_uploader')
            trackers = tracker_only.get(host)
            if trackers is not None and tracker not in trackers:
                sections = " or ".join(f"[image.{code}]" for code in trackers)
                raise ValueError(
                    f'image.{setting} = "{host}": the {host} image host only displays on '
                    f"{'/'.join(code.upper() for code in trackers)}, so it can only be set under {sections}"
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


def _check_corrections(kind: str, table: dict, replacements: list[str], one: str) -> None:
    if any(not name.strip() for name in [*table, *replacements]):
        raise ValueError(f"metadata.corrections.{kind} has an empty name.")
    keys = [name.strip().casefold() for name in table]
    if len(set(keys)) < len(keys):
        raise ValueError(f"metadata.corrections.{kind} names {one} twice, in two cases or spacings.")


class Corrections(BaseStruct):
    # An artist's name (whole, any case, outer spaces ignored) and the name salmon writes instead, trimmed.
    artists: dict[str, str] = msgspec.field(default_factory=dict)
    # A genre (matched the same way) and the genres salmon writes in its place; none drops it.
    genres: dict[str, list[str]] = msgspec.field(default_factory=dict)

    def __post_init__(self):
        _check_corrections("artists", self.artists, list(self.artists.values()), "an artist")
        self.artists = {name: to.strip() for name, to in self.artists.items()}
        _check_corrections("genres", self.genres, [g for to in self.genres.values() for g in to], "a genre")


class Metadata(BaseStruct):
    discogs_token: str | None = None
    qobuz: QobuzSettings = msgspec.field(default_factory=QobuzSettings)
    tidal: TidalSettings = msgspec.field(default_factory=TidalSettings)
    apple_music: AppleMusicSettings = msgspec.field(default_factory=AppleMusicSettings)
    beatport: BeatportSettings = msgspec.field(default_factory=BeatportSettings)
    corrections: Corrections = msgspec.field(default_factory=Corrections)


class GazelleTrackerSettings(BaseStruct):
    session: str
    api_key: str | None = None
    # TODO: validate this
    dottorrents_dir: str | None = None


class Tracker(BaseStruct):
    red: GazelleTrackerSettings | None = None
    ops: GazelleTrackerSettings | None = None
    dic: GazelleTrackerSettings | None = None
    # Without -t, a run uploads to these in order, then offers the other trackers. Empty: asks which.
    default_trackers: list[str] = msgspec.field(default_factory=list)
    # Print every tracker request and response, secrets masked.
    debug_connection: bool = False

    def __post_init__(self):
        if (self.red is None) and (self.ops is None) and (self.dic is None):
            raise ValueError("You need a tracker session cookie in your config!")

        self.default_trackers = list(
            dict.fromkeys(code.strip().upper() for code in self.default_trackers if code.strip())
        )
        for code in self.default_trackers:
            if code not in ("RED", "OPS", "DIC"):
                raise ValueError(f"tracker.default_trackers names {code}, which is not RED, OPS or DIC.")
            if getattr(self, code.lower()) is None:
                raise ValueError(
                    f"tracker.default_trackers names {code}, which has no [tracker.{code.lower()}] in your config."
                )


class Seedbox(BaseStruct):
    name: str = ""
    enabled: bool = True  # false skips this seedbox
    url: str = ""  # Name of remote in rclone
    type: Literal["local", "rclone"] = "local"
    directory: str = ""  # Directory when adding torrent to download client
    # Trackers this seedbox serves; empty means every tracker, so existing configs are unchanged.
    trackers: list[str] = msgspec.field(default_factory=list)
    flac_only: bool = False  # if true, only upload FLAC files
    extra_args: list[str] = msgspec.field(default_factory=list)  # pass these arguments to rclone
    torrent_client: str = ""
    label: str = ""  # Label to apply to torrents in download client
    add_paused: bool = False  # If true, add torrents to client in paused state
    # rclone only: salmon up removes the folder from download_directory once every seedbox has it and seeds it.
    remove_local_after_copy: bool = False

    def __post_init__(self):
        if self.type not in ("local", "rclone"):
            raise ValueError("Invalid seedbox type specified")
        if self.remove_local_after_copy and self.type != "rclone":
            raise ValueError(f"seedbox '{self.name}': remove_local_after_copy only applies to rclone seedboxes")
        self.trackers = [t.upper() for t in self.trackers]
        unknown = sorted(set(self.trackers) - {"RED", "OPS", "DIC"})
        if unknown:
            raise ValueError(f"Unknown tracker(s) for seedbox '{self.name}': {', '.join(unknown)}")


# The stores salmon searches for metadata, as salmon.search.SEARCHSOURCES names them (a test keeps the two equal).
# Spelled out here: salmon.search needs the config before it can be imported.
SEARCH_STORES = ("Bandcamp", "MusicBrainz", "Apple Music", "Discogs", "Beatport", "Qobuz", "Tidal", "Deezer")


def _search_stores(setting: str, names: list[str]) -> list[str]:
    """`names` as the stores' own names, each once, in order; ValueError on a name that is not a store salmon searches.

    Case does not matter: "bandcamp" is Bandcamp.
    """
    known = {store.lower(): store for store in SEARCH_STORES}
    stores: list[str] = []
    for name in names:
        store = known.get(name.strip().lower())
        if store is None:
            raise ValueError(
                f'upload.search.{setting} names "{name}", which is not a store salmon searches: '
                f"{', '.join(SEARCH_STORES)}."
            )
        if store not in stores:
            stores.append(store)
    return stores


class UploadSearch(BaseStruct):
    limit: int = 3
    # TODO: are these reasonable defaults?
    excluded_labels: list[str] = msgspec.field(default_factory=lambda: ["edm comps"])
    blacklisted_genres: list[str] = msgspec.field(default_factory=lambda: ["Soundtrack", "Asian Music"])
    # Stores whose results are listed first, in this order, and marked "preferred".
    preferred_sources: list[str] = msgspec.field(default_factory=list)
    # Stores salmon never searches, so never shows.
    skipped_sources: list[str] = msgspec.field(default_factory=list)

    def __post_init__(self):
        self.preferred_sources = _search_stores("preferred_sources", self.preferred_sources)
        self.skipped_sources = _search_stores("skipped_sources", self.skipped_sources)
        both = [store for store in self.preferred_sources if store in self.skipped_sources]
        if both:
            raise ValueError(
                f"upload.search names {', '.join(both)} in both preferred_sources and skipped_sources: keep each "
                "store in one of them."
            )


class UploadFormatting(BaseStruct):
    folder_template: str = "{artists} - {title} ({year}) [{source} {format}]"
    file_template: str = "{tracknumber}. {artist} - {title}"
    remove_source_dir: bool = False
    split_multi_disc_into_folders: bool = True

    # formatting options
    no_artist_in_filename_if_only_one_album_artist: bool = True
    one_album_artist_file_template: str = "{tracknumber}. {title}"
    lowercase_cover: bool = True
    various_artist_threshold: int = 4
    blacklisted_substitution: str = "_"
    guests_in_track_title: bool = False
    various_artist_word: str = "Various"
    strip_useless_versions: bool = True
    strip_ep_from_title: bool = True
    add_edition_title_to_album_tag: bool = True
    review_as_comment_tag: bool = True
    fullwidth_replacements: bool = False
    empty_track_comment_tag: bool = True


class UploadDescription(BaseStruct):
    bitrates_in_t_desc: bool = False
    include_tracklist_in_t_desc: bool = False
    copy_uploaded_url_to_clipboard: bool = False
    icons_in_descriptions: bool = True
    artist_tags_in_tracklist: bool = False
    lma_comment_in_t_desc: bool = False


class UploadSpectralsViewer(BaseStruct):
    """The spectrals viewer salmon upload opens in the browser, not ``salmon web`` ([web])."""

    host: str = "0.0.0.0"
    port: int = 55110
    display_host: str = "localhost"

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
    confirm_new_group: bool = False
    last_minute_dupe_check: bool = False


class UploadCompression(BaseStruct):
    flac_compression_level: Annotated[int, msgspec.Meta(ge=0, le=8)] = 8
    compress_spectrals: bool = True
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


# The conversions salmon up offers after a FLAC, by a name that holds whatever the release's rates are.
CONVERSION_KINDS = ("24bit", "16bit", "320", "V0")


def canonical_conversions(values: Iterable[str]) -> tuple[str, ...]:
    """The conversion kinds named, in canonical case and the order given.

    Raises:
        ValueError: A value is not one of CONVERSION_KINDS (in any case), or is named twice.
    """
    by_lower = {kind.lower(): kind for kind in CONVERSION_KINDS}
    allowed = ", ".join(f'"{kind}"' for kind in CONVERSION_KINDS)
    kinds: list[str] = []
    for value in values:
        kind = by_lower.get(value.strip().lower()) if isinstance(value, str) else None
        if kind is None:
            raise ValueError(f"{value!r} is not a conversion; the choices are {allowed}")
        if kind in kinds:
            raise ValueError(f"{kind!r} is named twice")
        kinds.append(kind)
    return tuple(kinds)


class Upload(BaseStruct):
    simultaneous_threads: int = 3
    user_agent: str = "salmon uploading tools"

    # Default text editor for click.edit operations
    # Can be "nano", "vim", "emacs", or any command available in PATH
    default_editor: str | None = None

    native_spectrals_viewer: bool = False
    # With native_spectrals_viewer: draw the spectrals in the terminal when it speaks the Kitty graphics protocol
    # (kitty, Ghostty, WezTerm...), over SSH too. False opens feh / Quick Look / the Windows viewer in any.
    terminal_spectrals_viewer: bool = True
    feh_fullscreen: bool = True
    prompt_puddletag: bool = False
    # must be within 0-1
    log_dupe_tolerance: Annotated[float, msgspec.Meta(ge=0.0, le=1.0)] = 0.5
    windows_use_recycle_bin: bool = True

    multi_tracker_upload: bool = True

    update_notification: bool = True
    update_notification_verbose: bool = True

    yes_all: bool = False

    # The conversions salmon up selects by default after a FLAC (and makes under -yyy): "24bit", "16bit", "320",
    # "V0". [] makes none. Every possible one is still offered at the prompt. Stored in canonical case.
    conversions: list[str] = msgspec.field(default_factory=lambda: list(CONVERSION_KINDS))

    # Check every FLAC file for an MQA marker before uploading. False checks only the first one: faster,
    # but MQA on another track is not caught.
    mqa_check_all_tracks: bool = True

    upload_to_seedbox: bool = True

    # Normalize file names in generated .torrent files to a fixed Unicode form.
    # "" or "none" (default) keeps names as on disk; "NFC" / "NFD" must be the form of the files the client seeds.
    torrent_name_normalization: str = ""

    # TODO: take these out of the upload struct!
    search: UploadSearch = msgspec.field(default_factory=UploadSearch)
    formatting: UploadFormatting = msgspec.field(default_factory=UploadFormatting)
    description: UploadDescription = msgspec.field(default_factory=UploadDescription)
    spectrals_viewer: UploadSpectralsViewer = msgspec.field(default_factory=UploadSpectralsViewer)
    requests: UploadRequests = msgspec.field(default_factory=UploadRequests)
    compression: UploadCompression = msgspec.field(default_factory=UploadCompression)
    ai_review: UploadAiReview = msgspec.field(default_factory=UploadAiReview)

    def __post_init__(self):
        valid_normalizations = ("", "none", "NFC", "NFD")
        if self.torrent_name_normalization not in valid_normalizations:
            raise ValueError(
                "upload.torrent_name_normalization must be one of "
                f"{valid_normalizations}, got {self.torrent_name_normalization!r}"
            )
        try:
            self.conversions = list(canonical_conversions(self.conversions))
        except ValueError as e:
            raise ValueError(f"upload.conversions: {e}") from None


class ProxyServicesCfg(BaseStruct):
    """A proxy per service, over [proxy] url: None keeps that one, an empty string connects directly."""

    # Trackers
    red: str | None = None
    ops: str | None = None
    dic: str | None = None
    # Metadata sources
    qobuz: str | None = None
    tidal: str | None = None
    bandcamp: str | None = None
    deezer: str | None = None
    beatport: str | None = None
    discogs: str | None = None
    apple_music: str | None = None
    # Image hosts
    catbox: str | None = None
    imgbb: str | None = None
    oeimg: str | None = None
    ptscreens: str | None = None
    ra: str | None = None


class ProxyCfg(BaseStruct):
    url: str | None = None
    services: ProxyServicesCfg = msgspec.field(default_factory=ProxyServicesCfg)

    def __post_init__(self):
        settings = {"url": self.url} | {
            f"services.{name}": getattr(self.services, name) for name in self.services.__struct_fields__
        }
        for key, url in settings.items():
            if url and not proxy.is_valid_url(url):
                # Without the URL itself: it may hold the proxy's password.
                raise ValueError(
                    f"proxy.{key} must be scheme://[user:password@]host:port, "
                    f"with scheme one of {', '.join(proxy.SCHEMES)}"
                )


# A web token guards uploads with the user's tracker accounts: long enough that guessing it is hopeless.
WEB_TOKEN_MIN_LENGTH = 32


class Web(BaseStruct):
    """``salmon web``. The CLI's spectrals viewer keeps its own [upload.spectrals_viewer]."""

    host: str = "127.0.0.1"
    port: int = 55155
    token: str | None = None
    # Host header values accepted besides loopback names and the bind address (DNS rebinding).
    allowed_hosts: list[str] = msgspec.field(default_factory=list)
    max_jobs: int = 2

    def __post_init__(self):
        if self.port < 1 or self.port > 65535:
            raise ValueError("web.port must be between 1 and 65535")
        if self.token == "":
            self.token = None
        if self.token is not None and len(self.token) < WEB_TOKEN_MIN_LENGTH:
            raise ValueError(
                f"web.token must be at least {WEB_TOKEN_MIN_LENGTH} characters"
                ' (python -c "import secrets; print(secrets.token_urlsafe(32))" makes one)'
            )
        if self.max_jobs < 1:
            raise ValueError("web.max_jobs must be at least 1")


class Cfg(BaseStruct):
    "This class defines the schema that msgspec uses to parse the config"

    directory: Directory
    metadata: Metadata = msgspec.field(default_factory=Metadata)
    image: ImageUploader = msgspec.field(default_factory=ImageUploader)
    tracker: Tracker = msgspec.field(default_factory=Tracker)
    seedbox: list[Seedbox] = msgspec.field(default_factory=list)
    upload: Upload = msgspec.field(default_factory=Upload)
    proxy: ProxyCfg = msgspec.field(default_factory=ProxyCfg)
    web: Web = msgspec.field(default_factory=Web)

    def __post_init__(self):
        # Uploads to RED's image host authenticate with the RED API key, whichever tracker the image is for.
        red_host_users = [setting for setting, _kind, host in self.image.selections() if host == "red"]
        if red_host_users and not (self.tracker.red and self.tracker.red.api_key):
            raise ValueError(f'image.{red_host_users[0]} = "red" needs tracker.red.api_key to be set')
        # A tracker's own dottorrents_dir gets .torrent files written with overwrite, as the global one does.
        for code in _TRACKER_CODES:
            settings = getattr(self.tracker, code)
            folder = settings.dottorrents_dir if settings else None
            if folder and (library := self.directory.library_of(folder)) is not None:
                raise ValueError(
                    f"library_dirs entry {library} must not contain tracker.{code}.dottorrents_dir ({folder})"
                )
