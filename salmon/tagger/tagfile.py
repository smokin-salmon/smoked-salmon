import asyncclick as click
import mutagen
from mutagen import id3

TAG_FIELDS = {
    "FLAC": {
        "album": "album",
        "date": "date",
        "upc": "upc",
        "label": "label",
        "catno": "catalognumber",
        "genre": "genre",
        "tracknumber": "tracknumber",
        "discnumber": "discnumber",
        "tracktotal": "tracktotal",
        "disctotal": "disctotal",
        "artist": "artist",
        "title": "title",
        "replay_gain": "replaygain_track_gain",
        "peak": "replaygain_track_peak",
        "isrc": "isrc",
        "comment": "comment",
        "albumartist": "albumartist",
    },
    "MP3": {
        "album": ["TALB"],
        "date": ["TDRC", "TYER"],
        "label": ["TPUB"],
        "genre": ["TCON"],
        "tracknumber": ["TRCK"],  # Special
        "tracktotal": ["TRCK"],
        "discnumber": ["TPOS"],
        "disctotal": ["TPOS"],
        "artist": ["TPE1"],
        "title": ["TIT2"],
        "isrc": ["TSRC"],
        "comment": ["COMM"],
        "albumartist": ["TPE2"],
    },
    "AAC": {
        "album": ["\xa9alb"],
        "date": ["\xa9day"],
        "genre": ["\xa9gen"],
        "tracknumber": ["trkn"],
        "tracktotal": ["trkn"],
        "discnumber": ["disk"],
        "disctotal": ["disk"],
        "artist": ["\xa9ART"],
        "title": ["\xa9nam"],
        "comment": ["\xa9cmt"],
        "albumartist": ["aART"],
    },
}


class TagFile:
    def __init__(self, filepath):
        super().__setattr__("mut", mutagen.File(filepath))

    def __getattr__(self, attr):
        try:
            if isinstance(self.mut, mutagen.flac.FLAC):
                if attr in {"artist", "genre"}:
                    return list(self.mut[TAG_FIELDS["FLAC"][attr]]) or []
                return "; ".join(self.mut[TAG_FIELDS["FLAC"][attr]]) or None
            elif isinstance(self.mut, mutagen.mp3.MP3):
                return self.parse_tag(attr, "MP3")
            elif isinstance(self.mut, mutagen.mp4.MP4):
                tag = self.parse_tag(attr, "AAC")
                return tag
        except KeyError:
            return None

    def parse_tag(self, attr, format):
        fields = TAG_FIELDS[format][attr]
        for field in fields:
            try:
                if attr in {"tracknumber", "tracktotal", "discnumber", "disctotal"}:
                    try:
                        val = str(self.mut.tags[field].text[0])
                        if "number" in attr:
                            return val.split("/")[0]
                        elif "total" in attr and "/" in val:
                            return val.split("/")[1]
                    except (AttributeError, KeyError):
                        number, total = self.mut.tags[field][0]
                        return (number if "number" in attr else total) or None
                try:
                    if attr in {"artist", "genre"}:
                        try:
                            return list(self.mut.tags[field].text) or []
                        except AttributeError:
                            return list(self.mut.tags[field]) or []
                    try:
                        return "; ".join(self.mut.tags[field].text) or None
                    except AttributeError:
                        return self.mut.tags[field][0] or None
                except TypeError:
                    return self.mut.tags[field].text[0].get_text()
            except KeyError:
                pass
        return None

    def __setattr__(self, key, value):
        try:
            if isinstance(self.mut, mutagen.flac.FLAC):
                self.mut.tags[TAG_FIELDS["FLAC"][key]] = value
            elif isinstance(self.mut, mutagen.mp3.MP3):
                self.set_mp3_tag(key, value)
            elif isinstance(self.mut, mutagen.mp4.MP4):
                self.set_aac_tag(key, value)
        except KeyError:
            return super().__setattr__(key, value)

    def set_mp3_tag(self, key, value):
        if not self.mut.tags:
            self.mut.tags = mutagen.id3.ID3()
        if key in {"tracknumber", "discnumber"}:
            tag_key = TAG_FIELDS["MP3"][key][0]
            try:
                _, total = self.mut.tags[tag_key].text[0].split("/")
                value = f"{value}/{total}"
            except (ValueError, KeyError):
                pass
            frame = getattr(id3, tag_key)(text=value)
            self.mut.tags.delall(tag_key)
            self.mut.tags.add(frame)
        elif key in {"tracktotal", "disctotal"}:
            tag_key = TAG_FIELDS["MP3"][key][0]
            try:
                track, _ = self.mut.tags[tag_key].text[0].split("/")
            except ValueError:
                track = self.mut.tags[tag_key].text[0]
            except KeyError:  # Well fuck...
                return
            frame = getattr(id3, tag_key)(text=f"{track}/{value}")
            self.mut.tags.delall(tag_key)
            self.mut.tags.add(frame)
        else:
            try:
                tag_key, desc = TAG_FIELDS["MP3"][key][0].split(":")
                frame = getattr(id3, tag_key)(desc=desc, text=value)
                self.mut.tags.add(frame)
            except ValueError:
                tag_key = TAG_FIELDS["MP3"][key][0]
                frame = getattr(id3, tag_key)(text=value)
                self.mut.tags.delall(tag_key)
                self.mut.tags.add(frame)

    def set_aac_tag(self, key, value):
        tag_key = TAG_FIELDS["AAC"][key][0]
        if key in {"tracknumber", "discnumber"}:
            try:
                _, total = self.mut.tags[tag_key][0]
            except (ValueError, KeyError):
                total = 0
            try:
                self.mut.tags[tag_key] = [(int(value), int(total))]
            except ValueError as e:
                click.secho("Can't have non-numeric AAC number tags, sorry!")
                raise e
        elif key in {"tracktotal", "disctotal"}:
            try:
                track, _ = self.mut.tags[tag_key][0]
            except (ValueError, KeyError):  # fack
                return
            try:
                self.mut.tags[tag_key] = [(int(track), int(value))]
            except ValueError as e:
                click.secho("Can't have non-numeric AAC number tags, sorry!")
                raise e
        else:
            self.mut.tags[tag_key] = value

    def save(self):
        self.mut.save()
