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

