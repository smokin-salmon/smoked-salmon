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

