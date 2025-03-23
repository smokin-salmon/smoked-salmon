# Changelog

Made a new file because there have been a few changes recently.

# Changes 17/03/2025 - 23/03/2025

## Source Integration Improvements
- Improved genre handling for Qobuz, now uses built-in standardization for genres (2025-03-23, @miandru)
- Fixed genre normalization to properly handle accented characters (e.g., "Électronique" → "electronique") (2025-03-23, @miandru)
- Enabled Tidal source integration for metadata (2025-03-21, @redusys)
- Added requirement to provide your own Tidal token in config.py (2025-03-22, @miandru)
- Prevent Tidal/Qobuz metadata search if tokens are not set in config.py (2025-03-21, @redusys)

## Bug Fixes
- Fixed year being parsed incorrectly; only parse alternative release year when matching edition keywords (2025-03-22, @miandru)
- Fixed handling of tracknumbers in x/y format (2025-03-21, @redusys)
- Fixed partial downconversion for last file due to race condition (2025-03-21, @redusys)
- Fixed truncating to real 180 characters (2025-03-21, @redusys)
- Fixed discnumber condition to exclude '1/1' in track description generation (2025-03-21, @redusys)
- Fixed "NoneType" error when label is None and metadata validator is called (2025-03-20, @miandru)
- Added delay before deciding that a folder being deleted is still there (2025-03-17, @miandru)

## General Improvements
- Included links not related to sources in description (2025-03-23, @redusys)
- Made all images 18px by default (OPS doesn't support img resizing in bbcode) (2025-03-23, @redusys)
- Updated artists roles in tracks metadata (2025-03-21, @redusys)
- Updated prompt message for spectral IDs upload to clarify input format (2025-03-21, @redusys)
- Fixed lossy master approval request (2025-03-21, @redusys)

## Configuration Changes
- Added EMPTY_TRACK_COMMENT_TAG configuration to empty comment tag before upload (2025-03-21, @redusys)
- Enabled spectral compression by default (2025-03-21, @redusys)
- Changed file permissions for run.py from executable to read/write (2025-03-21, @redusys)

## Other Improvements
- Propose sanitization only if integrity check failed (2025-03-21, @redusys)
- Code formatting and indentation fixes (2025-03-20, @miandru)

# Changes 16/03/2025
- Fixed bandcamp?? I did not really change that much, it's working though :D
- Added proper exception handling to Qobuz search
- Disabled a debug message to avoid confusion for the end user
- Made sure that necessary apt packages are included in the README.md install guide!
- Disabled beatport scraper until i decide to work on it.

# Changes 15/03/2025

## WebUI Improvements
- Added dark mode with toggle functionality
- Fixed the IP for the webserver to use 0.0.0.0
- Made the WebUI link clickable when generating spectrals and improved on the formatting
- Changed some html/css/javascript formatting

## Qobuz Integration (finished)
- Finalized Qobuz tagger implementation
- Added Qobuz credentials parameters to config
- Added description logo for Qobuz

# Changes 13/03/2025

## Qobuz Integration
- Added Qobuz BaseScraper
- Implemented Qobuz search functionality
- Implemented Qobuz tagger functionality

## Infrastructure Improvements
- Added support for Python uv package manager

# Changes 26/07/2020

## Multi tracker support
Adds support for OPS to smoked-salmon  
use --tracker or -t option to specify which tracker to upload to.   
adds options DEFAULT_TRACKER and TRACKER_LIST (have a gander at config.py.txt example)     
The script will offer the choice to upload to multiple trackers with one command.  
This can be disabled with MULTI_TRACKER_UPLOAD=False  
So far only RED and OPS are supported but the groundwork is there for other gazelle sites.  
(Setup documentation may need updating)      

## Requests checking
Added the option to input a request id to be filled as you upload. (-r)   
The script now searches site requests as you upload and offers a choice to fill one of the requests found.  
This can be disabled with CHECK_REQUESTS=False  

## Added recent upload dupe check
The script now searches for recent uploads similar to the release being uploaded in the site log.  
This is particularly useful for special chararacters in recent content on RED or anything not yet showing up in the regular search due to sphinx.  
This function might be a little slow.
It usses a similarity hueristic with an adjustable tolerance (default is LOG_DUPE_TOLERANCE=0.5)  
This extra dupe check can be disabled with CHECK_RECENT_UPLOADS=False  

## Added option USE_UPC_AS_CATNO
The script now uses the upc as the catalogue number on site if a catalogue number is not found.  
This function will also append the UPC to whatever catno is found.  
This can be disabled with USE_UPC_AS_CATNO=False  

## Spectrals afer upload option. (-a)
This option will tell the script to only generate spectrals after the upload is complete.      
It is advised that you only use this if you are in a hurry to get the torrent uploaded.    
It important that you still always check your spectrals!  
This feature then edits the existing torrent to add the spectrals to the description (and makes a report if asked to).  
It might be advisable good idea to only seed your torrents after you have checked your spectrals.  


## checkspecs
Added command to check spectrals for a torrent on site.  
This is a standalone command that can check and add spectrals to the description of an already uploaded torrent. This requires you to have the files locally.
(see checkspecs -h for more info)  

# Other Changes
The script can use an API key for uploading on RED (full support still pending API coverage)    
Streamlined the way a torrent group id is picked as you upload.  
A library is used for rate limiting (requirements.txt has been updated)  
Added choice to test 24 bit flac for upconverts as you upload.    


