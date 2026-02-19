__version__ = "0.9.7.4"

"""
Changelog for version 0.9.7.4 (2025-09-26):

## What's Changed
* Add support for Python 3.12+ and use dependabot for dependency updates by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/172
* Fix ruTorrent connection URL parsing and add connection test by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/173
* Fix multiple issues that cause program crashes by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/174
* Fix spectral folder rename crash and arrow keys in docker by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/176
* Bump ruff from 0.13.0 to 0.13.1 by @dependabot[bot] in https://github.com/smokin-salmon/smoked-salmon/pull/179
* Bump click from 8.2.1 to 8.3.0 by @dependabot[bot] in https://github.com/smokin-salmon/smoked-salmon/pull/178
* Bump pyperclip from 1.9.0 to 1.10.0 by @dependabot[bot] in https://github.com/smokin-salmon/smoked-salmon/pull/177
* Decode username and password when parsing torrent client credentials by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/181

## New Contributors
* @dependabot[bot] made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/179

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.7.3...0.9.7.4
"""

__version__ = "0.9.7.3"

"""
Changelog for version 0.9.7.3 (2025-09-04):

### ⚙ New Options

#### Default Editor Configuration
New configuration option for setting your preferred text editor:
```toml
[upload]
default_editor = "nano"  # Can be set to "vim", "emacs", or any editor in PATH
```

#### Enhanced Seedbox Configuration
New seedbox configuration options for better torrent client control:
```toml
[[seedbox]]
directory = "/downloads"  # Now overrides the directory provided to torrent client
add_paused = true  # Add torrents in paused state
```
- **`add_paused`**: Control whether torrents are added in paused or active state
- **Enhanced `directory`**: Now overrides the download path provided to the torrent client, useful when the torrent client has its own path mapping configuration

#### Integrity check is now performed by default (like log check and MQA check)
- Use `--skip-integrity-check` flag to skip the integrity verification of audio files

### 📝 Notes for Windows Users

SoX on Windows currently has issues with UTF-8 file paths. If you need UTF-8 path support, you can fix this using the following PowerShell commands. This fix is provided by [DevYukine](https://github.com/DevYukine) - many thanks to him!

```powershell
$soxDir = $((Get-Command sox).Source | Split-Path)
$zipPath = Join-Path -Path $soxDir -ChildPath "sox_windows_fix.zip"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/DevYukine/red_oxide/master/.github/dependency-fixes/sox_windows_fix.zip" -OutFile $zipPath
Expand-Archive -Path $zipPath -DestinationPath $soxDir -Force
regedit "$soxDir\\PreferExternalManifest.reg"
Remove-Item $zipPath
```

Enjoy SoX working with UTF-8 paths! 🎉

### 🔍 Metadata Sources and Connection Testing

Added **metadata sources and seedbox connection testing**:
- Use `checkconf -m` to test metadata source connections (Discogs, Tidal, Qobuz)
- Use `checkconf -s` to test seedbox connections

### 🐛 Various Fixes and Improvements

- **Transcoding Path Names**: Fixed bug where MP3 transcoding output folders would add [320] or [V0] separately - now the output folder name follows the **FOLDER_TEMPLATE** configuration
- **Range Rip CRC Calculation**: Added **CRC calculation for range rip logs** - automatically detects range rip type log files and calculates CRC by concatenating individual track files
- **Upload Fix**: Fixed site_page_upload failure when uploading to existing group when only `session` is specified without `api_key`

## What's Changed
* Updates the transcoding path name generation logic to follow the FOLDER_TEMPLATE by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/162
* Allow direct selection from recent upload results by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/163
* Add an option in the configuration file for a default editor by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/164
* Improve path handling logic in LocalUploader by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/165
* Add metadata sources and seedbox connection testing by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/166
* Add CRC calculation for range rip logs and various fixes by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/169


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.7.2...0.9.7.3
"""

__version__ = "0.9.7.2"

"""
Changelog for version 0.9.7.2 (2025-08-23):

## What's Changed
* Add illegal folder detection and various fixes by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/161


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.7.1...0.9.7.2
"""

__version__ = "0.9.7.1"

"""
Changelog for version 0.9.7.1 (2025-08-21):

## What's Changed
* Multiple bug fixes and improvements by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/155
* Resolve Linux input issues by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/157
* Skip transcoding when target folder already exists by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/159
* Update Docker documentation and default config paths by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/160


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.7...0.9.7.1
"""

__version__ = "0.9.7"

"""
Changelog for version 0.9.7 (2025-08-20):

## ⚠️ Breaking Change: Torrent Client Injection

This release introduces **breaking changes** to the torrent client integration system.

### 🔄 New Seedbox Upload System

The legacy qBittorrent and ruTorrent injection modules have been replaced with a unified seedbox upload system that supports:
- **Multiple torrent clients**: qBittorrent, Transmission, Deluge, ruTorrent
- **Multi-device deployment**: Upload files and torrents to multiple seedboxes simultaneously

#### ✅ Migration Guide

**Previous local injection is now considered a "local" seedbox**. You need to update your configuration:

1. **Remove old settings** (these are no longer supported):
   - Any qBittorrent or ruTorrent specific injection settings

2. **Add new seedbox configuration** to your `config.toml`:
   ```toml
   # Example: Local seedbox (replaces old injection)
   [[seedbox]]
   name = "local"
   enabled = true
   type = "local"
   directory = "/path/to/your/download/folder"
   torrent_client = "transmission+http://username:password@localhost:9091"
   label = "smoked-salmon"
   
   # Example: Remote seedbox via rclone
   [[seedbox]]
   name = "remote-seedbox"
   enabled = true
   type = "rclone"
   url = "nas"  # Name of remote in rclone
   directory = "/downloads"
   torrent_client = "qbittorrent+http://username:password@192.168.1.2:8080"
   flac_only = false
   extra_args = ["--checksum", "-P"]
   label = "smoked-salmon"
   ```

3. **Enable seedbox uploading**:
   ```toml
   [upload]
   upload_to_seedbox = true
   ```

### 🎵 New Automatic Transcoding Feature

This release introduces **automatic transcoding and upload** functionality:
- Automatically detects all possible transcode formats after upload
- Uploads all transcoded versions automatically  
- **Lossy master report**: If source is lossy master, all transcoded torrents are automatically reported as lossy master
- **No configuration required**: Works through interactive prompts during upload

### 🎯 New Tracker Support

Added support for **DICMusic** tracker. To configure:
```toml
[tracker.dic]
session = 'get-from-site-cookie'
```

### 🖼️ Automatic Cover Compression

New feature to automatically compress embedded cover images in FLAC files:
```toml
[image]
auto_compress_cover = true  # Set to true to enable
```

### ⚠️ Important Notes

- **No automatic migration** from old injection settings - manual configuration required
- The **Wiki** is currently outdated and will be updated in a future release
- **Please report** if you encounter crashes or strange behavior with these new features

## What's Changed
* Merge seedbox uploading module from smoked-salmon-oasis by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/118
* Merge automatic transcoding and uploading module from smoked-salmon-oasis by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/151


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.6.1...0.9.7
"""

__version__ = "0.9.6.1"

"""
Changelog for version 0.9.6.1 (2025-08-18):

This version fixes the issue on Windows platform where sometimes input would not be confirmed immediately and required multiple confirmations.

Oxipng and cambia no longer need to be installed as dependencies. All external dependencies required by the current program can now be installed directly using the system package manager. 

## What's Changed
* Format code with ruff by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/146
* Replace external oxipng with pyoxipng by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/147
* Replace external cambia with pycambia library by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/149
* Update README.md for docker usage by @maksii in https://github.com/smokin-salmon/smoked-salmon/pull/106
* Resolve async input handling issues on Windows by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/150

## New Contributors
* @maksii made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/106

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.6...0.9.6.1
"""

__version__ = "0.9.6"

"""
Changelog for version 0.9.6 (2025-07-21):

As of this version, smoked-salmon should now be able to run natively on Windows. This release features a refactored transcoding and downconverting module based on m3ercat, and other parts that were incompatible with Windows have also been refactored.

## What's Changed
* Add overwrite=True arg to torf write to avoid WriteError when file exists by @resu-detcader in https://github.com/smokin-salmon/smoked-salmon/pull/119
* Fix hardlink option parity being wacked up by @resu-detcader in https://github.com/smokin-salmon/smoked-salmon/pull/124
* Merge transcoding and downconverting module from smoked-salmon-oasis by @KyokoMiki in https://github.com/smokin-salmon/smoked-salmon/pull/105
* Add health command to show state of dependencies by @resu-detcader in https://github.com/smokin-salmon/smoked-salmon/pull/125
* Two fixes in foldername.py by @resu-detcader in https://github.com/smokin-salmon/smoked-salmon/pull/130
* Make default tracker field not required by @resu-detcader in https://github.com/smokin-salmon/smoked-salmon/pull/133
* Update README.md by @bznein in https://github.com/smokin-salmon/smoked-salmon/pull/135
* Renamed host and port in config by @btTeddy in https://github.com/smokin-salmon/smoked-salmon/pull/136

## New Contributors
* @KyokoMiki made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/105
* @bznein made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/135
* @btTeddy made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/136

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.5.1...0.9.6
"""

__version__ = "0.9.5.1"

"""
Changelog for version 0.9.5.1 (2025-06-15):

Fixed the error that occurred during torrent creation caused by switching from dottorrent to torf.

## What's Changed
* Bugfixes from toml pull request by @resu-detcader in https://github.com/smokin-salmon/smoked-salmon/pull/113
* Fix 'beautifulsoup4' dependency by @ambroisie in https://github.com/smokin-salmon/smoked-salmon/pull/112
* Add database location migration message by @resu-detcader in https://github.com/smokin-salmon/smoked-salmon/pull/116

## New Contributors
* @ambroisie made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/112

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.5...0.9.5.1
"""

__version__ = "0.9.5"

"""
Changelog for version 0.9.5 (2025-06-14):

## ⚠️ Breaking Change: Configuration File Format

This release introduces a **major breaking change**: we are transitioning away from the legacy `config.py` configuration file.

### 🔁 New Configuration System

Settings are now stored in a **TOML** file located at: `~/.config/smoked-salmon/config.toml`
There is **no automatic migration** from the old `config.py`, so you will need to manually migrate your settings.

#### ✅ Recommended Migration Path

Start from the [default configuration file](https://github.com/smokin-salmon/smoked-salmon/blob/master/data/config.default.toml).  
This ensures you're aligned with the latest structure and includes all newly introduced options.

### 📖 Documentation

- The **README** has been updated to reflect this change.
- The **Wiki** is currently outdated and will be updated in a future release.

We understand this requires extra effort and appreciate your patience as we make configuration cleaner and more maintainable moving forward.

## What's Changed
* Fix rare case of lost cover when uploading on multiple trackers by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/102
* Move to toml configuration by @resu-detcader in https://github.com/smokin-salmon/smoked-salmon/pull/95

## New Contributors
* @resu-detcader made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/95

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.4.9...0.9.5
"""

__version__ = "0.9.4.9"

"""
Changelog for version 0.9.4.9 (2025-06-02):

## What's Changed
* Fix spectrals folder rename when using TMP_DIR by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/101


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.4.8...0.9.4.9
"""

__version__ = "0.9.4.8"

"""
Changelog for version 0.9.4.8 (2025-06-02):

Please report if you encounter crashes or strange behavior when using `TMP_DIR`. All known issues related to that seem to be fixed for now.

## What's Changed
* Fix spectrals upload when using `TMP_DIR`
* Improve metadata source selections by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/98
  * Fix bandcamp single tracks parsing
  * Allow passing multiple release choices from same metadata provider (useful to provide multiple discogs links)
  * Allow passing custom URLs during metadata selection (will be linked in release description)


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.4.7...0.9.4.8
"""

__version__ = "0.9.4.7"

"""
Changelog for version 0.9.4.7 (2025-06-01):

New options:
`TMP_DIR`: spectrals will be generated in this folder, instead of the album folder. Leave empty to keep the old behavior.
`CLEAN_TMP_DIR`: cleanup the temp folder on each startup. Careful though, this option removes all files from the temp folder, so make sure it's only used by smoked salmon. This option has no effect if `TMP_DIR` is undefined.

## What's Changed
* Add option to use a specific TMP folder for spectrals by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/92
* Add ffmpeg dependency for cambia log checker (docker fix) by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/96
* Fix per-tracker paths being overriden with the default DOTTORRENTS_DIR by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/97


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.4.6...0.9.4.7
"""

__version__ = "0.9.4.6"

"""
Changelog for version 0.9.4.6 (2025-05-28):

## What's Changed
* Fix tag crash due to rename_files refactoring by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/91


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.4.5...0.9.4.6
"""

__version__ = "0.9.4.5"

"""
Changelog for version 0.9.4.5 (2025-05-28):

Fix docker build

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.4.4...0.9.4.5
"""

__version__ = "0.9.4.4"

"""
Changelog for version 0.9.4.4 (2025-05-28):

CD uploads will now automatically check for logs (score, edited logs, matching CRC with files).
You can pass `--skip-log-check` to bypass that check.

## What's Changed
* Change logchecker to more recent Cambia by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/89


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.4.3...0.9.4.4
"""

__version__ = "0.9.4.3"

"""
Changelog for version 0.9.4.3 (2025-05-28):

Added two new image hosters: oeimg and ptscreens. You will need to register on their site, and provide your API Key in your `config.py` file (`OEIMG_KEY` and `PTSCREENS_KEY`).
Also removed imgur as an option, due to its autoremoval policy making it unsuitable for this use case.

## What's Changed
* Add new image providers by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/87


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.4.2...0.9.4.3
"""

__version__ = "0.9.4.2"

"""
Changelog for version 0.9.4.2 (2025-05-27):

## What's Changed
* Fix Juno feat artists parsing by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/86
* Fix uploading tracks with no album tags (crash)


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.4.1...0.9.4.2
"""

__version__ = "0.9.4.1"

"""
Changelog for version 0.9.4.1 (2025-05-27):

## What's Changed
* Use beatport track_id instead of label_track_identifier by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/85


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.4...0.9.4.1
"""

__version__ = "0.9.4"

"""
Changelog for version 0.9.4 (2025-05-27):

Big news: beatport scraping is back! Please report any strangeness (if possible with the associated link) that you may encounter.

## What's Changed
* Beatport is back! by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/82
* Improve musicbrainz None catalog numbers by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/83
* Fix transcoding race conditions by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/84


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.3.7...0.9.4
"""

__version__ = "0.9.3.7"

"""
Changelog for version 0.9.3.7 (2025-05-12):

## What's Changed
* Use only lame for transcoding by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/74
* Allow using workdir with docker (-w) by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/75
* Fix strict python version (3.11.x) by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/76

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.3.6.1...0.9.3.7
"""

__version__ = "0.9.3.7"

"""
Changelog for version 0.9.3.7 (2025-05-12):

## What's Changed
* Use only lame for transcoding by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/74
* Allow using workdir with docker (-w) by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/75
* Fix strict python version (3.11) by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/76

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.3.6.1...0.9.3.7
"""

__version__ = "0.9.3.6.1"

"""
Changelog for version 0.9.3.6.1 (2025-05-10):

Technical fix for python 3.11

## What's Changed
* Fix multiline message for python 3.11 by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/73


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.3.6...0.9.3.6.1
"""

__version__ = "0.9.3.6"

"""
Changelog for version 0.9.3.6 (2025-05-09):

Transcoding should now work on docker, thanks to @milkers69 !

## What's Changed
* fix UnboundLocalError for unmatched urls by @asuna42 in https://github.com/smokin-salmon/smoked-salmon/pull/69
* Set python minimum version to 3.11 by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/72
* Fix Transcoding in Docker by @milkers69 in https://github.com/smokin-salmon/smoked-salmon/pull/70

## New Contributors
* @asuna42 made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/69
* @milkers69 made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/70

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.3.5...0.9.3.6
"""

__version__ = "0.9.3.5"

"""
Changelog for version 0.9.3.5 (2025-04-24):

Big update from Audionut! The coolest part: you can now send torrents straight to qBittorrent using its API. No more messing with watched folders or adding them by hand.

Check `config.py.txt` for the new `QBITTORRENT_XXX` settings. With that option, if you’re using Docker, you might not need to map the `.torrents` folder anymore, just use `/app/.torrents` inside the container: torrents will be generated there and injected into qbittorrent. Additional configuration, specific to docker, might be necessary to be able to call the `qbittorrent` API from smoked-salmon container.

## What's Changed
* Add qBitTorrent support by @Audionut in https://github.com/smokin-salmon/smoked-salmon/pull/62
* Add async input compatibility for Windows by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/63
* Allow use recycle bin on Windows by @Audionut in https://github.com/smokin-salmon/smoked-salmon/pull/61
* Update README for docker users by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/66

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.3.4...0.9.3.5
"""

__version__ = "0.9.3.4"

"""
Changelog for version 0.9.3.4 (2025-04-17):

## What's Changed
* Identify Self-Released label correctly when retrieved from base tags by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/57
* Fix crash when folder already exists by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/58


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.3.3...0.9.3.4
"""

__version__ = "0.9.3.3"

"""
Changelog for version 0.9.3.3 (2025-04-16):

We welcome @Audionut to the list of contributors 💯 
He actually already contributed, unknowingly, a good part of the docker image build and the update changelog mechanism ! 💘 

## What's Changed
* Open spectrals in native windows viewer by @Audionut in https://github.com/smokin-salmon/smoked-salmon/pull/55
* Fix docker permissions when ran with non-root user by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/56

## New Contributors
* @Audionut made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/55

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.3.2...0.9.3.3
"""

__version__ = "0.9.3.2"

"""
Changelog for version 0.9.3.2 (2025-04-16):

## What's Changed
* Fix /app permission when running docker as non-root by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/53


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.3.1...0.9.3.2
"""

__version__ = "0.9.3.1"

"""
Changelog for version 0.9.3.1 (2025-04-15):

## What's Changed
* Fix crashes by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/52
  * Fix crash when uploading to an existing group
  * Fix crash when passing -g argument


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.3...0.9.3.1
"""

__version__ = "0.9.3"

"""
Changelog for version 0.9.3 (2025-04-15):

Hardlinks are now used by default when copying files (if source and destination are on the same volume), unless the `DISABLE_HARDLINKS` config is set to `True` (default: `False`).
New `REMOVE_AUTO_DOWNLOADED_COVER_IMAGE` config option (default: `False`).

## What's Changed
* Copy files using hardlinks when possible by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/48
  * If source and destination are on the same volume, smoked-salmon will now default to using hardlinks. That should make copying a lot faster for most setups.
  * Added `DISABLE_HARDLINKS` to force disabling hardlinks usage if set to `True`
* Remove auto generated cover images for scene and improve cover upload  by @digerati-red in https://github.com/smokin-salmon/smoked-salmon/pull/49
  * Cover image will now be included in torrent if missing from source folder, even for existing groups
  * Added `REMOVE_AUTO_DOWNLOADED_COVER_IMAGE` to force delete any downloaded cover image before upload if set to `True`
* Check path length for scene releases before upload by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/45
* Fix metadata timeout by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/44
* Fix docker image run with non privileged user by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/51


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.20...0.9.3
"""

__version__ = "0.9.2.20"

"""
Changelog for version 0.9.2.20 (2025-04-11):

## What's Changed
* Fix crash when pasting recent log url from RED by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/38
* Improve metadata scraping by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/39
  * Fix Tidal guest parsing for edge cases (Like Artist [guest1 & guest2] which was causing a parsing crash)
  * Improve Self-released detection ("no label", labels starting with the main artist name...)
  * Improve (or try to) release_type heuristics (Singles / EP should be better recognized)
  * Upload all spectrals by default (`*`) when lossy master is selected. Keep sample of spectrals (`+`) when not lossy master

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.19...0.9.2.20
"""

__version__ = "0.9.2.19"

"""
Changelog for version 0.9.2.19 (2025-04-10):

There is a new option available for use in config.py: `REMOVE_SOURCE_DIR ` (default value: `False`)
When set to `True`, the source folder will be deleted after it has been copied to the target location.

## What's Changed
* Add option to upload a randomized subset of spectrals (use `+` during spectrals selection, this is the default choice now) by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/34
* Add option to remove old folder after moving to new location by @frwny in https://github.com/smokin-salmon/smoked-salmon/pull/36
* Allow more track data to update if base title does not match meta title by @digerati-red in https://github.com/smokin-salmon/smoked-salmon/pull/30
* Improve metadata combine from sources by @redusys in https://github.com/smokin-salmon/smoked-salmon/pull/32
  * Standardize / deduplicate genres (no more genres with "Funk, Soul, Funk & Soul".
  * Detect remixes from track titles during combine, and add the remixers as artists
  * Improve the whole release type heuristics - Singles/EPs/Albums/Live Albums/Compilations/Anthologies should be better detected now
  * Update trackumbers from preferred metadata source (the one with highest priority, or the one marked as rip source)
  * Update catno only from preferred metadata source (the one with highest priority, or the one marked as rip source) for WEB
  * Detect edition_title from title (and remove it from the actual title). It does impact metadata search, holding on using the edition_title in the searchstr of metadata search for now, until further testing.

## New Contributors
* @frwny made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/36

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.18...0.9.2.19
"""

__version__ = "0.9.2.18"

"""
Changelog for version 0.9.2.18 (2025-04-08):

## What's Changed
* Update Juno Source Artist Selectors by @digerati-red in https://github.com/smokin-salmon/smoked-salmon/pull/29

## New Contributors
* @digerati-red made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/29

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.17...0.9.2.18
"""

__version__ = "0.9.2.17"

"""
Changelog for version 0.9.2.17 (2025-04-07):

## What's Changed
* Fix mp3 upload (fix crash)
* Fix scene uploads: they should now work properly!
* Fix metadata combine (fix crash when metadata choices selection did not include a source)
* Improve track titles updates from metadata

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.16...0.9.2.17
"""

__version__ = "0.9.2.16"

"""
Changelog for version 0.9.2.16 (2025-04-06):

## What's Changed
* Remove leftover debug statement

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.15...0.9.2.16
"""

__version__ = "0.9.2.15"

"""
Changelog for version 0.9.2.15 (2025-04-06):

## Metadata Parsing Improvements

Numerous small changes were made to improve metadata parsing accuracy.  
If you encounter any strange metadata behavior, please share feedback — and if possible, include metadata links so we can reproduce the issue.

## What's Changed
- Update wiki installation link in `README.md` by [@digeratimt](https://github.com/digeratimt) ([#23](https://github.com/smokin-salmon/smoked-salmon/pull/23))
- Update `oxipng` install command in `README.md` by [@digeratimt](https://github.com/digeratimt) ([#25](https://github.com/smokin-salmon/smoked-salmon/pull/25))
- Release is now marked as 24bit if **any** file is 24bit (previously only the first file was checked)
- Improved Qobuz artist parsing (better detection of guest artists)
- More accurate Album/EP/Single classification:
  - Less than 3 tracks → **Single**
  - Less than 5 tracks → **EP**
  - 6 tracks with no release type → **EP**
  - Otherwise uses metadata release type, or defaults to **Album**
- Deezer API responses now use English (mainly affects genres)
- If a source is specified for a WEB release, it takes top priority for metadata
- Removed usage of Qobuz ID as catalog number (CatNo)
- Improved regex detection of "Self-released" label
- Added `NO_GENRES_FROM_QOBUZ` option:
  - Prevents fetching localized genres from Qobuz
  - Useful for non-English accounts
  - Default: `false`

## New Contributors
* @digeratimt made their first contribution in https://github.com/smokin-salmon/smoked-salmon/pull/23

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.14...0.9.2.15
"""

__version__ = "0.9.2.14"

"""
Changelog for version 0.9.2.14 (2025-04-05):

## What's Changed
* Fix request filling output for OPS (cosmetics only, requests were being filled correctly)
* Fix salmon upconv for single files, fixes #20 


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.13...0.9.2.14
"""

__version__ = "0.9.2.13"

"""
Changelog for version 0.9.2.13 (2025-04-04):

The file `config.py.txt` has been updated to include all possible configuration options along with their default values. If any options are unclear or insufficiently detailed on the wiki, feel free to open issues or submit PRs to enhance the documentation.

## What's Changed
* Update and organize default configuration file
* Remove imghdr dependency (deprecated and removed in python 3.13). Fixes #18 


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.12...0.9.2.13
"""

__version__ = "0.9.2.12"

"""
Changelog for version 0.9.2.12 (2025-04-04):

Technical release - still testing github actions

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.11...0.9.2.12
"""

__version__ = "0.9.2.11"

"""
Changelog for version 0.9.2.11 (2025-04-04):

Technical release - Testing github actions

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.10...0.9.2.11
"""

__version__ = "0.9.2.10"

"""
Changelog for version 0.9.2.10 (2025-04-04):

Technical release - Nothing changed.

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.9...0.9.2.10
"""

__version__ = "0.9.2.9"

"""
Changelog for version 0.9.2.9 (2025-04-04):

## What's Changed
* Fix github action rights to bump versions


**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.8...0.9.2.9
"""

__version__ = "0.9.2.7"

"""
Changelog for version 0.9.2.7 (2025-04-03):

## What's Changed
* Fix crash when folder doesn't need to be renamed (shutil related crash)
* Fix crash when exiting editor without saving during metadata edition
* USE_UPC_AS_CATNO will now use UPC as catno only if catno is empty.
* Improve README instructions for docker users

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.6...0.9.2.7
"""

__version__ = "0.9.2.6"

"""
Changelog for version 0.9.2.6 (2025-04-02):

## What's Changed
* Improve Multi-Discs releases ! It should be working fine, feedback appreciated.
* For post-upload lma checks, ask if this is a lma and which spectrals to upload (even if YES_ALL is active)
* Add `checkconf --reset` option, to reset or generate a config.py file

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.5...0.9.2.6
"""

__version__ = "0.9.2.5"

"""
Changelog for version 0.9.2.5 (2025-04-02):

## What's Changed
* Fix spectrals upload in multi trackers scenario and post-upload spectrals check
* Force lossy master prompt if checking spectrals post upload (upload is done, we have time)
* Skip request confirmation in YES_ALL mode

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.4...0.9.2.5
"""

__version__ = "0.9.2.4"

"""
Changelog for version 0.9.2.4 (2025-04-01):

## What's Changed
* Add `--skip-mqa` option to skip the MQA check during upload

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.3...0.9.2.4
"""

__version__ = "0.9.2.3"

"""
Changelog for version 0.9.2.3 (2025-04-01):

## New dependency
Oxipng is a modern replacement for OptiPNG, written in Rust and optimized for multi-core processors. It offers faster processing and better compression than OptiPNG. Smoked-salmon now utilizes Oxipng for PNG compression, achieving around a 30% reduction in file size, making it more efficient for image hosting.

You'll need to install Oxipng on your system (it's already included in the Docker image). Installation instructions can be found on the official repository: https://github.com/shssoichiro/oxipng.

Ubuntu/Debian users can simply run the following command (make sure to check the latest Oxipng release first, and replace the link accordingly):
```
wget https://github.com/shssoichiro/oxipng/releases/download/v9.1.4/oxipng_9.1.4-1_amd64.deb && sudo dpkg -i oxipng_9.1.4-1_amd64.deb
```
Alternatively, you can still disable compression by setting `COMPRESS_SPECTRALS = False` in your `config.py` file.

## What's Changed
* Replace `optipng` by `oxipng`
* Use `process_files` (better multithreaded processing) for spectrals generation
* Fix broken log upload for CDs (broken during ruff fixes)

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.2...0.9.2.3
"""

__version__ = "0.9.2.2"

"""
Changelog for version 0.9.2.2 (2025-03-31):

## What's Changed
* Fix special characters handling in github actions (like `this is a string` or `this`)

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2.1...0.9.2.2
"""

__version__ = "0.9.2.1"

"""
Changelog for version 0.9.2.1 (2025-03-31):

##New config options
* DEBUG_TRACKER_CONNECTION : Automatically set to True when using the  subcommand. It shouldn't be needed to override this option manually in your config.py
* UPDATE_NOTIFICATION : when set to True (default value), will show a notice message when a new version is available on this repository
* UPDATE_NOTIFICATION_VERBOSE : when set to True (default value), will show the changelog between the local version and the latest version available on this repository

## What's Changed
* Add checkconf command (check configuration/connection to trackers)
* Add update notification feature (heavily inspired by https://github.com/Audionut/Upload-Assistant <3)

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.2...0.9.2.1
"""

__version__ = "0.9.2"

"""
Changelog for version 0.9.2 (2025-03-31):

First pre-release since creating this new home for smoked-salmon.

## What's Changed

- No unaliasing of tags for scene releases
- Test for MQA release at beginning of upload (only for the first file)
- Updated styling (boldness, colors, etc...) across the app
- Fix smoked tag command crash (was not properly handling the source marker )
- Improve specs coommand ordering (was mangled due to ordering of 1. / 10. / 2. tracks using lexical ordering)
- Remove poetry/requirements/old files from the repository (full uv now)
- Add ruff linting
- Add docker image build

**Full Changelog**: https://github.com/smokin-salmon/smoked-salmon/compare/0.9.1...0.9.2
"""

