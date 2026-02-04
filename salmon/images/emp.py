import contextlib
import mimetypes
import os
from random import choice

import requests
from bs4 import BeautifulSoup

from salmon.constants import UAGENTS
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

mimetypes.init()

HEADERS = {
    "User-Agent": choice(UAGENTS),
    "referrer": "https://jerking.empornium.ph/",
    "Accept": "application/json",
    "Linx-Expiry": "0",
}
AUTH_TOKEN = None
cookies = {"AGREE_CONSENT": "1", "PHPSESSID": "45onca6s8hi8oi07ljqla31gfu"}


class ImageUploader(BaseImageUploader):
    def __init__(self):
        "When class is first used we need to fetch an authtoken."
        global AUTH_TOKEN
        if not AUTH_TOKEN:
            resp = requests.get("https://jerking.empornium.ph", cookies=cookies)
            soup = BeautifulSoup(resp.text, "html.parser")
            AUTH_TOKEN = soup.find(attrs={"name": "auth_token"})["value"]
        self.auth_token = AUTH_TOKEN
        if not self.auth_token:
            raise ImageUploadFailed

    def upload_file(self, filename):
        # The ExitStack closes files for us when the with block exits
        with contextlib.ExitStack() as stack:
            open_file = stack.enter_context(open(filename, "rb"))
            mime_type, _ = mimetypes.guess_type(filename)
            if not mime_type or mime_type.split("/")[0] != "image":
                raise ValueError(f"Unknown image file type {mime_type}")
            ext = os.path.splitext(filename)[1]
            return self._perform((filename, open_file, mime_type), ext)

    def _perform(self, file_, ext):
        url = "https://jerking.empornium.ph/json"
        files = {"source": file_}
        data = {
            "action": "upload",
            "type": "file",
            "auth_token": self.auth_token,
        }

        resp = requests.post(url, headers=HEADERS, data=data, cookies=cookies, files=files)
        # print(resp.json())
        if resp.status_code == requests.codes.ok:
            try:
                resp_data = resp.json()
                return resp_data["image"]["url"], None
            except ValueError as e:
                raise ImageUploadFailed(f"Failed decoding body:\n{e}\n{resp.content}") from e
        else:
            raise ImageUploadFailed(f"Failed. Status {resp.status_code}:\n{resp.content}")
