# Use an official Python runtime as a base image
FROM python:3.12

# Install system dependencies
RUN ARCH=$(dpkg --print-architecture) && \
    apt-get update && apt-get install -y --no-install-recommends \
    sox flac mp3val curl nano vim-tiny \
    ca-certificates && rm -rf /var/lib/apt/lists/* && \
    if [ "$ARCH" = "amd64" ]; then \
        wget https://github.com/shssoichiro/oxipng/releases/download/v9.1.4/oxipng_9.1.4-1_amd64.deb && \
        dpkg -i oxipng_9.1.4-1_amd64.deb; \
    elif [ "$ARCH" = "arm64" ]; then \
        wget https://github.com/shssoichiro/oxipng/releases/download/v9.1.4/oxipng_9.1.4-1_arm64.deb && \
        dpkg -i oxipng_9.1.4-1_arm64.deb; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi

# Download the latest installer
ADD https://astral.sh/uv/install.sh /uv-installer.sh

# Run the installer then remove it
RUN sh /uv-installer.sh && rm /uv-installer.sh

# Ensure the installed binary is on the `PATH`
ENV PATH="/root/.local/bin/:$PATH"

# Set the working directory in the container
WORKDIR /app

# Copy the project files into the container
COPY . /app

# Install the required Python packages
RUN uv sync --no-dev

# Set the entrypoint to run the 'salmon' script
ENTRYPOINT ["uv", "run", "salmon"]
