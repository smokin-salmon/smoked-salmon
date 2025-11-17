import requests

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

HEADERS = {"referer": "https://imgbb.com/", "User-Agent": cfg.upload.user_agent}


class ImageUploader(BaseImageUploader):
    def _perform(self, file_, ext):
        data = {"key": cfg.image.imgbb_key}
        url = "https://api.imgbb.com/1/upload"
        files = {"image": file_}
        resp = requests.post(url, headers=HEADERS, data=data, files=files)
        if resp.status_code == requests.codes.ok:
            try:
                return resp.json()["data"]["url"], None
            except (ValueError, KeyError, TypeError) as e:
                raise ImageUploadFailed(f"Failed decoding body:\n{e}\n{resp.content}") from e
        else:
            raise ImageUploadFailed(f"Failed. Status {resp.status_code}:\n{resp.content}")
