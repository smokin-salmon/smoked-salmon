[![Build and Publish Docker Image](https://github.com/smokin-salmon/smoked-salmon/actions/workflows/docker-image.yml/badge.svg)](https://github.com/smokin-salmon/smoked-salmon/actions/workflows/docker-image.yml) [![Linting](https://github.com/smokin-salmon/smoked-salmon/actions/workflows/lint.yml/badge.svg?branch=master)](https://github.com/smokin-salmon/smoked-salmon/actions/workflows/lint.yml)

# 🐟 smoked-salmon  

A simple tool to take the work out of uploading on Gazelle-based trackers. It generates spectrals, gathers metadata, allows re-tagging/renaming files, and automates the upload process.

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
  - Bandcamp, Beatport, Deezer, Discogs, iTunes, JunoDownload, MusicBrainz, Qobuz, Tidal.
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

#### Linux
1. Install system packages:
    ```bash
    sudo apt install sox flac mp3val curl unzip lame
    ```

2. Install uv:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

3. Install smoked-salmon package from github:
	```bash
	uv tool install git+https://github.com/smokin-salmon/smoked-salmon
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
	uv tool install git+https://github.com/smokin-salmon/smoked-salmon
	```

#### macOS
1. Install Homebrew (if you haven't already):
    ```bash
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    ```

2. Install system packages using Homebrew:
    ```bash
    brew install sox flac mp3val curl unzip lame
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
1. Run salmon for the first time and follow the instructions to create a default configuration:
	```
	salmon-user@salmon:~$ salmon
	Could not find configuration path at /home/salmon-user/.config/smoked-salmon/config.toml.
	Do you want smoked-salmon to create a default config file at /home/salmon-user/.config/smoked-salmon/config.default.toml? [y/N]:
	```

2. Copy the default config to `~/.config/smoked-salmon/config.toml`.
	```
	cp ~/.config/smoked-salmon/config.default.toml ~/.config/smoked-salmon/config.toml
	```

3. Edit the `config.toml` file with your preferred text editor to add your API keys, session cookies and update your preferences (see the [Configuration Wiki](https://github.com/smokin-salmon/smoked-salmon/wiki/Configuration)).

4. Use the `checkconf` command to verify that the connection to the trackers is working:

	```
	salmon checkconf
	```

5. Use the `health` command to verify that all necesasary command line dependencies are installed:

	```
	salmon health
	```

### 🐳 Docker Installation

A Docker image is generated per release.  
**Disclaimer**: I am not actively using the docker image myself, feedback is appreciated regarding that guide.

1. Pull the latest image:

   ```bash
   docker pull ghcr.io/smokin-salmon/smoked-salmon:latest
   ```

2. Copy the content of the file [`config.toml`](https://github.com/smokin-salmon/smoked-salmon/blob/master/data/config.default.toml) to a location on your host server.  
   Edit the `config.toml` file with your preferred text editor to add your API keys, session cookies and update your preferences (see the [Configuration Wiki](https://github.com/smokin-salmon/smoked-salmon/wiki/Configuration)).

3. Configure rclone if needed. The Docker Compose configuration expects an rclone configuration file. You can get the path to your rclone config file by running `rclone config file` on your host system.

---

### 🔁 Recommended Docker Operation Order

1. **Check Configuration** -> **Run Migration** -> **Run the Web UI**  
   Run the container with the `checkconf` command to verify that the connection to the trackers is working:

   ```bash
   docker run --rm -it --network=host \
   -v /path/to/your/music:/app/.music \
   -v /path/to/your/config.toml/directory:/root/.config/smoked-salmon/ \
   -v /path/to/your/smoked.db/directory:/root/.local/share/smoked-salmon/ \
   -v /path/to/your/generated/dottorrents:/app/.torrents \
   -v /get/this/from/"rclone config file":/root/.config/rclone/rclone.conf  # Optional: only if using rclone features \
   ghcr.io/smokin-salmon/smoked-salmon:latest checkconf
   ```

   If the configuration is valid, use the `migrate` command to initialize or upgrade the database schema:
   Once migration is complete, you may launch container in persistent mode with `web` command.

2. **Connect to the Running Container**  
   To manually execute operations inside the container(`web` command required), connect via SSH and run:

   ```bash
   docker exec -it smoked-salmon /bin/sh
   ```

   Then, inside the container, you can run the commands like this:

   ```bash
   .venv/bin/salmon up "/path/to/your/music" -s WEB
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

### 📦 Portainer Stack Alternative

If using Portainer or Docker Compose, here's an example stack for persistent usage:

```yaml
version: "3"
services:
  smoked-salmon:
    image: ghcr.io/smokin-salmon/smoked-salmon:latest
    container_name: smoked-salmon
    network_mode: host
    restart: unless-stopped
    volumes:
      - /path/to/your/music:/app/.music
      - /path/to/your/config.toml/directory:/root/.config/smoked-salmon/
      - /path/to/your/smoked.db/directory:/root/.local/share/smoked-salmon/
      - /path/to/your/generated/dottorrents:/app/.torrents
      - /get/this/from/"rclone config file":/root/.config/rclone/rclone.conf  # Optional: only if using rclone features
    command: web
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

On the first run, you will need to create the database:
```bash
salmon migrate
```

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
