from typing import Any

import asyncclick as click
from mutagen import File as MutagenFile
from mutagen import flac, id3, mp3, mp4
from mutagen._vorbis import VCommentDict
from mutagen.id3 import ID3Tags
from mutagen.mp4 import MP4Tags

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
    mut: flac.FLAC | mp3.MP3 | mp4.MP4 | None

    def __init__(self, filepath: str) -> None:
        super().__setattr__("mut", MutagenFile(filepath))

    def __getattr__(self, attr: str) -> Any:
        mut = self.mut
        if mut is None:
            return None
        try:
            if isinstance(mut, flac.FLAC):
                if attr in {"artist", "genre"}:
                    return list(mut[TAG_FIELDS["FLAC"][attr]]) or []
                return "; ".join(mut[TAG_FIELDS["FLAC"][attr]]) or None
            elif isinstance(mut, mp3.MP3):
                return self.parse_tag(attr, "MP3")
            elif isinstance(mut, mp4.MP4):
                tag = self.parse_tag(attr, "AAC")
                return tag
        except KeyError:
            return None
        return None

    def parse_tag(self, attr: str, format: str) -> Any:
        """Parse a tag value from MP3 or AAC format.

        Args:
            attr: The attribute name to parse (e.g., 'artist', 'tracknumber').
            format: The format type ('MP3' or 'AAC').

        Returns:
            The parsed tag value, or None if not found.
        """
        mut = self.mut
        if mut is None:
            return None
        raw_tags = mut.tags
        if raw_tags is None:
            return None
        # Verify tags type based on format
        if not isinstance(raw_tags, (ID3Tags, MP4Tags)):
            return None
        tags: ID3Tags | MP4Tags = raw_tags
        fields = TAG_FIELDS[format][attr]
        for field in fields:
            try:
                if attr in {"tracknumber", "tracktotal", "discnumber", "disctotal"}:
                    try:
                        # MP3: ID3 frames have .text attribute
                        frame = tags[field]
                        val = str(frame.text[0])
                        if "number" in attr:
                            return val.split("/")[0]
                        elif "total" in attr and "/" in val:
                            return val.split("/")[1]
                    except (AttributeError, KeyError):
                        # AAC: MP4 tags are tuples like (track, total)
                        tag_val = tags[field]
                        number, total = tag_val[0]
                        return (number if "number" in attr else total) or None
                try:
                    if attr in {"artist", "genre"}:
                        try:
                            # MP3: ID3 frames have .text attribute
                            frame = tags[field]
                            return list(frame.text) or []
                        except AttributeError:
                            # AAC: MP4 tags are lists directly
                            tag_val = tags[field]
                            return list(tag_val) or []
                    try:
                        # MP3: ID3 frames have .text attribute
                        frame = tags[field]
                        return "; ".join(frame.text) or None
                    except AttributeError:
                        # AAC: MP4 tags are lists directly
                        tag_val = tags[field]
                        return tag_val[0] or None
                except TypeError:
                    # Handle ID3TimeStamp which has get_text() method
                    frame = tags[field]
                    return frame.text[0].get_text()
            except KeyError:
                pass
        return None

    def __setattr__(self, key: str, value: Any) -> None:
        mut = self.mut
        if mut is None:
            super().__setattr__(key, value)
            return
        try:
            if isinstance(mut, flac.FLAC):
                raw_tags = mut.tags
                if raw_tags is not None and isinstance(raw_tags, VCommentDict):
                    # FLAC tags use VCommentDict which supports dynamic keys
                    raw_tags[TAG_FIELDS["FLAC"][key]] = value
            elif isinstance(mut, mp3.MP3):
                self.set_mp3_tag(key, value)
            elif isinstance(mut, mp4.MP4):
                self.set_aac_tag(key, value)
        except KeyError:
            super().__setattr__(key, value)

    def set_mp3_tag(self, key: str, value: Any) -> None:
        mut = self.mut
        if mut is None or not isinstance(mut, mp3.MP3):
            return
        if not mut.tags:
            mut.tags = id3.ID3()
        tags = mut.tags
        if not isinstance(tags, id3.ID3):
            return
        if key in {"tracknumber", "discnumber"}:
            tag_key = TAG_FIELDS["MP3"][key][0]
            try:
                _, total = tags[tag_key].text[0].split("/")
                value = f"{value}/{total}"
            except (ValueError, KeyError, AttributeError):
                pass
            frame = getattr(id3, tag_key)(text=value)
            tags.delall(tag_key)
            tags.add(frame)
        elif key in {"tracktotal", "disctotal"}:
            tag_key = TAG_FIELDS["MP3"][key][0]
            try:
                track, _ = tags[tag_key].text[0].split("/")
            except ValueError:
                track = tags[tag_key].text[0]
            except (KeyError, AttributeError):  # Well fuck...
                return
            frame = getattr(id3, tag_key)(text=f"{track}/{value}")
            tags.delall(tag_key)
            tags.add(frame)
        else:
            try:
                tag_key, desc = TAG_FIELDS["MP3"][key][0].split(":")
                frame = getattr(id3, tag_key)(desc=desc, text=value)
                tags.add(frame)
            except ValueError:
                tag_key = TAG_FIELDS["MP3"][key][0]
                frame = getattr(id3, tag_key)(text=value)
                tags.delall(tag_key)
                tags.add(frame)

    def set_aac_tag(self, key: str, value: Any) -> None:
        mut = self.mut
        if mut is None or not isinstance(mut, mp4.MP4) or mut.tags is None:
            return
        tags = mut.tags
        tag_key = TAG_FIELDS["AAC"][key][0]
        if key in {"tracknumber", "discnumber"}:
            try:
                _, total = tags[tag_key][0]
            except (ValueError, KeyError):
                total = 0
            try:
                tags[tag_key] = [(int(value), int(total))]
            except ValueError as e:
                click.secho("Can't have non-numeric AAC number tags, sorry!")
                raise e
        elif key in {"tracktotal", "disctotal"}:
            try:
                track, _ = tags[tag_key][0]
            except (ValueError, KeyError):  # fack
                return
            try:
                tags[tag_key] = [(int(track), int(value))]
            except ValueError as e:
                click.secho("Can't have non-numeric AAC number tags, sorry!")
                raise e
        else:
            tags[tag_key] = value

    def save(self) -> None:
        mut = self.mut
        if mut is not None:
            mut.save()
