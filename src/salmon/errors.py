class ScrapeError(Exception):
    def __init__(self, message, payload=None):
        self.payload = payload
        super().__init__(message)


class AbortAndDeleteFolder(Exception):
    pass


class DownloadError(Exception):
    pass


class UploadError(Exception):
    pass


class FilterError(Exception):
    pass


class TrackCombineError(Exception):
    pass


class SourceNotFoundError(Exception):
    pass


class InvalidMetadataError(Exception):
    pass


class ImageUploadFailed(Exception):
    pass


class InvalidSampleRate(Exception):
    pass


class GenreNotInWhitelist(Exception):
    pass


class NotAValidInputFile(Exception):
    pass


class UpconvertCheckError(Exception):
    """Raised when an upconvert check cannot be performed on a file."""

    pass


class NoncompliantFolderStructure(Exception):
    pass


class RequestError(Exception):
    pass


class RequestFailedError(RequestError):
    pass


class UnknownOutcomeError(RequestError):
    """A request that changes state on the tracker failed after it may have reached it.

    The tracker may or may not have acted on it, so it is not sent again.
    """

    pass


class RateLimitedError(RequestError):
    """The tracker rate limited a request and asks to wait longer than salmon waits for one request.

    Raised on a 429, which the tracker answers without acting on the request, or for an idempotent
    request, on another error status naming the rate limit. It is not sent again: the user tries
    again later.
    """

    pass


class LoginError(RequestError):
    pass


class EditedLogError(Exception):
    """Raised when a log file has been edited."""

    pass


class CRCMismatchError(Exception):
    """Raised when CRC values don't match between log and audio files."""

    pass


class LogCheckSkipped(Exception):
    """Raised when a log's CRCs can't be checked against the audio; not a verdict on the rip."""

    pass


class AmbiguousTrackOrderError(Exception):
    """Raised when retagging can't tell which file is which track from tags or folder layout."""

    pass
