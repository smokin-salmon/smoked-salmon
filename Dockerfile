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

FROM python:3.11-slim-buster

ARG uid=1000
ARG gid=1000

WORKDIR /salmon

COPY ./ /salmon

RUN apt-get update \
    && echo "----- Installing dependencies" \
    && apt-get install -y gcc sox flac mp3val \
    && echo "----- Installing python requirements" \
    && pip install --trusted-host pypi.python.org -r requirements.txt \
    && echo "----- Initializing salmon" \
    # If `WEB_HOST` exists in config.py.txt, leave it alone. Otherwise append `WEB_HOST = '0.0.0.0'`
    && grep -q "WEB_HOST" config.py.txt || echo "\nWEB_HOST = '0.0.0.0'" >> config.py.txt \
    && cp config.py.txt config.py \
    && python run.py migrate \
    && echo "----- Adding salmon user and group and chown" \
    && groupadd -r salmon -g ${gid} \
    && useradd --no-log-init -MNr -g ${gid} -u ${uid} salmon \
    && chown salmon:salmon -R /salmon \
    && apt-get remove -y gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

USER salmon:salmon

EXPOSE 55110

VOLUME ["/downloads", "/torrents", "/queue"]

ENTRYPOINT ["python", "run.py"]
