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
ARG SALMON_RCLONE_VERSION=1.75.1
ARG SALMON_RCLONE_SHA256_AMD64=982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab
ARG SALMON_RCLONE_SHA256_ARM64=03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9

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

# Ensure app directory and its contents are writable by any user
RUN mkdir -p /app/.music /app/.torrents && chmod -R 777 /app

# Expose port for web interface
EXPOSE 55110

# Set the entrypoint to run the 'salmon' script
ENTRYPOINT ["salmon"]
