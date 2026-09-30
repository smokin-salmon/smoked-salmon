# ===========================================
# Stage 1: Builder - Install dependencies and build the project
# ===========================================
FROM python:3.13-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Set environment variables for uv optimization
ENV UV_CACHE_DIR=/opt/uv-cache
ENV UV_PYTHON_CACHE_DIR=/opt/uv-cache/python
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy dependency files first for better layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN --mount=type=cache,target=/opt/uv-cache \
    uv sync --locked --no-install-project --no-editable --no-dev

# Copy source code
COPY . .

# Install the project in non-editable mode for production
RUN --mount=type=cache,target=/opt/uv-cache \
    uv sync --locked --no-editable --no-dev

# ===========================================
# Stage 2: Runtime - Minimal Python slim image
# ===========================================
FROM python:3.13-slim-trixie

# Set working directory
WORKDIR /app

# rclone comes from the official release, not Debian: trixie ships 1.60.1 (2022) with open CVEs
# that will not be fixed there (https://security-tracker.debian.org/tracker/source-package/rclone).
# To bump: set SALMON_RCLONE_VERSION and both sums from
# https://downloads.rclone.org/v<version>/SHA256SUMS (rclone-v<version>-linux-amd64.zip and
# -linux-arm64.zip; verify the file's PGP signature per https://rclone.org/downloads/).
# .github/docker-smoke.sh checks the image reports this version. The ARGs have no RCLONE_ prefix
# because rclone reads every RCLONE_* environment variable as a flag.
ARG TARGETARCH
ARG SALMON_RCLONE_VERSION=1.60.1
ARG SALMON_RCLONE_SHA256_AMD64=fd6bc19cc7fadb13538cc109128bf92ef47762a83a3eaf2ab699b03bb2a1fe32
ARG SALMON_RCLONE_SHA256_ARM64=34cb5687aff755ad7a3d1069b3cb0f5dd0b5b592b4d539ecd6c6a82599131ec7

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    sox libsox-fmt-mp3 flac mp3val curl nano vim \
    ca-certificates lame \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install rclone with a pinned checksum per architecture. unzip is not in the slim image, so
# Python's zipfile extracts the binary.
RUN set -eu; \
    case "$TARGETARCH" in \
        amd64) sha256="$SALMON_RCLONE_SHA256_AMD64" ;; \
        arm64) sha256="$SALMON_RCLONE_SHA256_ARM64" ;; \
        *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    zip="rclone-v${SALMON_RCLONE_VERSION}-linux-${TARGETARCH}.zip"; \
    curl -fsSL -o "/tmp/$zip" "https://downloads.rclone.org/v${SALMON_RCLONE_VERSION}/$zip"; \
    echo "$sha256  /tmp/$zip" | sha256sum -c -; \
    python3 -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1]); open('/usr/local/bin/rclone', 'wb').write(z.read(sys.argv[2]))" \
        "/tmp/$zip" "rclone-v${SALMON_RCLONE_VERSION}-linux-${TARGETARCH}/rclone"; \
    chmod 0755 /usr/local/bin/rclone; \
    rm -f "/tmp/$zip"; \
    rclone version

# Copy the virtual environment from builder stage
COPY --from=builder /app/.venv /app/.venv

# Set environment variables for Python virtual environment
ENV PATH="/app/.venv/bin:$PATH"
# Single mount for config.toml (and rclone.conf, see README); the old
# /root/.config/smoked-salmon/ mount still works as a fallback.
ENV SALMON_CONFIG_DIR=/config
ENV PYTHONDONTWRITEBYTECODE=1

# Only .music and .torrents need writing by an arbitrary uid; config lives in /config.
# Sticky+writable (1777) so a uid can still create a configured relative dir under /app,
# but cannot remove another uid's files. Scoped to these three paths, not recursive, so
# the venv copied in above is not rewritten into a new layer. The build fails if
# anything under /app is not world-readable, since a rootless uid could not run it.
RUN set -e; \
    mkdir -p /app/.music /app/.torrents; \
    chmod 1777 /app /app/.music /app/.torrents; \
    unreadable="$(find /app \( -type f ! -perm -0004 \) -o \( -type d ! -perm -0005 \) | head -20)"; \
    if [ -n "$unreadable" ]; then echo "not world-readable:"; echo "$unreadable"; exit 1; fi

# Expose port for web interface
EXPOSE 55110

# Set the entrypoint to run the 'salmon' script
ENTRYPOINT ["salmon"]
