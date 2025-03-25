from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import subprocess

from tqdm import tqdm

from salmon import config


def get_audio_files(path):
    """
    Iterate over a path and return all the files that match the allowed
    audio file extensions.
    """
    files = []
    for root, folders, files_ in os.walk(path):
        files += [
            create_relative_path(root, path, f)
            for f in files_
            if os.path.splitext(f.lower())[1] in {".flac", ".mp3", ".m4a"}
        ]
    return sorted(files)


def create_relative_path(root, path, filename):
    """
    Create a relative path to a filename. For example, given:
        root     = '/home/xxx/Tidal/Album/Disc 1'
        path     = '/home/xxx/Tidal/Album'
        filename = '01. Track.flac'
    'Disc 1/01. Track.flac' would be returned.
    """
    return os.path.join(
        root.split(path, 1)[1][1:], filename
    )  # [1:] to get rid of the slash.


def compress(filepath):
    """Re-compress a .flac file with the configured level."""
    with open(os.devnull, "w") as devnull:
        subprocess.call(
            [
                "flac",
                f"-{config.FLAC_COMPRESSION_LEVEL}",
                filepath,
                "-o",
                f"{filepath}.new",
                "--delete-input-file",
            ],
            stdout=devnull,
            stderr=devnull,
        )
    os.rename(f"{filepath}.new", filepath)


def alac_to_flac(filepath):
    """Convert alac to flac"""
    with open(os.devnull, "w") as devnull:
        subprocess.call(
            [
                "ffmpeg",
                # "-y",
                "-i",
                filepath,
                "-acodec",
                "flac",
                f"{filepath}.flac",
                # "--delete-input-file",
            ],
            stdout=devnull,
            stderr=devnull,
        )
    os.rename(f"{filepath}.flac", filepath)

def process_files(files, process_func, desc):
    with ThreadPoolExecutor(max_workers=config.SIMULTANEOUS_THREADS) as executor:
        futures = [executor.submit(process_func, file) for file in files]
        results = []
        for future in tqdm(as_completed(futures), total=len(files), desc=desc, colour="cyan"):
            results.append(future.result())
    return results
