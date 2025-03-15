# smoked-salmon

A tool to assist with finding music and uploading it to RED.

## Setup Guide

### Installation

#### Recommended: Using uv (Python Package Manager)

**HIGHLY RECOMMEND** using [uv](https://github.com/astral-sh/uv) for setting up salmon. uv is significantly faster than pip and provides better dependency resolution.

1. Install uv:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. Clone the repository:
```bash
git clone https://github.com/miandru/smoked-salmon.git
cd smoked-salmon
```

3. Install dependencies and create virtual environment:

```bash
uv sync
```

5. Configure salmon:
```bash
cp config.py.txt config.py
```
Edit `config.py` with your preferred text editor to add your API keys and preferences.
<details>
<summary>Alternative: Using pip </summary>

1. Clone the repository:
```bash
git clone https://github.com/ligh7s/smoked-salmon.git
cd smoked-salmon
```

2. Create and activate a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows use: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Configure salmon:
```bash
cp config.py.txt config.py
```
Edit `config.py` with your preferred text editor to add your API keys and preferences.
</details>

### Usage

Basic usage:
```bash
cd ~/smoked-salmon
uv run salmon
```
Alernatively:
```bash
source ~/smoked-salmon/bin/activate
salmon --help
```

For help with available commands:
```bash
uv run salmon --help
```

Common commands:
- Search for music: `uv run salmon search "artist name" "album name"`
- Upload music: `uv run salmon upload /path/to/album`
- Generate spectrals: `uv run salmon spectral /path/to/album`
- Check for requests: `uv run salmon request "artist name" "album name"`
- Browse WebUI: Start the WebUI with `uv run salmon web` and navigate to http://127.0.0.1:55110 in your browser

### Updating

With uv:
```bash
git pull
uv sync
```

With pip:
```bash
git pull
pip install -r requirements.txt
```

<details>
<summary>Old description</summary>
    All information pertaining to its use can be found in the wiki.

    Wiki: https://github.com/ligh7s/smoked-salmon/wiki

    ### Plugin Installation

    Clone plugins into the plugins/ folder. Salmon will automatically detect
    and import them. Their CLI commands should appear when salmon is next ran.

    ### Colors

    The different terminal colors used throughout salmon will generally stick to the
    following pattern of use.

    - **Default** - Information on what salmon is doing
    - **Red** - Failure, urgent, requires attention
    - **Green** - Success, no problems found
    - **Yellow** - Information block headers
    - **Cyan** - Section headers
    - **Magenta** - User prompts, attention please!
</details>

### Testimonials

```
Salmon filled the void in my heart. I no longer chase after girls. ~boot
With the help of salmon, I overcame my addition to kpop thots. ~b
I warn 5 people every day on the forums using salmon! ~jon
```

---

The Salmon Icon made by <a href="http://www.freepik.com" title="Freepik">Freepik</a> from
<a href="https://www.flaticon.com/" title="Flaticon">www.flaticon.com</a> is licensed by
<a href="http://creativecommons.org/licenses/by/3.0/" title="Creative Commons BY 3.0"
target="_blank">CC 3.0 BY</a>.
