from salmon import cfg
from salmon.trackers.base import BaseGazelleApi


class RedApi(BaseGazelleApi):
    def __init__(self):
        self.site_code = "RED"
        self.base_url = "https://redacted.sh"
        self.tracker_url = "https://flacsfor.me"
        self.site_string = "RED"
        if cfg.tracker.red:
            red_cfg = cfg.tracker.red

            self.cookie = red_cfg.session
            if red_cfg.api_key:
                self.api_key = red_cfg.api_key

            if red_cfg.dottorrents_dir:
                self.dot_torrents_dir = red_cfg.dottorrents_dir
            else:
                self.dot_torrents_dir = cfg.directory.dottorrents_dir

        super().__init__()
