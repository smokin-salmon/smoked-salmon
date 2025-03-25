# Upgrades

This file contains changes made to the script that require users to change
something.

Any config changes must be made or else newer versions will error.

## July 22nd 2020

Lots of changes. Please read CHANGELOG.md  
You will probably need to run:  
    pip install -r requirements.txt
again.
Also any plugins will probably need updating. 



## October 5th, 2018

Add option to allow for lossy master/web notes to be put in the torrent description.
Also rip the salmon image torrent description option.

## September 24th, 2018

Added salmon image configuration option for the description. Set the `SALMON_IMAGE`
configuration option to change it. They do not need to adhere to a size.

## September 23rd, 2018

Add MQA script and the `bitstring` dependency. Reinstall!!

## September 22nd, 2018

Add option to recompress spectrals with optipng. Requires optipng to be installed
and in your path. Can be enabled with the `COMPRESS_SPECTRALS` configuration option.

## September 17th, 2018

Rename queue upload/download to up/dl respectively. Rename downconvert to downconv.

## September 15th, 2018

Added option to disable adding edition title to album tag, default remains unchanged.

## September 15th, 2018

Forgot to mention the presence of a new dependency for the logchecker
functionality. Update dependencies guys!

## September 15th, 2018

Made the puddletag tag editing prompt optional, default off with a config key
of `PROMPT_PUDDLETAG`.

## September 15th, 2018

Made spectrals web viewer the default interface; enable `NATIVE_SPECTRALS_VIEWER` to use
feh/macos thing.

## September 12th, 2018

Updated dependencies: added logchecker.

## September 11th, 2018

Major revamp of metadata review and confirmation step(s).

## September 8th, 2018

Add option to put tracklist in torrent description. This is now off by default.

## September 8th, 2018

There are many, many changes in the prompts and visual display. Please be careful
when reading and responding to prompts. There are new dependencies that must be installed
as well.

## September 5th, 2018

Added a `SIMULTANEOUS_CONVERSIONS` option for the converter. This affects how many
transcodes and downconverts can run simultaneously. (edit in 2025-03-25: replaced by `SIMULTANEOUS_THREADS`)

## September 3rd, 2018

Lots of configuration changes. Image hosts gained a few new options; and the options
were shuffled slightly. `IMAGE_UPLOADER` is now the de-facto image uploader for general
image uploaders. `COVER_UPLOADER` is the uploader for the cover art. `SPECS_UPLOADER`
remains the uploader for spectrals. `imgur` and `vgy.me` were added as image uploader
options. `imgur` requires a client ID from the website. Each client ID is limited to
1,250 uploads per day. If you and a friend do not come close to this limit, you can
share the ID.

The webserver also has several new configuration options. `WEB_SPECTRALS_HOST` enables
viewing spectrals via a webserver instead of with feh. The other webserver options
are all overrides for default values if your setup is nonstandard. `WEB_HOST` is what
the script will print to stdout as the base URL for the salmon tool--if you are
reverse proxying the webserver, you will want to substitute this with your (sub)domain
and, if applicable, location block. `WEB_PORT` allows you to run the webserver on a port
other than the provided 55110. `WEB_STATIC_ROOT_URL` allows you to change the directory
for `/static` resources to match a location block change. For example, if you are hosting
this on `domain.tld/salmon`, set this option to `/salmon/static`.

## September 2nd, 2018

Puff added a webserver to the script. I got salmon running again by running
`pipenv install` in the salmon directory and making sure the db was up to date
with `salmon migrate`. If you're not running this in an environment, you might
want to `pip3 install -r requirements.txt` Booty said he'd update the wiki with
setting it up for nginx so let's hold him accountable.

## September 1st, 2018

Added configuration option for CJK fullwidth replacements for blacklisted
characters.

## August 31st, 2018

Removed imgur as an image host option due to rate limiting, may readd later.
Added options for metadata source icons in the descriptions and for a salmon
in the torrent description.

## August 31st, 2018

Added image host options when uploading. Imgur is now an option.

## August 28th, 2018

Added option for stripping useless versions from track title.

## August 27th, 2018

Added toggle option for full screen of the spectral image viewer.

## August 25th, 2018

Added location for queued downloads to the config. The database system will now be
used, so you will also need to run `$ ./run.py migrate` to create the database.

## August 25th, 2018 - b34145f54

Added option to not put the album review in the tags.

## August 21st, 2018

Added option to copy uploaded torrent URL to clipboard.

## July 22nd, 2018

Added option to recompress flacs during the upload process: -c

## July 17th, 2018

Added TIDAL_FETCH_REGIONS config option. It's required, so set it!

## July 9th, 2018

Added option for bitrates in torrent description.

## July 8th, 2018

Added a VARIOUS_ARTIST_WORD option that determines what VA will be called.
Common choices are VA / Various / Various Artists.

## July 7th, 2018

Added a ONE_ALBUM_ARTIST_FILE_TEMPLATE config option. It is required if the
NO_ARTIST_IN_FILENAME_IF_ONLY_ONE_ALBUM_ARTIST option is enabled!

## July 6th, 2018

Added a NO_ARTIST_IN_FILENAME_IF_ONLY_ONE_ALBUM_ARTIST config option.

## July 5th, 2018

Added a blacklisted FS substitution thing, u know what to do.

## July 5th, 2018

Added a GUESTS_IN_TRACK_TITLE configuration option. fix your config thanks.

## July 5th, 2018

Added a VARIOUS_ARTIST_THRESHOLD configuration option. fix your config thanks.

## July 5th, 2018

Added a LOWERCASE_COVER flag to the config, which determines whether or not your cover
is called `Cover.{jpg,png}` or `cover.{jpg,png}`.

## July 5th, 2018

Integers are automatically zero padded in the filename template if they are integers.
Remove the :02d from your configuration if it was present, as it will cause issues
with non-integer track numbers.

## July 4th, 2018

Added two new configuration values: `FLAC_COMPRESSION_LEVEL` and `TIDAL_SEARCH_REGIONS`.
Make sure your configurations contain them.
