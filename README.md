[![Build and Publish Docker Image](https://github.com/smokin-salmon/smoked-salmon/actions/workflows/docker-image.yml/badge.svg)](https://github.com/smokin-salmon/smoked-salmon/actions/workflows/docker-image.yml) [![Linting](https://github.com/smokin-salmon/smoked-salmon/actions/workflows/lint.yml/badge.svg?branch=master)](https://github.com/smokin-salmon/smoked-salmon/actions/workflows/lint.yml)

# 🐟 smoked-salmon  

A simple tool to take the work out of uploading on Gazelle-based trackers. It generates spectrals, gathers metadata, allows re-tagging/renaming files, and automates the upload process.

## 🌟 Features  

- **Interactive Uploading** – Supports **multiple trackers** (RED / OPS).
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
- **Update Notifications** – Informs users when a new version is available.

## 📥 Installation  

Installation instructions can be found on the [Wiki](https://github.com/miandru/smoked-salmon/wiki/Installation).

### 🔹 Manual Installation  
Requires Python 3.12+ and [`uv`](https://github.com/astral-sh/uv) for dependency management.  

1. Install system packages and uv:
    ```bash
    sudo apt install sox flac mp3val git wget curl
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

If you want to enable spectrals compression (~30% gain in size), you also need to install [oxipng](https://github.com/shssoichiro/oxipng). Follow the installation instructions on their repository. On Debian/Ubuntu systems, you can typically install it with (check if this is the latest version):

    ```wget https://github.com/shssoichiro/oxipng/releases/download/v9.1.4/oxipng_9.1.4-1_amd64.deb && sudodpkg -i oxipng_9.1.4-1_amd64.deb\n``` 

2. Clone the repository:
    ```bash
    git clone https://github.com/smokin-salmon/smoked-salmon.git
    cd smoked-salmon
    ```

3. Install python dependencies and create virtual environment:
    ```bash
    uv sync
    ```

5. Configure salmon:
    ```bash
    cp config.py.txt config.py
    ```

Edit the `config.py` file with your preferred text editor to add your API keys, session cookies and update your preferences (see the [Configuration Wiki](https://github.com/smokin-salmon/smoked-salmon/wiki/Configuration)).

6. Use the `checkconf` command to verify that the connection to the trackers is working:
    ```bash
    .venv/bin/salmon checkconf
    ```

### 🐳 Docker Installation
A Docker image is generated per release.
**Disclaimer**: I am not actively using the docker image myself, feedback is appreciated regarding that guide.

1. Pull the latest image:
    ```bash
    docker pull ghcr.io/smokin-salmon/smoked-salmon:latest
    ```

Copy the content of the file [`config.py`](https://github.com/smokin-salmon/smoked-salmon/blob/master/config.py.txt) to a location on your host server.
Edit the `config.py` file with your preferred text editor to add your API keys, session cookies and update your preferences (see the [Configuration Wiki](https://github.com/smokin-salmon/smoked-salmon/wiki/Configuration)).

2. Run the container with the `checkconf` command to verify that the connection to the trackers is workin:
    ```bash
    docker run --rm -it  --network=host \
    -v /path/to/your/music:/data \
    -v /path/to/your/config.py:/app/config.py \
    -v /path/to/your/smoked.db:/app/smoked.db \
    ghcr.io/smokin-salmon/smoked-salmon:latest checkconf
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

For ease of use, add an alias to your .bashrc (or adapt for your favorite shell):
```bash
echo "alias salmon='/path/to/smoked-salmon/.venv/bin/salmon'" >> ~/.bashrc
source ~/.bashrc
```

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

To start an upload (with the WEB source):
```bash
salmon up /data/path/to/album -s WEB
```

You can get help directly from the CLI by appending --help to any command. This is especially useful for the up command which has a lot of possible options.

### 🌐 Spectral Web Interface
Spectrals are viewable via a built-in web server. By default, access it at: http://localhost:55110/spectrals

## 🔄 Updating

For **manual installs**:
```bash
cd smoked-salmon
git pull
uv sync
```

For **Docker users**:
```bash
docker pull smokin-salmon/smoked-salmon:latest
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
* Further development & maintenance by elghoto, xmoforf, miandru, redusys and others. Keeping the dream alive.
