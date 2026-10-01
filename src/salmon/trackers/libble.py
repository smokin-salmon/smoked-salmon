import re
from typing import Any

from bs4 import BeautifulSoup

from salmon import cfg
from salmon.common import UploadFiles
from salmon.errors import LoginError, RequestFailedError
from salmon.trackers.base import BaseGazelleApi

_AUTHKEY_RE = re.compile(r'var authkey = "([^"]+)"')
_PASSKEY_RE = re.compile(r"passkey=([0-9a-f]{32})")
_TORRENTID_RE = re.compile(r"torrents\.php\?[^\"']*?torrentid=(\d+)")
_TORRENT_ROW_RE = re.compile(r"torrent_(\d+)")
_GROUPID_RE = re.compile(r"(?:torrents\.php\?id=|upload\.php\?groupid=)(\d+)")
_YEAR_RE = re.compile(r"\[(\d{4})\]")
_PAD_RE = re.compile(r"\[pad(?:=[^\]]*)?\](.*?)\[/pad\]", re.IGNORECASE | re.DOTALL)
_BARE_IMG_RE = re.compile(r"\[img=([^\[\]]+)\]", re.IGNORECASE)
_CODE_RE = re.compile(r"\[code\](.*?)\[/code\]", re.IGNORECASE | re.DOTALL)
_SIZE_OPEN_RE = re.compile(r"\[size(?:=[^\]]*)?\]", re.IGNORECASE)
_SIZE_CLOSE_RE = re.compile(r"\[/size\]", re.IGNORECASE)


def convert_bbcode_for_libble(text: str | None) -> str | None:
    """Rewrite RED/OPS-dialect BBCode into tags Libble renders.

    Libble supports b/i/s/u/color/pre/quote/align/url/img/hide/aud/spotify
    but not pad, bare img=, code, or size (size renders on RED/OPS only).
    """
    if not text:
        return text
    text = _PAD_RE.sub(r"\1", text)
    text = _BARE_IMG_RE.sub(r"[img]\1[/img]", text)
    text = _CODE_RE.sub(r"[pre]\1[/pre]", text)
    text = _SIZE_OPEN_RE.sub("", text)
    text = _SIZE_CLOSE_RE.sub("", text)
    return text


_FORMATS = {
    "MP3",
    "FLAC",
    "Ogg",
    "APE",
    "AAC",
    "WMA",
    "MP2",
    "AC3",
    "WavPack",
    "DTS",
    "ALAC",
}
_MEDIA = {
    "CD",
    "CDr",
    "DVD",
    "Vinyl",
    "WEB",
    "Soundboard",
    "DAT",
    "Radio",
    "SACD",
    "Blu-ray",
    "Cassette",
}
_SKIP_TOKENS = {"log", "cue", "scene", "freeleech!", "neutral!", "reported", "inactive!"}


def _parse_extrainfo(text: str) -> dict[str, str]:
    """Split a details/browse ExtraInfo string into format/encoding/media.

    e.g. "FLAC / Lossless / Log (100%) / Cue / CD" -> FLAC/Lossless/CD.
    """
    fmt, enc, media = "", "", ""
    for part in re.split(r"\s*/\s*", re.sub(r"^[\s»\t]+|[\s»\t]+$", "", re.sub(r"\s+", " ", text))):
        token = re.sub(r"\s*\(\d+%\)\s*", "", part).strip()
        if not token:
            continue
        low = token.lower()
        if token in _FORMATS and not fmt:
            fmt = token
        elif token in _MEDIA and not media:
            media = token
        elif low in _SKIP_TOKENS or re.fullmatch(r"\d+%", token):
            continue
        elif not enc:
            enc = token
    return {"format": fmt, "encoding": enc, "media": media}


class LibbleApi(BaseGazelleApi):
    def __init__(self):
        self.site_code = "LIB"
        self.base_url = "https://libble.me"
        self.tracker_url = "http://tracker.libble.me:34000"
        self.site_string = "Libble"

        if cfg.tracker.lib:
            lib_cfg = cfg.tracker.lib
            if lib_cfg.dottorrents_dir:
                self.dot_torrents_dir = lib_cfg.dottorrents_dir
            else:
                self.dot_torrents_dir = cfg.directory.dottorrents_dir

            self.cookie = lib_cfg.session
            # cookie-only, Libble has no ajax API (ajax.php 403s)

        super().__init__()

        # Libble types from upload.php: +Video/Box Set/Collection,
        # no Anthology/Demo/Split/DJ Mix/Concert Recording
        self.release_types = {
            "Album": 1,
            "Soundtrack": 3,
            "EP": 5,
            "Compilation": 7,
            "Single": 9,
            "Live album": 11,
            "Remix": 13,
            "Bootleg": 14,
            "Interview": 15,
            "Mixtape": 16,
            "Video": 17,
            "Box Set": 18,
            "Collection": 19,
            "Unknown": 21,
            # Aliases for types valid elsewhere but missing on Libble, mapped to closest equivalent.
            "Anthology": 7,  # single-artist compilation -> Compilation
            "DJ Mix": 16,  # mixed set -> Mixtape
            "Concert Recording": 11,  # live recording -> Live album
            "Demo": 21,
            "Split": 21,
        }

    async def authenticate(self) -> None:
        """Scrape authkey/passkey from upload.php HTML (no ajax API)."""
        resp = await self._request("GET", self.base_url + "/upload.php", timeout_secs=10, needs_authkey=False)
        if "userinfo" not in resp.text or "login.php" in resp.url:
            raise LoginError("Logged out of Libble (session cookie missing/expired)")
        authkey = _AUTHKEY_RE.search(resp.text)
        passkey = _PASSKEY_RE.search(resp.text)
        if not authkey or not passkey:
            raise LoginError("Could not scrape authkey/passkey from Libble upload page")
        self.authkey = authkey.group(1)
        self.passkey = passkey.group(1)
        self._authenticated = True

    async def api_call(self, action: str, params: dict[str, Any] | None = None) -> dict:
        """HTML-backed subset of the Gazelle API (Libble has no ajax.php).

        Supports browse/torrentgroup/torrent shapes used by dupe check,
        group confirm, timeout recovery and spectral append.
        """
        await self.ensure_authenticated()
        params = params or {}
        if action == "browse":
            return await self._html_browse(str(params.get("searchstr", "")))
        if action == "torrentgroup":
            return await self._html_torrentgroup(int(params["id"]))
        if action == "torrent":
            return await self._html_torrent(int(params["id"]))
        raise RequestFailedError(f"Libble has no API action '{action}'")

    async def _html_browse(self, searchstr: str) -> dict:
        """Search via torrents.php?searchstr=, return browse-shaped results."""
        resp = await self._request(
            "GET", self.base_url + "/torrents.php", params={"searchstr": searchstr}, timeout_secs=10
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for row in soup.select("tr.group"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            content = cells[2]
            link = content.find("a", href=re.compile(r"torrents\.php\?id=\d+"))
            if not link:
                continue
            gid = re.search(r"id=(\d+)", str(link.get("href", "")))
            if not gid:
                continue
            group_id = int(gid.group(1))
            group_name = link.get_text(strip=True)
            full = content.get_text(" ", strip=True)
            year = _YEAR_RE.search(full)
            artist = full.split(group_name)[0].strip(" -") if group_name in full else ""
            tags = [a.get_text(strip=True) for a in content.select("div.tags a")]
            torrents = []
            for trow in soup.select(f"tr.group_torrent.groupid_{group_id}"):
                tlink = trow.find("a", href=_TORRENTID_RE)
                if not tlink:
                    continue
                tid = _TORRENTID_RE.search(str(tlink.get("href", "")))
                if not tid:
                    continue
                info = _parse_extrainfo(tlink.get_text(" ", strip=True))
                try:
                    file_count = int(trow.find_all("td")[1].get_text(strip=True))
                except (IndexError, ValueError):
                    file_count = 0
                torrents.append(
                    {
                        "id": int(tid.group(1)),
                        "media": info["media"],
                        "format": info["format"],
                        "encoding": info["encoding"],
                        "remastered": False,
                        "remasterYear": 0,
                        "remasterTitle": "",
                        "remasterRecordLabel": "",
                        "remasterCatalogueNumber": "",
                        "fileCount": file_count,
                    }
                )
            results.append(
                {
                    "groupId": group_id,
                    "artist": artist,
                    "groupName": group_name,
                    "groupYear": int(year.group(1)) if year else 0,
                    "releaseType": "Unknown",
                    "tags": tags,
                    "torrents": torrents,
                }
            )
        return {"results": results}

    async def _html_torrentgroup(self, group_id: int) -> dict:
        """Scrape torrents.php?id= into torrentgroup-shaped data."""
        resp = await self._request("GET", self.base_url + "/torrents.php", params={"id": group_id}, timeout_secs=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        title = (soup.title.get_text() if soup.title else "").replace(":: Libble.me", "").strip()
        year = _YEAR_RE.search(title)
        name = _YEAR_RE.sub("", title).strip()
        artists: list[str] = []
        for cls in ("artist_main", "artist_guest", "artists_remix"):
            for li in soup.select(f"li.{cls} a"):
                href = str(li.get("href", ""))
                if "artist.php" in href:
                    artists.append(li.get_text(strip=True))
        if " - " in name and not artists:
            maybe_artists, _, maybe_name = name.partition(" - ")
            if maybe_name:
                artists, name = [maybe_artists], maybe_name
        elif artists and name.startswith(artists[0] + " - "):
            name = name[len(artists[0]) + 3 :]
        group = {
            "id": group_id,
            "name": name or f"Group {group_id}",
            "year": int(year.group(1)) if year else 0,
            "recordLabel": "",
            "catalogueNumber": "",
            "musicInfo": {"artists": [{"name": a} for a in artists]},
        }
        torrents = []
        remastered, remaster_year, remaster_title = False, 0, ""
        for row in soup.select("table.torrent_table tr"):
            edition = row.select_one("td.edition_info")
            if edition:
                text = edition.get_text(" ", strip=True)
                if text.startswith("Original Release"):
                    remastered, remaster_year, remaster_title = False, 0, ""
                elif text.startswith("Unknown Release"):
                    remastered, remaster_year, remaster_title = True, 0, ""
                else:
                    remastered = True
                    ym = re.match(r"(\d{4})\s*(?:-\s*)?(.*)", text)
                    remaster_year = int(ym.group(1)) if ym else 0
                    remaster_title = ym.group(2).split("/")[0].strip() if ym else ""
                continue
            toggle = row.find("a", onclick=re.compile(r"\$\('#torrent_\d+'\)"))
            if not toggle:
                continue
            tid = re.search(r"torrent_(\d+)", str(toggle.get("onclick", "")))
            if not tid:
                continue
            info = _parse_extrainfo(toggle.get_text(" ", strip=True))
            torrents.append(
                {
                    "id": int(tid.group(1)),
                    "media": info["media"],
                    "format": info["format"],
                    "encoding": info["encoding"],
                    "remastered": remastered,
                    "remasterYear": remaster_year,
                    "remasterTitle": remaster_title,
                    "remasterRecordLabel": "",
                    "remasterCatalogueNumber": "",
                    "fileCount": 0,
                    "description": "",
                }
            )
        artist_str = " ".join(artists)
        return {
            "group": group,
            "torrents": torrents,
            "groupId": group_id,
            "groupName": group["name"],
            "groupYear": group["year"],
            "artist": artist_str,
        }

    async def _html_torrent(self, torrent_id: int) -> dict:
        """Resolve a torrent via ?torrentid= redirect, return torrent-shaped data."""
        group_id = await self.get_redirect_torrentgroupid(torrent_id)
        if group_id is None:
            raise RequestFailedError(f"Libble torrent {torrent_id} not found")
        group = await self._html_torrentgroup(group_id)
        for t in group["torrents"]:
            if t["id"] == torrent_id:
                return {
                    "torrent": {
                        "remasterYear": t["remasterYear"],
                        "remasterTitle": t["remasterTitle"],
                        "remasterRecordLabel": t["remasterRecordLabel"],
                        "remasterCatalogueNumber": t["remasterCatalogueNumber"],
                        "format": t["format"],
                        "encoding": t["encoding"],
                        "media": t["media"],
                        "description": t.get("description", ""),
                        "fileCount": t.get("fileCount", 0),
                    }
                }
        raise RequestFailedError(f"Libble torrent {torrent_id} not found in group {group_id}")

    def parse_most_recent_torrent_and_group_id_from_group_page(self, text: str) -> tuple[int, int]:
        """Libble redirects uploads to torrents.php?id=G; take max torrent id."""
        tids = [int(m) for m in _TORRENTID_RE.findall(text)]
        if not tids:
            tids = [int(m) for m in _TORRENT_ROW_RE.findall(text)]
        gids = [int(m) for m in _GROUPID_RE.findall(text)]
        if not tids or not gids:
            raise TypeError("Could not parse torrent/group id from Libble group page")
        return max(tids), gids[0]

    async def upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent via upload.php (cookie session only)."""
        data = dict(data)
        # Libble validates type against category names and routes on posted.
        data["type"] = "Music"
        data["posted"] = True
        data.pop("submit", None)
        if data.get("album_desc"):
            data["album_desc"] = convert_bbcode_for_libble(data["album_desc"])
        if data.get("release_desc"):
            data["release_desc"] = convert_bbcode_for_libble(data["release_desc"])
        return await self.site_page_upload(data, files)
