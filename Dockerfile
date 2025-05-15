# Dockerfile created by Bendall and @kieraneglin
# 
# Step by step:
#
# 1. Create docker image:
# $ docker image build -t salmon .
#   - You can pass in `uid` and `gid` as `--build-arg`s if you need to change the user and group IDs.
#
# 2. Alias docker command and replace /path/to with your desired path. (Add to .bashrc or whatever):
# alias salmon='docker run --rm -it -v /path/to/config.py:/salmon/config.py -v /path/to/accounts.json:/salmon/accounts.json -v /path/to/downloads:/downloads -v /path/to/queue:/queue -v /path/to/torrents:/torrents -p 55110:55110/tcp salmon'
#
# Done

FROM python:3.13-slim

WORKDIR /salmon

COPY ./ /salmon

ENV LANGUAGE="en_US.UTF-8" \
    LANG="en_US.UTF-8"

RUN apt-get update \
    && echo "----- Installing dependencies" \
    && apt-get install -y gcc sox flac mp3val vim nano ffmpeg libsox-fmt-mp3 lame rclone curl locales \
    && curl -L "https://github.com/KyokoMiki/cambia/releases/latest/download/cambia-ubuntu-latest" -o "/usr/local/bin/cambia" \
    && chmod +x "/usr/local/bin/cambia" \
    && echo "----- Generate locale" \
    && locale-gen en_US.UTF-8 \
    && echo "----- Installing python requirements" \
    && pip install -r requirements.txt \
    && echo "----- Initializing salmon" \
    && mkdir config \
    && cp config.py.txt config/config.py \
    && cp seedbox.json.txt config/seedbox.json \
    && python run.py migrate \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 55110

VOLUME ["/torrents", "/queue", "/salmon/config"]

ENTRYPOINT ["python", "run.py"]
