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

