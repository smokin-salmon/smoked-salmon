import requests

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

HEADERS = {
    "X-API-Key": cfg.image.ptscreens_key
}


class ImageUploader(BaseImageUploader):
    def _perform(self, file_, ext):
        url = "https://ptscreens.com/api/1/upload"
        files = {'source': file_}
        resp = requests.post(url, headers=HEADERS, files=files)
        if resp.status_code == requests.codes.ok:
            try:
                r = resp.json()
                if 'image' in r:
                    url = r['image'].get('url')
                    return url, None
                raise ImageUploadFailed("Missing image data in response")
            except ValueError as e:
                raise ImageUploadFailed(
                    f"Failed decoding body:\n{e}\n{resp.content}"
                ) from e
        else:
            raise ImageUploadFailed(
                f"Failed. Status {resp.status_code}:\n{resp.content}"
            )
