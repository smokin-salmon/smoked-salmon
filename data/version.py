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

