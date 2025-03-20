import re
import subprocess
import os

import click

FLAC_IMPORTANT_REGEXES = [
    re.compile(".+\\.flac: testing,.*\x08ok"),
]

MP3_IMPORTANT_REGEXES = [
    re.compile(r"WARNING: .*"),
    re.compile(r"INFO: .*"),
]


def check_integrity(path):
    if path.lower().endswith(".flac"):
        return _check_flac_integrity(path)
    elif path.lower().endswith(".mp3"):
        return _check_mp3_integrity(path)
    elif os.path.isdir(path):
        integrities_out = []
        integrities = True
        for root, _, figles in os.walk(path):
            for f in figles:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    filepath = os.path.join(root, f)
                    (integrity, integrity_out) = check_integrity(filepath)
                    integrities = integrities and integrity
                    integrities_out.append(integrity_out)
        return(integrities, "\n".join(integrities_out))
    raise click.Abort


def format_integrity(arg):
    return arg[1]


def _check_flac_integrity(path):
    try:
        resp = subprocess.check_output(["flac", "-wt", path], stderr=subprocess.STDOUT)
        important_lines = []
        for line in resp.decode("utf-8").split("\n"):
            for important_line_re in FLAC_IMPORTANT_REGEXES:
                if important_line_re.match(line):
                    important_lines.append(line)
        return (True, "\n".join(important_lines))
    except:
        return (False, "Failed integrity")


def _check_mp3_integrity(path):
    try:
        resp = subprocess.check_output(["mp3val", path])
        important_lines = []
        for line in resp.decode("utf-8").split("\n"):
            for important_line_re in MP3_IMPORTANT_REGEXES:
                if important_line_re.match(line):
                    important_lines.append(line)
        return (True, "\n".join(important_lines))
    except:
        return (False, "Failed integrity")

def sanitize_integrity(path):
    if path.lower().endswith(".flac"):
        return _sanitize_flac(path)
    elif path.lower().endswith(".mp3"):
        return _sanitize_mp3(path)
    elif os.path.isdir(path):
        integrities_out = []
        integrities = True
        for root, _, figles in os.walk(path):
            for f in figles:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    filepath = os.path.join(root, f)
                    integrity = sanitize_integrity(filepath)
                    integrities = integrities and integrity
        return integrities
    raise click.Abort

def _sanitize_flac(path):
    os.rename(path, path + ".corrupted")
    subprocess.run(["flac", path + ".corrupted", "-o", path])
    os.remove(path + ".corrupted")
    subprocess.run(["metaflac", "--dont-use-padding", "--remove", "--block-type=PADDING,PICTURE", path])
    subprocess.run(["metaflac", "--add-padding=8192", path])
    return True

def _sanitize_mp3(path):
    return True
