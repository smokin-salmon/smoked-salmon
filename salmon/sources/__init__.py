from salmon import cfg
from salmon.sources.apple_music import AppleMusicBase
from salmon.sources.bandcamp import BandcampBase
from salmon.sources.beatport import BeatportBase
from salmon.sources.deezer import DeezerBase
from salmon.sources.discogs import DiscogsBase
from salmon.sources.musicbrainz import MusicBrainzBase
from salmon.sources.qobuz import QobuzBase
from salmon.sources.tidal import TidalBase

__all__ = [
    "cfg",
    "BandcampBase",
    "BeatportBase",
    "DeezerBase",
    "DiscogsBase",
    "AppleMusicBase",
    "MusicBrainzBase",
    "QobuzBase",
    "TidalBase",
    "SOURCE_ICONS",
]

SOURCE_ICONS = {
    "Bandcamp": "https://ptpimg.me/91oo89.png",
    "Beatport": "https://ptpimg.me/5hwjpv.png",
    "Deezer": "https://ptpimg.me/m265v2.png",
    "Discogs": "https://ptpimg.me/mt4ql3.png",
    "Apple Music": "https://ptpimg.me/0z2x90.png",
    "MusicBrainz": "https://ptpimg.me/56plwd.png",
    "Qobuz": "https://ptpimg.me/e4d045.png",
    "Tidal": "https://ptpimg.me/5vxo23.png",
}
