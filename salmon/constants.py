import click

ARROWS = click.style(">>>", fg="cyan", bold=True)

BLACKLISTED_CHARS = r'[:\?<>\\*\|"\/]'

BLACKLISTED_FULLWIDTH_REPLACEMENTS = {
    ":": "：",
    "?": "？",
    "<": "＜",
    ">": "＞",
    "*": "＊",
    "|": "｜",
    '"': "＂",
    "/": "／",
    "\\": "＼",
}

QUALITY_INDEX = ["LOSSLESS", "HIGH", "LOW"]

SOURCES = {
    "web": "WEB",
    "cd": "CD",
    "dvd": "DVD",
    "vinyl": "Vinyl",
    "soundboard": "Soundboard",
    "sacd": "SACD",
    "dat": "DAT",
    "cassette": "Cassette",
}

ARTIST_IMPORTANCES = {
    "main": 1,
    "guest": 2,
    "remixer": 3,
    "composer": 4,
    "conductor": 5,
    "djcompiler": 6,
    "producer": 7,
}

RELEASE_TYPES = {
    "Album": 1,
    "Soundtrack": 3,
    "EP": 5,
    "Anthology": 6,
    "Compilation": 7,
    "Single": 9,
    "Live album": 11,
    "Remix": 13,
    "Bootleg": 14,
    "Interview": 15,
    "Mixtape": 16,
    "Demo": 17,
    "Concert Recording": 18,
    "DJ Mix": 19,
    "Unknown": 21,
}

ENCODINGS = {
    "24bit Lossless",
    "Lossless",
    "192",
    "256",
    "320",
    "V0 (VBR)",
    "V2 (VBR)",
}  # Fuck the esoteric and Other, no support found here.

TAG_ENCODINGS = {
    "V0": ["V0 (VBR)", False],
    "V1": ["V1 (VBR)", False],
    "V2": ["V2 (VBR)", False],
    "320": ["320", False],
    "256": ["256", False],
    "192": ["192", False],
    "320V": ["320", True],
    "256V": ["256", True],
    "192V": ["192", True],
}

FORMATS = {
    ".flac": "FLAC",
    ".m4a": "AAC",
    ".mp3": "MP3",
}

UAGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/67.0.3396.87 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:60.0) Gecko/20100101 Firefox/60.0",
    "Mozilla/5.0 (X11; U; Linux i686; en-US) AppleWebKit/534.3 (KHTML, like Gecko)"
    "Chrome/6.0.472.63 Safari/534.3",
    "Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 5.1)",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    "Chrome/57.0.2987.133 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/57.0.2987.133 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/55.0.2883.87 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    "Chrome/62.0.3202.94 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.2; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    "Chrome/55.0.2883.87 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_6) AppleWebKit/537.36 (KHTML, like "
    "Gecko) Chrome/61.0.3163.100 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    "Chrome/60.0.3112.90 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.3; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    "Chrome/55.0.2883.87 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/39.0.2171.95 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/42.0.2311.135 Safari/537.36",
]


GENRE_LIST = {
    "abstract": {"Abstract"},
    "acid": {"Acid"},
    "acidhouse": {"Acid House"},
    "acoustic": {"Acoustic"},
    "african": {"African"},
    "afrobeat": {"Afrobeat"},
    "alternative": {"Alternative"},
    "ambient": {"Ambient"},
    "american": {"American"},
    "americana": {"Americana"},
    "anime": {"Anime"},
    "artrock": {"Art Rock"},
    "australian": {"Australian"},
    "avantgarde": {"Avant Garde"},
    "ballad": {"Ballad"},
    "baroque": {"Baroque"},
    "bass": {"Bass"},
    "beats": {"Beats"},
    "bigband": {"Big Band"},
    "blackmetal": {"Black Metal"},
    "bluegrass": {"Bluegrass"},
    "blues": {"Blues"},
    "bluesrock": {"Blues Rock"},
    "bossanova": {"Bossa Nova"},
    "brazilian": {"Brazilian"},
    "breakbeat": {"Breakbeat"},
    "breakcore": {"Breakcore"},
    "breaks": {"Breaks"},
    "british": {"British"},
    "britpop": {"Britpop"},
    "canadian": {"Canadian"},
    "celtic": {"Celtic"},
    "chambermusic": {"Chamber Music"},
    "chillout": {"Chillout"},
    "chillwave": {"Chillwave"},
    "chiptune": {"Chiptune"},
    "choral": {"Choral"},
    "christian": {"Christian"},
    "christmas": {"Christmas"},
    "classical": {"Classical"},
    "classicrock": {"Classic Rock"},
    "comedy": {"Comedy"},
    "contemporary": {"Contemporary"},
    "contemporaryjazz": {"Contemporary Jazz"},
    "country": {"Country"},
    "countryrock": {"Country Rock"},
    "dance": {"Dance"},
    "dancehall": {"Dancehall"},
    "darkambient": {"Dark Ambient"},
    "darkpsytrance": {"Dark Psytrance"},
    "darkwave": {"Darkwave"},
    "deathmetal": {"Death Metal"},
    "deephouse": {"Deep House"},
    "deeptech": {"Deep Tech"},
    "disco": {"Disco"},
    "doommetal": {"Doom Metal"},
    "doujin": {"Doujin"},
    "downtempo": {"Downtempo"},
    "dreampop": {"Dream Pop"},
    "drone": {"Drone"},
    "drumbass": {"Drum & Bass"},
    "drumnbass": {"Drum & Bass"},
    "drumandbass": {"Drum & Bass"},
    "dnb": {"Drum & Bass"},
    "dub": {"Dub"},
    "dubstep": {"Dubstep"},
    "dubtechno": {"Dub Techno"},
    "dutch": {"Dutch"},
    "easylistening": {"Easy Listening"},
    "ebm": {"Ebm"},
    "electro": {"Electro"},
    "electrohouse": {"Electro House"},
    "electronic": {"Electronic"},
    "emo": {"Emo"},
    "epic": {"Epic"},
    "ethereal": {"Ethereal"},
    "eurohouse": {"Euro House"},
    "europop": {"Europop"},
    "experimental": {"Experimental"},
    "fantasy": {"Fantasy"},
    "femalevocalist": {"Female Vocalist"},
    "fieldrecording": {"Field Recording"},
    "finnish": {"Finnish"},
    "folk": {"Folk"},
    "folkrock": {"Folk Rock"},
    "freeimprovisation": {"Free Improvisation"},
    "freejazz": {"Free Jazz"},
    "freelyavailable": {"Freely Available"},
    "funk": {"Funk"},
    "fusion": {"Fusion"},
    "futurejazz": {"Future Jazz"},
    "gabber": {"Gabber"},
    "gangsta": {"Gangsta"},
    "garagehouse": {"Garage House"},
    "garagerock": {"Garage Rock"},
    "german": {"German"},
    "glitch": {"Glitch"},
    "goatrance": {"Goa Trance"},
    "gospel": {"Gospel"},
    "gothicrock": {"Gothic Rock"},
    "greek": {"Greek"},
    "grime": {"Grime"},
    "grindcore": {"Grindcore"},
    "guitar": {"Guitar"},
    "happyhardcore": {"Happy Hardcore"},
    "hardbop": {"Hard Bop"},
    "hardcoredance": {"Hardcore Dance"},
    "hardcorepunk": {"Hardcore Punk"},
    "hardrock": {"Hard Rock"},
    "hardstyle": {"Hardstyle"},
    "hardtrance": {"Hard Trance"},
    "heavymetal": {"Heavy Metal"},
    "hiphop": {"Hip Hop"},
    "hiphoprap": {"Hip Hop", "Rap"},
    "raphiphop": {"Hip Hop", "Rap"},
    "history": {"History"},
    "house": {"House"},
    "idm": {"idm"},
    "indie": {"Indie"},
    "indiedance": {"Indie Dance"},
    "indiepop": {"Indie Pop"},
    "indierock": {"Indie Rock"},
    "industrial": {"Industrial"},
    "instrumental": {"Instrumental"},
    "italian": {"Italian"},
    "italodisco": {"Italo Disco"},
    "jam": {"Jam"},
    "jamband": {"Jam Band"},
    "japanese": {"Japanese"},
    "jazz": {"Jazz"},
    "jazzfunk": {"Jazz Funk"},
    "jazzrock": {"Jazz Rock"},
    "jpop": {"J-Pop"},
    "jungle": {"Jungle"},
    "korean": {"Korean"},
    "kpop": {"K-Pop"},
    "krautrock": {"Krautrock"},
    "latin": {"Latin"},
    "leftfield": {"Leftfield"},
    "library": {"Library"},
    "lofi": {"Lo Fi"},
    "loops": {"Loops"},
    "lounge": {"Lounge"},
    "mathrock": {"Math Rock"},
    "melodicdeathmetal": {"Melodic Death Metal"},
    "metal": {"Metal"},
    "metalcore": {"Metalcore"},
    "minimal": {"Minimal"},
    "minimalhouse": {"Minimal House"},
    "modernclassical": {"Modern Classical"},
    "mpb": {"mpb"},
    "musiqueconcrete": {"Musique Concrete"},
    "mystery": {"Mystery"},
    "neofolk": {"Neofolk"},
    "newage": {"New Age"},
    "newwave": {"New Wave"},
    "newyork": {"New York"},
    "noise": {"Noise"},
    "noiserock": {"Noise Rock"},
    "norwegian": {"Norwegian"},
    "nudisco": {"Nu Disco"},
    "numetal": {"Nu Metal"},
    "opera": {"Opera"},
    "orchestral": {"Orchestral"},
    "piano": {"Piano"},
    "polish": {"Polish"},
    "pop": {"Pop"},
    "poppunk": {"Pop Punk"},
    "poprap": {"Pop Rap"},
    "poprock": {"Pop Rock"},
    "postbop": {"Post Bop"},
    "posthardcore": {"Post Hardcore"},
    "postmetal": {"Post Metal"},
    "postpunk": {"Post Punk"},
    "postrock": {"Post Rock"},
    "powermetal": {"Power Metal"},
    "powerpop": {"Power Pop"},
    "progressivehouse": {"Progressive House"},
    "progressivemetal": {"Progressive Metal"},
    "progressiverock": {"Progressive Rock"},
    "progressivetrance": {"Progressive Trance"},
    "psybient": {"Psybient"},
    "psychedelic": {"Psychedelic"},
    "psychedelicrock": {"Psychedelic Rock"},
    "psychill": {"Psychill"},
    "psytrance": {"Psytrance"},
    "punk": {"Punk"},
    "rap": {"Rap"},
    "reggae": {"Reggae"},
    "rhythmnblues": {"Rhythm & Blues"},
    "rhythmblues": {"Rhythm & Blues"},
    "randb": {"Rhythm & Blues"},
    "rnb": {"Rhythm & Blues"},
    "rhythmandblues": {"Rhythm & Blues"},
    "rock": {"Rock"},
    "rockandroll": {"Rock & Roll"},
    "romantic": {"Romantic"},
    "rootsreggae": {"Roots Reggae"},
    "russian": {"Russian"},
    "samplepack": {"Sample Pack"},
    "score": {"Score"},
    "screamo": {"Screamo"},
    "shoegaze": {"Shoegaze"},
    "singersongwriter": {"Singer & Songwriter"},
    "singerandsongwriter": {"Singer & Songwriter"},
    "ska": {"Ska"},
    "sludgemetal": {"Sludge Metal"},
    "smoothjazz": {"Smooth Jazz"},
    "softrock": {"Soft Rock"},
    "soul": {"Soul"},
    "souljazz": {"Soul Jazz"},
    "southernrock": {"Southern Rock"},
    "spacerock": {"Space Rock"},
    "spanish": {"Spanish"},
    "spokenword": {"Spoken Word"},
    "stageandscreen": {"Stage and Screen"},
    "stonerrock": {"Stoner Rock"},
    "surf": {"Surf"},
    "swedish": {"Swedish"},
    "swing": {"Swing"},
    "synthpop": {"Synthpop"},
    "synthwave": {"Synthwave"},
    "techhouse": {"Tech House"},
    "technohouse": {"Techno", "House"},
    "techno": {"Techno"},
    "thrashmetal": {"Thrash Metal"},
    "thriller": {"Thriller"},
    "touhou": {"Touhou"},
    "trance": {"Trance"},
    "trap": {"Trap"},
    "tribal": {"Tribal"},
    "triphop": {"Trip Hop"},
    "turkish": {"Turkish"},
    "ukgarage": {"UK Garage"},
    "upliftingtrance": {"Uplifting Trance"},
    "vaporwave": {"Vaporwave"},
    "videogame": {"Video Game"},
    "vocal": {"Vocal"},
    "worldmusic": {"World Music"},
    # French to English
    "musiqueelectronique": {"Electronic"},
    "electronique": {"Electronic"},
    "musiqueclassique": {"Classical"},
    "rapfrancais": {"French Rap"},
    "musiquedumonde": {"World Music"},
    "varietefrancaise": {"French Pop"},
    "jazzvocal": {"Vocal Jazz"},
    # German to English
    "elektronischemusik": {"Electronic"},
    "klassischemusik": {"Classical"},
    "zeitgenossischemusik": {"Contemporary"},
    # Spanish to English
    "musicaelectronica": {"Electronic"},
    "musicaclasica": {"Classical"},
    # Common mistranslations or variants
    "electronica": {"Electronic"},
    "indieandalternative": {"Indie"},
    "alternativeandindie": {"Alternative"},
    "randbsoul": {"R&B"}
}

ALLOWED_EXTENSIONS = {
    ".ac3",
    ".accurip",
    ".chm",
    ".cue",
    ".dts",
    ".flac",
    ".gif",
    ".htm",
    ".html",
    ".jpeg",
    ".jpg",
    ".log",
    ".m3u",
    ".m3u8",
    ".m4a",
    ".m4b",
    ".md5",
    ".mp3",
    ".nfo",
    ".pdf",
    ".png",
    ".rtf",
    ".sfv",
    ".txt",
}
