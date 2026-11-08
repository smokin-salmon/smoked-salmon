"""Whether a path is inside a folder. A leaf module: it imports nothing of salmon's, so anything can import it."""

import os


def is_within(path: str, folder: str) -> bool:
    """Whether path is folder or inside it, by their text alone: it never looks at the disk and never resolves.

    Give both resolved (`os.path.realpath`): as text, /music/../x is inside /music. commonpath: /music-old is not in
    /music.
    """
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # Different drives on Windows, or one path relative.
        return False
