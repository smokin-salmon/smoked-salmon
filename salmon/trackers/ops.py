import click
import requests
import asyncio
from requests.exceptions import ConnectTimeout, ReadTimeout

from salmon.trackers.base import BaseGazelleApi
from salmon import config
from salmon.errors import (
    LoginError,
    RequestError,
    RequestFailedError,
)

loop = asyncio.get_event_loop()

class OpsApi(BaseGazelleApi):
    def __init__(self):
        self.site_code = 'OPS'
        self.base_url = 'https://orpheus.network'
        self.tracker_url = 'https://home.opsfet.ch'
        self.site_string = 'OPS'
        if config.OPS_DOTTORRENTS_DIR:
            self.dot_torrents_dir = config.OPS_DOTTORRENTS_DIR
        else:
            self.dot_torrents_dir = config.DOTTORRENTS_DIR

        self.cookie = config.OPS_SESSION
        if config.OPS_API_KEY:
            self.api_key = config.OPS_API_KEY

        super().__init__()

    async def api_key_upload(self, data, files):
        """Attempt to upload a torrent to the site.
        using the API"""
        url = self.base_url + "/ajax.php?action=upload"
        data["auth"] = self.authkey
        # Shallow copy. We don't want the future requests to send the api key.
        api_key_headers = {**self.headers, "Authorization": self.api_key}
        resp = await loop.run_in_executor(
            None,
            lambda: self.session.post(
                url, data=data, files=files, headers=api_key_headers
            ),
        )
        resp = resp.json()
        # print(resp) debug
        try:
            if resp["status"] != "success":
                raise RequestError(f"API upload failed: {resp['error']}")
            elif resp["status"] == "success":
                if (
                    'requestId' in resp['response'].keys()
                    and resp['response']['requestId']
                ):
                    if resp['response']['requestId'] == -1:
                        click.secho(
                            "Request fill failed!", fg="red",
                        )
                    else:
                        click.secho(
                            "Filled request: "
                            + self.request_url(resp['response']['requestId']),
                            fg="green",
                        )
                return resp["response"]["torrentId"], resp["response"]["groupId"]
        except TypeError:
            raise RequestError(f"API upload failed, response text: {resp.text}")