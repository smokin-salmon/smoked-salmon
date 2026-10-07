[![Build and Publish Docker Image](https://github.com/smokin-salmon/smoked-salmon/actions/workflows/docker-image.yml/badge.svg)](https://github.com/smokin-salmon/smoked-salmon/actions/workflows/docker-image.yml) [![Linting](https://github.com/smokin-salmon/smoked-salmon/actions/workflows/lint.yml/badge.svg?branch=master)](https://github.com/smokin-salmon/smoked-salmon/actions/workflows/lint.yml)

# 🐟 smoked-salmon  

A simple tool to take the work out of uploading on Gazelle-based trackers. It generates spectrals, gathers metadata, allows re-tagging/renaming files, and automates the upload process.

## 🌟 Features  

- **Interactive Uploading** – Supports **multiple trackers** (RED / OPS / DIC).
- **Log Checking** – Calculates log scores, verifies log checksum integrity, and validates log-to-FLAC file matching.
- **Upconvert Detection** – Checks 24-bit flac files for potential upconverts.
- **MQA Detection** – Checks files for common MQA markers.
- **Duplicate Upload Detection** – Prevents redundant uploads.  
- **Do-Not-Upload Lists** – salmon ships a copy of RED's and OPS's Do-Not-Upload lists (fakes, unreleased albums, bootlegs, some whole discographies and labels; some entries only for one media, such as WEB), and never uploads a listed release to that tracker, with or without `--yes-all`: it says which entry matched and why, and the run goes on to the other trackers. A legitimate copy needs that tracker's staff approval first. The maintainers update the lists with salmon's releases; `cross-upload` checks the target's list too.  
- **Spectral Analysis** – Generates, compresses, and verifies spectrals, shown on a web page during upload.  
- **Frequency Analysis** – Before the lossy-master question, measures the two marks a lossy encoder leaves in each track: a brick-wall lowpass where MP3 and AAC encoders cut, and highs that flip between content and digital silence. It prints what it measured, shows an averaged-spectrum plot next to each track's spectrals (kept local, never uploaded), and makes "yes" the question's default when a track carries the marks. It is a measurement, not a verdict: high-bitrate AAC can leave neither mark.  
- **Spectral Upload** – Can generate spectrals for an existing upload (based on local files), and update the release description.  
- **Lossy Master Report Generation** – Supports lossy master reports during upload.
- **Metadata Retrieval** – Fetches metadata from:
  - Apple Music, Bandcamp, Beatport, Deezer, Discogs, MusicBrainz, Qobuz, Tidal.
- **File Management** –  
  - Retags and renames files to standard formats (based on metadata).
  - Checks file integrity and sanitizes if needed.  
- **Request Filling** – Scans for matching requests on trackers.
- **Description generation** – Edition description generation (tracklist, sources, available streaming platforms, encoding details...).
- **Down-convert and Transcode** – Can downconvert 24-bit flac files to 16-bit, and transcode to mp3.
- **Multi-Format Upload** – Automatically transcodes and uploads multiple formats (FLAC 16-bit, MP3, etc.) in a single workflow.
- **Torrent Client Injection** – Can inject generated torrent files into torrent clients (qBittorrent, Transmission, Deluge, ruTorrent).
- **Remote Seeding** – Can transfer files to multiple remote locations via rclone and inject torrents into remote torrent clients for automatic seeding.
- **Update Notifications** – Informs users when a new version is available.

## 📥 Installation  

Manual installation instructions can be found on the [Wiki](https://github.com/smokin-salmon/smoked-salmon/wiki/Installation).

### 🔹  Install smoked-salmon 
These steps use [`uv`](https://github.com/astral-sh/uv) for installing the *smoked-salmon* package. [`pipx`](https://github.com/pypa/pipx) also works.
Installing with pip is not recommended because uv (and pipx) manage python versions and isolate the *smoked-salmon* installation from the system python installation.
If smoked-salmon runs on Python 3.14 (uv may pick it on a fresh install), spectral images are uploaded uncompressed because oxipng is not available for it yet; to have them compressed, install with `uv tool install --python 3.13 git+https://github.com/smokin-salmon/smoked-salmon` instead.

#### Linux
1. Install system packages:
    ```bash
    sudo apt install sox libsox-fmt-mp3 flac mp3val curl lame
    ```
    Debian and Ubuntu's `sox` package reads MP3 files only when `libsox-fmt-mp3` is also installed.

2. Install uv:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

3. Install smoked-salmon package from github:
	```bash
	uv tool install git+https://github.com/smokin-salmon/smoked-salmon
	```

#### Windows
Run these commands in **PowerShell** (Start menu, "Windows PowerShell"), not in Command Prompt (`cmd`). They fail in Command Prompt.

1. Install required system packages using winget:
    ```powershell
    winget install -e ChrisBagwell.SoX Xiph.FLAC LAME.LAME ring0.MP3val.WF
    ```
    Then close PowerShell and open a new window, so it finds the new programs.

2. Fix sox Unicode filename handling issue on Windows:
    ```powershell
    $soxDir = $((Get-Command sox).Source | Split-Path)
    $zipPath = Join-Path -Path $soxDir -ChildPath "sox_windows_fix.zip"
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/DevYukine/red_oxide/master/.github/dependency-fixes/sox_windows_fix.zip" -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $soxDir -Force
    regedit "$soxDir\PreferExternalManifest.reg"
    Remove-Item $zipPath
    ```

3. Install uv:
    ```powershell
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    ```
    Then close PowerShell and open a new window, so it finds `uv`.

4. Install smoked-salmon package from github:
	```powershell
	uv tool install git+https://github.com/smokin-salmon/smoked-salmon
	```
    smoked-salmon has no window or Start menu entry: you type `salmon` in PowerShell (see Initial Setup below).
    If PowerShell says `salmon` is not recognized, run `uv tool update-shell`, then open a new PowerShell window.

#### macOS
1. Install Homebrew (if you haven't already):
    ```bash
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    ```

2. Install system packages using Homebrew:
    ```bash
    brew install sox flac mp3val curl lame
    ```

3. Install uv:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

4. Install smoked-salmon package from github:
	```bash
	uv tool install git+https://github.com/smokin-salmon/smoked-salmon
	```

### 🔹  Initial Setup
smoked-salmon is a command-line program: run every `salmon` command in a terminal (PowerShell on Windows).

1. Run salmon for the first time. It offers to create a default configuration file and prints where it is:
	```
	salmon-user@salmon:~$ salmon
	Could not find configuration path at /home/salmon-user/.config/smoked-salmon/config.toml.
	Do you want smoked-salmon to create a default config file at /home/salmon-user/.config/smoked-salmon/config.toml? [y/N]:
	```

2. Edit the `config.toml` file with your preferred text editor to add your API keys, session cookies and update your preferences (see the [Configuration Wiki](https://github.com/smokin-salmon/smoked-salmon/wiki/Configuration)).

3. Use the `checkconf` command to verify that the connection to the trackers is working:

	```
	salmon checkconf
	```

4. Use the `health` command to verify that all necessary command line dependencies are installed:
	```
	salmon health
	```

### 🐳 Docker Installation

A Docker image is generated per release (`:latest`) and on every push to `master` (`:alpha`).  
Feedback on this guide is welcome.

1. Pull the image:

   ```bash
   # Stable release
   docker pull ghcr.io/smokin-salmon/smoked-salmon:latest

   # Alpha (built on every push to master, equivalent to `uv tool install git+...`)
   docker pull ghcr.io/smokin-salmon/smoked-salmon:alpha
   ```

   > The examples below use the `latest` tag. Replace with `alpha` to use the latest development version.

2. Copy the content of the file [`config.toml`](https://github.com/smokin-salmon/smoked-salmon/blob/master/src/salmon/data/config.default.toml) to `config.toml` in a directory on your host server; this directory gets mounted at `/config` in the container.
   Edit the `config.toml` file with your preferred text editor to add your API keys, session cookies and update your preferences (see the [Configuration Wiki](https://github.com/smokin-salmon/smoked-salmon/wiki/Configuration)).

3. Configure rclone if needed by copying your host's rclone config (`rclone config file` prints its path) into the same directory, as `/config/rclone.conf`.

> The image sets `SALMON_CONFIG_DIR=/config`, so a single mount holds `config.toml`
> (and `rclone.conf`). If you are upgrading an image that mounted
> `/root/.config/smoked-salmon/` directly, that old mount still works, but move
> `config.toml` into the new `/config` mount when you can.

---

### 🔁 Docker Usage

1. **Check Configuration**
   Run the container with the `checkconf` command to verify that the connection to the trackers is working:

   ```bash
   # The -e RCLONE_CONFIG line is optional: only needed if you use rclone features.
   docker run --rm -it --network=host \
   -v /path/to/your/music:/app/.music \
   -v /path/to/your/config:/config \
   -v /path/to/your/generated/dottorrents:/app/.torrents \
   -e RCLONE_CONFIG=/config/rclone.conf \
   ghcr.io/smokin-salmon/smoked-salmon:latest checkconf
   ```

2. **Upload**
   Run the upload command directly (replace `checkconf` with any salmon command):

   ```bash
   # The -e RCLONE_CONFIG line is optional: only needed if you use rclone features.
   docker run --rm -it --network=host \
   -v /path/to/your/music:/app/.music \
   -v /path/to/your/config:/config \
   -v /path/to/your/generated/dottorrents:/app/.torrents \
   -e RCLONE_CONFIG=/config/rclone.conf \
   ghcr.io/smokin-salmon/smoked-salmon:latest up "/app/.music/path/to/album" -s WEB
   ```

### 💡 Shell Alias (Optional)

To avoid repeating the long `docker run` command, add the following alias to your shell configuration file (`~/.bashrc`, `~/.zshrc`, etc.):

```bash
alias salmon='docker run --rm -it --network=host \
  -v /path/to/your/music:/app/.music \
  -v /path/to/your/config:/config \
  -v /path/to/your/generated/dottorrents:/app/.torrents \
  -e RCLONE_CONFIG=/config/rclone.conf \
  ghcr.io/smokin-salmon/smoked-salmon:latest'
```

Then use it just like a native install:

```bash
salmon checkconf
salmon health
salmon up "/app/.music/path/to/album" -s WEB
```

---

### ⚠️ Notes

- **Permission Issues**  
  The container does not manage file ownership for you.  
  If your torrent client is not run as root, or if new uploads are inaccessible, you may need to:
  - Manually adjust file/folder ownership (`chown`) or permissions (`chmod`)
  - Ensure the container and torrent client users are compatible
  - Optionally run containers with matching `--user` flags or add `umask` logic
     ```bash
    user: "1001:100"
    environment:
      - PUID=1001
      - PGID=100
     ```

- **Hardlinks**  
  With `hardlinks = true` (the default), salmon hardlinks the release into `download_directory` instead of copying it. A hardlink cannot cross mounts, even two bind mounts of the same disk, so point `download_directory` at a folder inside the music mount (e.g. `/app/.music/seeding`). Otherwise salmon falls back to a full copy.

- **.torrent Directory Mapping**  
  Depending on how you've set `dottorrents_dir` in your `config.toml`, you may need to map an additional directory for `.torrent` file output. Add:

  ```bash
  -v /your/host/torrent/output:/app/.torrents
  ```

- **rclone Configuration**  
  If you're using rclone features, put your rclone config file (`rclone config file` prints its path on your host system) in the `/config` mount, alongside `config.toml`, and point rclone at it:

  ```bash
  -e RCLONE_CONFIG=/config/rclone.conf
  ```

---

### 📦 Docker Compose

If using Docker Compose, create a `docker-compose.yml` to define your volume mappings and network settings, then use `docker compose run` to execute any salmon command on demand:

```yaml
services:
  salmon:
    image: ghcr.io/smokin-salmon/smoked-salmon:latest
    network_mode: host
    environment:
      - RCLONE_CONFIG=/config/rclone.conf  # Optional: only if using rclone features
    volumes:
      - /path/to/your/music:/app/.music
      - /path/to/your/config:/config
      - /path/to/your/generated/dottorrents:/app/.torrents

```

```bash
# Check configuration
docker compose run --rm salmon checkconf

# Upload
docker compose run --rm salmon up "/app/.music/path/to/album" -s WEB
```

## 🚀 Usage

### 🎨 Terminal Colors
smoked-salmon uses distinct terminal colors for different types of messages:

* Default – General information
* Red – Errors or critical failures
* Green – Success messages
* Yellow – Information headers
* Cyan – Section headers
* Magenta – User prompts

### 🔧 CLI Mode
smoked-salmon runs in CLI mode, except for spectral visualization, which launches a web server. Quick start usage instructions can be found on the [Wiki Usage page](https://github.com/smokin-salmon/smoked-salmon/wiki#usage).

The examples below show how to run smoked-salmon directly. If you're using Docker, you'll need to adjust them accordingly, but the underlying principles remain the same.

To see the available commands, just type:
```bash
salmon
```

To test the connection to the trackers, run:
```bash
salmon checkconf
```

To check the status of salmon's command line and config dependencies, run:
```bash
salmon health
```

To start an upload (with the WEB source):
```bash
salmon up /data/path/to/album -s WEB
```

If the FLAC torrent is already in an existing group, skip re-uploading it and
select the lower formats to transcode and upload:
```bash
salmon up /data/path/to/album -s WEB -g GROUP_ID --skip-flac-upload
```
This only works from a lossless FLAC. The album folder is never modified, since it is most likely the
one seeding that FLAC: salmon copies it into `download_directory/.salmon-staging`, works on the copy,
and removes the copy when the run ends. The transcodes land in `download_directory` as usual.

The transcode descriptions link to the group's FLAC in this release's edition (same media, encoding,
year and edition title, as reviewed; the catalogue number is not compared, since sites and uploaders
write it in different conventions); if there are several, salmon asks which one (with `--yes-all` it
stops). Formats the edition already has are flagged as a dupe risk and left out unless you pick them
by number.

To see what an upload would send, without sending anything, add `--dry-run`:
```bash
salmon up /data/path/to/album -s WEB --dry-run
```
salmon goes through the whole upload (checks, prompts, review, torrents, transcodes) on a copy of the album
in `download_directory/.salmon-staging`, and prints each upload's form (secrets masked), the torrent's files
and piece size instead of sending it. It only reads from the tracker (the login check and searches), uploads
no image (the form shows a placeholder for each image URL), copies nothing to a seedbox and adds nothing to a
torrent client. The copy, with its torrent files and transcodes, is removed when the run ends.

To upload a torrent that is on RED, OPS or DIC to another of them, from the files you already have:
```bash
salmon cross-upload 123456 RED OPS
```
Give the source torrent's ID, URL or `.torrent` file, up to 5 per run, then the source and target trackers.
The files must be exactly the torrent's, in `download_directory/<the torrent's folder>` (or give the folder
with `--path`). salmon first reads each torrent and checks its files, its log and its images, and shows the
plan; then, for each release, it checks the target for duplicates as `up` does and uploads it with the
source's group and torrent data. The album folder is never changed: the new torrent seeds from the same files,
and a seedbox only gets the new torrent, not another copy. `--dry-run` reads from both trackers and sends
nothing. A cross-upload to DIC is never marked Self-purchased, Self-rip or Exclusive (it is a re-post, so it is
not asked), and a 24-bit one gets its sample rate as `up` sends it. DIC's support follows chodeus's fork: its
rules and answers have not been checked against DIC itself.

You can get help directly from the CLI by appending --help to any command. This is especially useful for the up command which has a lot of possible options.

### 🌐 Spectral Web Interface
When `up` reaches spectral review, it starts a small web server and prints the link to the spectrals page (by default `http://localhost:55110/spectrals`); the server stops when you continue. There is no separate `web` command since 0.11.0. If salmon runs on another machine or in Docker, set `display_host` under `[upload.web_interface]` to that machine's address so the printed link works. Set `native_spectrals_viewer = true` to open the images locally instead.

## 🔄 Updating

For **normal installs**:
```bash
uv tool upgrade salmon
```

For **manual installs**:
```bash
cd smoked-salmon
git pull
uv sync
```

For **Docker users**:
```bash
docker pull ghcr.io/smokin-salmon/smoked-salmon:latest
```

## 📞 Support
For bug reports and feature requests, use GitHub Issues. Or use the forums.


## 🎭 Testimonials
```
"Salmon filled the void in my heart. I no longer chase after girls." ~boot
"With the help of salmon, I overcame my addiction to kpop thots." ~b
"I warn 5 people every day on the forums using salmon!" ~jon
```

## 🎩 Credits
* Originally created by [ligh7s](https://github.com/ligh7s/smoked-salmon). Huge thanks!
* Further development & maintenance by elghoto, xmoforf, miandru, redusys, kyokomiki and others. Keeping the dream alive.
* Docker image build workflow and update notification mechanisms heavily inspired from the awesome work of Audionut on his [Upload Assistant tool](https://github.com/Audionut/Upload-Assistant) !
