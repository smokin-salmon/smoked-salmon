[![Build and Publish Docker Image](https://github.com/tomerh2001/smoked-salmon/actions/workflows/docker-image.yml/badge.svg)](https://github.com/tomerh2001/smoked-salmon/actions/workflows/docker-image.yml) [![Linting](https://github.com/tomerh2001/smoked-salmon/actions/workflows/lint.yml/badge.svg?branch=master)](https://github.com/tomerh2001/smoked-salmon/actions/workflows/lint.yml)

# 🐟 smoked-salmon  

A simple tool to take the work out of uploading on Gazelle-based trackers. It generates spectrals, gathers metadata, allows re-tagging/renaming files, and automates the upload process.

This repository is Tomer's actively maintained fork of `smokin-salmon/smoked-salmon`. If you want the forked build, Docker image, and update path to stay aligned, use the commands in this README rather than the upstream wiki.

## 🔗 Fork Links

- Fork repository: https://github.com/tomerh2001/smoked-salmon
- Upstream repository: https://github.com/smokin-salmon/smoked-salmon
- Fork issues: https://github.com/tomerh2001/smoked-salmon/issues
- Fork releases: https://github.com/tomerh2001/smoked-salmon/releases
- Docker images: `ghcr.io/tomerh2001/smoked-salmon:latest`, `ghcr.io/tomerh2001/smoked-salmon:personal-fork`, and `ghcr.io/tomerh2001/smoked-salmon:alpha`

## 🧩 Fork Master Composition

This fork's `master` branch is an integration branch. It is intentionally built from `smokin-salmon/smoked-salmon` `master` plus the in-flight patch sets below so there is one branch that always reflects the combined state I run locally.

It is not meant to be reviewed upstream as one giant PR. The upstream review units are the smaller PRs listed here.

| PR | Status | Included in fork `master` | Summary |
| --- | --- | --- | --- |
| [#342](https://github.com/smokin-salmon/smoked-salmon/pull/342) | Open | Yes | Optional AI metadata review workflow |
| [#345](https://github.com/smokin-salmon/smoked-salmon/pull/345) | Open | Yes | Upload CLI automation flags and source-url helpers |
| [#347](https://github.com/smokin-salmon/smoked-salmon/pull/347) | Open | Yes | Bandcamp parsing fixes for catno-prefixed and label-hosted releases |
| [#352](https://github.com/smokin-salmon/smoked-salmon/pull/352) | Open | Yes | `open.qobuz.com` URL handling |
| [#362](https://github.com/smokin-salmon/smoked-salmon/pull/362) | Open | Yes | RED cookie-backed upload hardening |

Fork-only commits on `master`:

- prerelease CI/CD for the fork `master` branch
- rolling Docker tags `personal-fork` and `alpha`
- immutable fork prereleases in the form `0.10.1-personal-fork.<run>`
- fork-specific README and install/update guidance
- fork images report their own `0.10.1-personal-fork.<run>` runtime version inside Salmon

How new work enters fork `master`:

1. Create a new issue on the upstream repository for the bug or feature.
2. Branch from upstream `smokin-salmon/smoked-salmon` `master`, not from this fork's `master`.
3. Implement the fix on that upstream-based branch and open a focused upstream PR.
4. Merge that PR branch into this fork's `master` so the integration branch stays ahead with the combined local state.
5. Let the fork `master` CI/CD publish a new prerelease and refresh the rolling Docker tags.
6. Let local consumers use the new fork release artifacts instead of relying on an editable local checkout.

If you only want the AI work without the rest of the integration branch, use [PR #342](https://github.com/smokin-salmon/smoked-salmon/pull/342).

## 🌟 Features  

- **Interactive Uploading** – Supports **multiple trackers** (RED / OPS / DIC).
- **Log Checking** – Calculates log scores, verifies log checksum integrity, and validates log-to-FLAC file matching.
- **Upconvert Detection** – Checks 24-bit flac files for potential upconverts.
- **MQA Detection** – Checks files for common MQA markers.
- **Duplicate Upload Detection** – Prevents redundant uploads.  
- **Spectral Analysis** – Generates, compresses, and verifies spectrals, exposed via a web interface.  
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

This README is the main installation and configuration guide for the fork. The checked-in template at [`src/salmon/data/config.default.toml`](src/salmon/data/config.default.toml) is the source of truth for current config keys.

### 🔹  Install smoked-salmon 
These steps use [`uv`](https://github.com/astral-sh/uv) for installing the *smoked-salmon* package. [`pipx`](https://github.com/pypa/pipx) also works.
Installing with pip is not recommended because uv (and pipx) manage python versions and isolate the *smoked-salmon* installation from the system python installation.

#### Linux
1. Install system packages:
    ```bash
    sudo apt install sox flac mp3val curl lame
    ```

2. Install uv:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

3. Install smoked-salmon package from github:
	```bash
	uv tool install git+https://github.com/tomerh2001/smoked-salmon
	```

#### Windows
1. Install required system packages using winget:
    ```powershell
    winget install -e ChrisBagwell.SoX Xiph.FLAC LAME.LAME ring0.MP3val.WF
    ```

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

4. Install smoked-salmon package from github:
	```powershell
	uv tool install git+https://github.com/tomerh2001/smoked-salmon
	```

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
	uv tool install git+https://github.com/tomerh2001/smoked-salmon
	```

### 🔹  Initial Setup
1. Run salmon for the first time and follow the instructions to create a default configuration:
	```
	salmon-user@salmon:~$ salmon
	Could not find configuration path at /home/salmon-user/.config/smoked-salmon/config.toml.
	Do you want smoked-salmon to create a default config file at /home/salmon-user/.config/smoked-salmon/config.toml? [y/N]:
	```

2. Edit the `config.toml` file with your preferred text editor to add your API keys, session cookies, and update your preferences. The checked-in template lives at [`src/salmon/data/config.default.toml`](src/salmon/data/config.default.toml).

3. Use the `checkconf` command to verify that the connection to the trackers is working:

	```
	salmon checkconf
	```

4. Use the `health` command to verify that all necessary command line dependencies are installed:
	```
	salmon health
	```

### Configuration Notes

Use [`src/salmon/data/config.default.toml`](src/salmon/data/config.default.toml) as the current reference for available settings. If you compare against the upstream wiki, prefer the fork's checked-in config template when there is a mismatch.

### 🐳 Docker Installation

The fork publishes three GHCR image tracks:

- `ghcr.io/tomerh2001/smoked-salmon:latest` for tagged releases
- `ghcr.io/tomerh2001/smoked-salmon:personal-fork` as the rolling "use this fork right now" tag
- `ghcr.io/tomerh2001/smoked-salmon:alpha` as a compatibility alias for the current `master` branch

If you want the newest fork changes before the next tagged release, use `:personal-fork`.
Every push to fork `master` also creates an immutable prerelease tag in the form `0.10.1-personal-fork.<run>` on the fork releases page.

1. Pull the image:

   ```bash
   # Stable release
   docker pull ghcr.io/tomerh2001/smoked-salmon:latest

   # Current fork master
   docker pull ghcr.io/tomerh2001/smoked-salmon:personal-fork

   # Compatibility alias for the same moving master build
   docker pull ghcr.io/tomerh2001/smoked-salmon:alpha
   ```

   The examples below use the `latest` tag. Replace it with `personal-fork` if you want the current fork `master` build.

2. Copy the content of [`src/salmon/data/config.default.toml`](src/salmon/data/config.default.toml) to a location on your host server.  
   Edit the `config.toml` file with your preferred text editor to add your API keys, session cookies, and update your preferences.

3. Configure rclone if needed. The Docker Compose configuration expects an rclone configuration file. You can get the path to your rclone config file by running `rclone config file` on your host system.

---

### 🔁 Docker Usage

1. **Check Configuration**
   Run the container with the `checkconf` command to verify that the connection to the trackers is working:

   ```bash
   docker run --rm -it --network=host \
   -v /path/to/your/music:/app/.music \
   -v /path/to/your/config.toml/directory:/root/.config/smoked-salmon/ \
   -v /path/to/your/generated/dottorrents:/app/.torrents \
   -v /get/this/from/"rclone config file":/root/.config/rclone/rclone.conf  # Optional: only if using rclone features \
   ghcr.io/tomerh2001/smoked-salmon:latest checkconf
   ```

2. **Upload**
   Run the upload command directly (replace `checkconf` with any salmon command):

   ```bash
   docker run --rm -it --network=host \
   -v /path/to/your/music:/app/.music \
   -v /path/to/your/config.toml/directory:/root/.config/smoked-salmon/ \
   -v /path/to/your/generated/dottorrents:/app/.torrents \
   -v /get/this/from/"rclone config file":/root/.config/rclone/rclone.conf  # Optional: only if using rclone features \
   ghcr.io/tomerh2001/smoked-salmon:personal-fork up "/app/.music/path/to/album" -s WEB
   ```

### 💡 Shell Alias (Optional)

To avoid repeating the long `docker run` command, add the following alias to your shell configuration file (`~/.bashrc`, `~/.zshrc`, etc.):

```bash
alias salmon='docker run --rm -it --network=host \
  -v /path/to/your/music:/app/.music \
  -v /path/to/your/config.toml/directory:/root/.config/smoked-salmon/ \
  -v /path/to/your/generated/dottorrents:/app/.torrents \
  -v /path/to/your/rclone.conf:/root/.config/rclone/rclone.conf \
  ghcr.io/tomerh2001/smoked-salmon:personal-fork'
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
  The container currently **able to handle permissions** properly.  
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

- **.torrent Directory Mapping**  
  Depending on how you've set the `DOTTORRENTS_DIR` in your `config.toml`, you may need to map an additional directory for `.torrent` file output. Add:

  ```bash
  -v /your/host/torrent/output:/app/.torrents
  ```

- **rclone Configuration**  
  If you're using rclone features, make sure to map your rclone configuration file. This is optional and only needed if you plan to use rclone functionality. You can find your rclone config file location by running `rclone config file` on your host system:

  ```bash
  -v /path/to/your/rclone.conf:/root/.config/rclone/rclone.conf
  ```

---

### 📦 Docker Compose

If using Docker Compose, create a `docker-compose.yml` to define your volume mappings and network settings, then use `docker compose run` to execute any salmon command on demand:

```yaml
services:
  salmon:
    image: ghcr.io/tomerh2001/smoked-salmon:latest
    container_name: smoked-salmon
    network_mode: host
    volumes:
      - /path/to/your/music:/app/.music
      - /path/to/your/config.toml/directory:/root/.config/smoked-salmon/
      - /path/to/your/generated/dottorrents:/app/.torrents
      - /get/this/from/"rclone config file":/root/.config/rclone/rclone.conf  # Optional: only if using rclone features

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
smoked-salmon runs in CLI mode, except for spectral visualization, which launches a web server. The most useful commands are shown below.

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

You can get help directly from the CLI by appending --help to any command. This is especially useful for the up command which has a lot of possible options.

### 🌐 Spectral Web Interface
Spectrals are viewable via a built-in web server. By default, access it at: http://localhost:55110/spectrals

## 🔄 Updating

For **normal installs**:
```bash
uv tool update salmon
```

If you installed from GitHub directly and want to stay on the fork explicitly:

```bash
uv tool install --force git+https://github.com/tomerh2001/smoked-salmon
```

For **manual installs**:
```bash
cd smoked-salmon
git pull
uv sync
```

For **Docker users**:
```bash
docker pull ghcr.io/tomerh2001/smoked-salmon:latest
```

## 📞 Support
For fork-specific bug reports and feature requests, use [GitHub Issues](https://github.com/tomerh2001/smoked-salmon/issues). Upstream discussion can still happen on the forums.


## 🎭 Testimonials
```
"Salmon filled the void in my heart. I no longer chase after girls." ~boot
"With the help of salmon, I overcame my addiction to kpop thots." ~b
"I warn 5 people every day on the forums using salmon!" ~jon
```

## 🎩 Credits
* Originally created by [ligh7s](https://github.com/ligh7s/smoked-salmon). Huge thanks!
* Further development & maintenance by elghoto, xmoforf, miandru, redusys, kyokomiki and others. Keeping the dream alive.
* Fork packaging, publishing, and custom maintenance by [tomerh2001](https://github.com/tomerh2001).
* Docker image build workflow and update notification mechanisms heavily inspired from the awesome work of Audionut on his [Upload Assistant tool](https://github.com/Audionut/Upload-Assistant) !
