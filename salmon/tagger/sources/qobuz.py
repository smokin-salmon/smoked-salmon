import re
from collections import defaultdict
from html import unescape

from salmon.common import RE_FEAT, parse_copyright, re_split
from salmon.errors import ScrapeError
from salmon.sources import QobuzBase
from salmon.tagger.sources.base import MetadataMixin

#------------------------------------------------------------------------------
# Constants and Regular Expressions
#------------------------------------------------------------------------------

# Pre-compiled regular expressions for better performance
RE_YEAR = re.compile(r"(\d{4})")
RE_EDITION = re.compile(r"\((.*?)\)(?:\s*-\s*(?:Single|EP))?$")
RE_EP = re.compile(r" ?-? *E\.?P\.?$", re.IGNORECASE)
RE_SINGLE = re.compile(r"-? *Single$", re.IGNORECASE)
RE_SOUNDTRACK = re.compile(r"original.*soundtrack", re.IGNORECASE)

# Release type mappings
RECORD_TYPES = {
    "album": "Album",
    "ep": "EP",
    "single": "Single",
}

# Genre splitting for combined genres
SPLIT_GENRES = {
    "Pop Rock": {"Pop", "Rock"},
    "Pop/Rock": {"Pop", "Rock"},
    "Pop/Rock→Pop": {"Pop", "Rock"},
    "Pop/Rock→Rock": {"Pop", "Rock"},
    "Rock & Pop": {"Rock", "Pop"},
    "Pop/Rock→Rock→Alternatif et Indé": {"Rock", "Pop", "Indie", "Alternative"},
    "Alternatif et Indé": {"Indie", "Alternative"},
    "Rock/Pop": {"Rock", "Pop"},
    "Hip-Hop/Rap": {"Hip Hop", "Rap"},
    "Hip Hop/Rap": {"Hip Hop", "Rap"},
    "R&B/Soul": {"R&B", "Soul"},
    "Soul/R&B": {"Soul", "R&B"},
    "Indie Pop": {"Indie", "Pop"},
    "Indie Rock": {"Indie", "Rock"},
    "Electronic/Dance": {"Electronic", "Dance"},
    "Dance/Electronic": {"Dance", "Electronic"},
    "House/Techno": {"House", "Techno"},
    "Techno/House": {"Techno", "House"},
    "Folk/Country": {"Folk", "Country"},
    "Country/Folk": {"Country", "Folk"},
    "Jazz Funk": {"Jazz", "Funk"},
    "Funk/Jazz": {"Funk", "Jazz"},
    "Classical/Contemporary": {"Classical", "Contemporary"},
    "Blues Rock": {"Blues", "Rock"},
    "Rock Blues": {"Rock", "Blues"},
    "Ambient/Experimental": {"Ambient", "Experimental"},
    "Trip Hop": {"Trip Hop"},  # Keep this one as is
    "Music": {},  # Skip generic 'Music' genre
}

# Common edition keywords to look for
EDITION_KEYWORDS = {
    "Live", "Remaster", "Deluxe", "Edition", "Version", 
    "Anniversary", "Expanded", "Special", "Collector", 
    "Extended", "Director", "Cut", "Bonus", "Acoustic",
    "Demo", "Mix", "Original", "Alternate", "Vinyl"
}

#------------------------------------------------------------------------------
# Helper Functions
#------------------------------------------------------------------------------

def safe_get(d, keys, default=None):
    """
    Safely access nested dictionary values without raising KeyError.
    """
    if not isinstance(d, dict):
        return default
        
    result = d
    for key in keys:
        if not isinstance(result, dict) or key not in result:
            return default
        result = result.get(key)
    
    # Return default for None/empty values
    return result if result else default

#------------------------------------------------------------------------------
# Qobuz Metadata Scraper Class
#------------------------------------------------------------------------------

class Scraper(QobuzBase, MetadataMixin):
    """
    Qobuz metadata scraper that implements MetadataMixin abstract methods
    and uses QobuzBase for authentication and API access.
    """
    
    #--------------------------------------------------------------------------
    # Core API and Connection Methods
    #--------------------------------------------------------------------------
    
    async def create_soup(self, url):
        """
        Override create_soup to properly get the album data from the API.
        This method uses the QobuzBase get_json method.
        """
        try:
            rls_id = self.regex.match(url)[1]
        except (TypeError, IndexError) as err:
            raise ScrapeError(f"Failed to extract release ID from URL: {url}") from err
            
        try:
            response = await self.get_json(
                self.release_format.format(rls_id=rls_id),
                headers=self.headers
            )
        except Exception as err:
            raise ScrapeError(f"Failed to fetch data from Qobuz API: {str(err)}") from err
        
        if "error" in response:
            raise ScrapeError(f"Qobuz API error: {response['error']}")
            
        # Verify basic required fields exist
        if not response.get("title"):
            raise ScrapeError("Missing required field 'title' in Qobuz API response")
            
        return response
    
    @staticmethod
    def format_url(rls_id=None, rls_name=None, url=None):
        """Format a URL for the release based on ID or original URL."""
        if url:
            return url
        return f"https://www.qobuz.com/album/-/{rls_id}"
    
    #--------------------------------------------------------------------------
    # Required Metadata Methods (from base.MetadataMixin)
    #--------------------------------------------------------------------------
    
    def parse_release_title(self, soup):
        """Parse the release title from the API response."""
        return RE_FEAT.sub("", soup["title"])
    
    def parse_release_group_year(self, soup):
        return RE_YEAR.search(safe_get(soup, ["release_date_original"])).group(1)
    
    def parse_release_year(self, soup):
        if self.parse_edition_title(soup):
            if any(keyword in self.parse_edition_title(soup) for keyword in EDITION_KEYWORDS):
                return RE_YEAR.search(safe_get(soup, ["copyright"])).group(1)
            else:
                return None
        else:
            return self.parse_release_group_year(soup)
            
    def parse_release_label(self, soup):
        """
        Parse label name, marking as Self-Released if artist name appears in label.
        Also attempts to extract label from copyright information.
        """
        # Try to get label directly from API
        label = safe_get(soup, ["label", "name"])
        
        # If not available, try to extract from copyright
        if not label:
            copyright_text = soup.get("copyright", "")
            if copyright_text:
                extracted_label = parse_copyright(copyright_text)
                if extracted_label:
                    label = extracted_label
        
        # If still no label, return None
        if not label:
            return None
            
        # Check if this is likely self-released
        artist = safe_get(soup, ["artist", "name"])
        if artist and artist.lower() in label.lower():
            return "Self-Released"
            
        return label
            
    def parse_tracks(self, soup):
        """
        Parse track information from the API response.
        """
        tracks = defaultdict(dict)
        
        # Get main artist from release
        main_artist = safe_get(soup, ["artist", "name"])
        
        # Get any featured artists at the release level
        featured_artists = []
        artist_list = soup.get("artists", [])
        if isinstance(artist_list, list):
            for artist_data in artist_list:
                artist_name = artist_data.get("name")
                roles = artist_data.get("roles", [])
                # Check for any variation of featured artist role
                if artist_name and artist_name != main_artist and any("feat" in role.lower() for role in roles):
                    featured_artists.append(artist_name)
        
        track_items = safe_get(soup, ["tracks", "items"], [])
        if not isinstance(track_items, list):
            return {}
            
        for track in track_items:
            disc_number = str(track.get("media_number", 1))
            track_number = str(track.get("track_number", 1))
            
            # Collect artists with their roles
            artists = self._collect_track_artists(track, main_artist, featured_artists)
            
            # Parse track title with version
            title = track.get("title", "")
            if version := track.get("version"):
                title = f"{title} ({version})"

            # Create track entry
            tracks[disc_number][track_number] = self.generate_track(
                trackno=track_number,
                discno=disc_number,
                artists=artists,
                title=title,
                isrc=track.get("isrc"),
                explicit=track.get("parental_warning", False)
            )
            
        return dict(tracks)
        
    #--------------------------------------------------------------------------
    # Optional Metadata Methods
    #--------------------------------------------------------------------------
    
    def parse_cover_url(self, soup):
        """
        Parse the cover URL from the API response.
        Qobuz already compresses their images, so using the large image is best.
        """
        return safe_get(soup, ["image", "large"])
        
    def parse_edition_title(self, soup):
        """
        Extract edition information from the API response.
        Prioritizes Qobuz's explicit version field, then checks title for edition keywords.
        """
        # First check if Qobuz provides a specific version field
        if version := soup.get("version"):
            return version
        
        # Check for edition information in the title
        title = soup.get("title", "")
        if not title:
            return None
            
        # Extract text in parentheses at the end of the title
        match = RE_EDITION.search(title)
        if not match:
            return None
            
        edition_text = match.group(1).strip()
        
        # Only return if it contains a known edition keyword
        edition_lower = edition_text.lower()
        if any(keyword.lower() in edition_lower for keyword in EDITION_KEYWORDS):
            return edition_text
            
        return None
    
    def parse_release_date(self, soup):
        """Parse the release date from the API response."""
        return soup.get("release_date_original")
    
    def parse_release_catno(self, soup):
        """Parse the catalog number from the API response."""
        return str(soup.get("id")).upper() if soup.get("id") else None
        
    def parse_release_type(self, soup):
        """
        Parse the release type from the API response.
        Returns a standardized release type based on Qobuz data.
        """
        # Try to get directly from Qobuz's mapping first
        qobuz_type = soup.get("release_type", "").lower()
        if qobuz_type in RECORD_TYPES:
            return RECORD_TYPES[qobuz_type]
            
        # Check the title for explicit indicators
        title = soup.get("title", "")
        if RE_EP.search(title):
            # Remove the suffix from the title
            soup["title"] = RE_EP.sub("", title)
            return "EP"
        elif RE_SINGLE.search(title):
            # Remove the suffix from the title
            soup["title"] = RE_SINGLE.sub("", title)
            return "Single"
        elif RE_SOUNDTRACK.search(title):
            return "Soundtrack"
            
        # Simple fallback based on track count
        track_count = soup.get("tracks_count", 0)
        if track_count < 3:
            return "Single"
        if track_count < 5:
            return "EP"
            
        # Default to Album
        return "Album"
    
    def parse_genres(self, soup):
        """Parse the genres from the API response."""
        genres = {g for gs in soup.get("genres_list") for g in SPLIT_GENRES.get(gs, [gs])}
        return genres
        
    def parse_upc(self, soup):
        """Parse the UPC from the API response."""
        return soup.get("upc")
        
    def parse_comment(self, soup):
        """Parse any comments from the API response."""
        return None
        #return soup.get("description") # This will return release notes in html format (better to keep disabled)
        
    # Override base.py's determine_rls_type to respect Qobuz's explicit release type.
    def determine_rls_type(self, data):
        """
        Override the base class's determine_rls_type method to prioritize 
        the release type that was explicitly provided by Qobuz.
        """
        # If Qobuz provided an explicit release type, respect it and keep it
        if data["rls_type"] in ["Album", "EP", "Single", "Soundtrack", "Compilation"]:
            return data["title"], data["rls_type"]
            
        # Otherwise, fall back to the base class's heuristics
        return super().determine_rls_type(data)
    
    #--------------------------------------------------------------------------
    # Helper Methods
    #--------------------------------------------------------------------------
    
    def _collect_track_artists(self, track, main_artist, featured_artists):
        """
        Helper method to collect artists with their roles for a track.
        
        This method applies several strategies to identify and categorize artists:
        1. Uses the track's performer as main artist when available
        2. Falls back to the release's main artist if no track performer
        3. Parses the performers string for additional artists and their roles
        4. Adds any release-level featured artists not already present
        5. Extracts featured artists from the track title using RE_FEAT
        
        Args:
            track (dict): Track data from Qobuz API
            main_artist (str): Main artist name from the release
            featured_artists (list): Featured artists identified at release level
            
        Returns:
            list: List of tuples (artist_name, role) where role is 'main' or 'guest'
        """
        artists = []
        seen_artists = set()  
        # Track artists we've already processed
        
        # 1. Add track's performer as main artist if available
        if performer := safe_get(track, ["performer", "name"]):
            artists.append((performer, "main"))
            seen_artists.add(performer)
        # 2. Otherwise use the release's main artist
        elif main_artist:
            artists.append((main_artist, "main"))
            seen_artists.add(main_artist)
                
        # 3. Parse the performers string for additional artists
        if "performers" in track:
            performers_str = track["performers"]
            
            # Split by segments first
            for artist_segment in performers_str.split(" - "):
                parts = artist_segment.split(", ")
                if len(parts) >= 2:
                    artist_name = parts[0].strip()
                    roles = parts[1:]
                    
                    # Skip artists we already have
                    if artist_name in seen_artists:
                        continue
                        
                    # Check roles: prioritize FeaturedArtist over other roles
                    if "FeaturedArtist" in roles:
                        artists.append((artist_name, "guest"))
                        seen_artists.add(artist_name)
                
        # 4. Add any release-level featured artists not already added
        for guest in featured_artists:
            if guest not in seen_artists:
                artists.append((guest, "guest"))
                seen_artists.add(guest)
            
        # 5. Check for "feat." in title and add those artists as guests
        title = track.get("title", "")
        if feat := RE_FEAT.search(title):
            for artist in re_split(feat[1]):
                guest_name = unescape(artist)
                if guest_name not in seen_artists:
                    artists.append((guest_name, "guest"))
                    seen_artists.add(guest_name)
                    
        return artists
        