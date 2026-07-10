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
    "Bandcamp": "https://i.ibb.co/jd8Ysrm/bandcamp.png",
    "Beatport": "https://i.ibb.co/fdbp8V7S/beatport.png",
    "Deezer": "https://i.ibb.co/zHbd0GMm/deezer.png",
    "Discogs": "https://i.ibb.co/TMPGqrKj/discogs.png",
    "Apple Music": "https://i.ibb.co/KpqNkPn2/applemusic.png",
    "MusicBrainz": "https://i.ibb.co/Y42MftV1/musicbrainz.png",
    "Qobuz": "https://i.ibb.co/39M2cBx9/qobuz.png",
    "Tidal": "https://i.ibb.co/bjnnJcFm/tidal.png",
}
