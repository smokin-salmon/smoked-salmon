"""The guided first run: what salmon does when it finds no config (#761).

In a terminal, salmon asks for the two folders and the cookie (and API key) of each tracker, writes the short
config (config.default.toml) with them, and offers the connection check ``salmon checkconf -t`` runs. Without a
terminal (Docker without ``-it``, a service), it copies the short config and says what to fill in. It never
overwrites a file.

Every question goes through ``salmon.interaction``, so salmon web can run the same flow later. This runs while
``salmon`` is still importing, before ``salmon.cfg`` exists: nothing here reads it until the config is written.
"""

import os
import re
import sys
from pathlib import Path
from typing import Any

import asyncclick as click
import msgspec

from salmon import interaction
from salmon.config.validations import Cfg

TRACKERS = ("RED", "OPS", "DIC")
# DIC does not take API keys (trackers/dic.py): not asked.
TAKES_API_KEY = frozenset({"RED", "OPS"})
SITES = {"RED": "redacted.sh", "OPS": "orpheus.network", "DIC": "dicmusic.com"}

FOLDERS = (
    ("download_directory", "Folder salmon copies albums into, usually your torrent client's download folder"),
    ("dottorrents_dir", "Folder salmon writes .torrent files to, such as your torrent client's watch folder"),
)


class TrackerAnswer(msgspec.Struct, frozen=True):
    code: str
    session: str
    api_key: str | None = None


class Answers(msgspec.Struct, frozen=True):
    download_directory: str
    dottorrents_dir: str
    trackers: list[TrackerAnswer]


def is_terminal() -> bool:
    """Whether someone can answer: stdin and stdout are both a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def write_new(dest: Path, text: str) -> None:
    """Write a file that must not exist yet (FileExistsError if it does), creating its folder."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("x", encoding="utf-8", newline="\n") as f:
        f.write(text)


def copy_and_explain(template: Path, dest: Path) -> None:
    """Without a terminal: copy the short config and say what to fill in before salmon can start."""
    write_new(dest, template.read_text(encoding="utf-8"))
    click.secho(f"Created a config to fill in at {dest}. salmon cannot start until it is edited:", fg="yellow")
    click.echo("  1. Uncomment the [tracker.<name>] block of each tracker you use and set its session cookie.")
    click.echo("  2. Set download_directory and dottorrents_dir to folders that exist.")
    click.echo("  Then run salmon again. In a terminal, salmon asks for these itself when there is no config.")


def _toml_line(key: str, value: str) -> str:
    """``key = value`` as a TOML writer quotes it: any string decodes back unchanged."""
    return msgspec.toml.encode({key: value}).decode("utf-8").strip()


def _toml_list_line(key: str, values: list[str]) -> str:
    """``key = ["a", "b"]`` on one line, each string quoted as ``_toml_line`` quotes it."""
    quoted = [_toml_line(key, value).split(" = ", 1)[1] for value in values]
    return f"{key} = [{', '.join(quoted)}]"


def _replace_once(pattern: str, text: str, replace: Any) -> str:
    new, count = re.subn(pattern, replace, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"The short config no longer matches {pattern!r}: cannot fill it in.")
    return new


def _tracker_block(answer: TrackerAnswer) -> Any:
    """Uncomment the tracker's block in the short config and set its values."""

    def replace(match: re.Match[str]) -> str:
        lines = [f"[tracker.{answer.code.lower()}]"]
        for line in match.group("keys").splitlines():
            key = line.removeprefix("# ").split("=", 1)[0].strip()
            if key == "session":
                lines.append(_toml_line("session", answer.session))
            elif key == "api_key" and answer.api_key:
                lines.append(_toml_line("api_key", answer.api_key))
            else:
                lines.append(line)
        return "\n".join(lines) + "\n"

    return replace


def render(template_text: str, answers: Answers) -> str:
    """The short config with the answers in it: the two folders, the trackers' blocks and default_trackers."""
    text = template_text
    for key in ("download_directory", "dottorrents_dir"):
        line = _toml_line(key, getattr(answers, key))
        text = _replace_once(rf"^{key} = .*$", text, lambda _, line=line: line)
    codes = [answer.code for answer in answers.trackers]
    default_trackers = _toml_list_line("default_trackers", codes)
    text = _replace_once(r"^\[tracker\]$", text, lambda _: f"[tracker]\n{default_trackers}\n")
    for answer in answers.trackers:
        code = re.escape(answer.code.lower())
        text = _replace_once(rf"^# \[tracker\.{code}\]\n(?P<keys>(?:# \w+ = .*\n)*)", text, _tracker_block(answer))
    return text


async def ask_folder(key: str, label: str, default: str) -> str:
    """Ask for a folder until it exists: one that does not is created if the user agrees, else asked again."""
    while True:
        answer = str(await interaction.prompt(f"{label} ({key})", default=default)).strip() or default
        path = os.path.abspath(os.path.expanduser(answer))
        if os.path.isdir(path):
            return path
        if os.path.exists(path):
            click.secho(f"{path} is not a folder.", fg="red")
            continue
        if await interaction.confirm(f"{path} does not exist. Create it?", default=True):
            try:
                os.makedirs(path)
            except OSError as e:
                click.secho(f"Could not create {path}: {e}", fg="red")
                continue
            return path
        click.secho(f"salmon needs {key} to start: give a folder that exists, or agree to create it.", fg="yellow")


async def ask_trackers() -> list[TrackerAnswer]:
    """Ask which trackers, then each one's session cookie (required) and API key (optional)."""
    while True:
        answer = str(await interaction.prompt(f"Trackers you upload to ({', '.join(TRACKERS)}; separate with commas)"))
        codes = list(dict.fromkeys(word.upper() for word in re.split(r"[\s,]+", answer) if word))
        unknown = [code for code in codes if code not in TRACKERS]
        if unknown:
            click.secho(f"Not a tracker salmon knows: {', '.join(unknown)}.", fg="red")
        elif not codes:
            click.secho("salmon needs at least one tracker.", fg="red")
        else:
            break

    trackers: list[TrackerAnswer] = []
    for code in codes:
        click.echo(f"{code}: the value of the 'session' cookie your browser has for {SITES[code]}, while logged in.")
        session = ""
        while not session:
            session = str(await interaction.prompt(f"{code} session cookie")).strip()
        api_key = None
        if code in TAKES_API_KEY:
            key = await interaction.prompt(f"{code} API key (Enter to skip)", default="", show_default=False)
            api_key = str(key).strip() or None
        trackers.append(TrackerAnswer(code=code, session=session, api_key=api_key))
    return trackers


async def ask(template_text: str) -> Answers:
    """Everything a new user must give, defaults from the short config made absolute against the current folder."""
    defaults = msgspec.toml.decode(template_text)["directory"]
    folders = {key: await ask_folder(key, label, os.path.abspath(defaults[key])) for key, label in FOLDERS}
    return Answers(**folders, trackers=await ask_trackers())


async def check_connection(cfg: Cfg, codes: list[str]) -> None:
    """What ``salmon checkconf -t`` runs for each tracker, with the config just written."""
    # salmon is still importing: the tracker clients read the config from salmon.cfg once it is set.
    import salmon

    salmon.cfg = cfg
    from salmon.trackers.connection import check_trackers

    await check_trackers(codes)


async def run(template: Path, dest: Path) -> int:
    """The guided first run in a terminal. Returns the exit code: 0 once a config that loads is written."""
    from salmon.config import _parse_config

    template_text = template.read_text(encoding="utf-8")
    click.secho("No config yet: a few questions to write one. Ctrl-C stops without writing anything.", fg="cyan")
    try:
        answers = await ask(template_text)
    except click.Abort:
        click.secho("\nStopped: no config was written.", fg="yellow")
        return 1

    try:
        write_new(dest, render(template_text, answers))
    except OSError as e:
        click.secho(f"Could not write {dest}: {e}", fg="red")
        return 1
    click.secho(f"Config written to {dest}.", fg="green")

    try:
        cfg = _parse_config(dest)
    except Exception as e:
        click.secho(f"It does not load: {e}", fg="red")
        click.secho("Fix it, then run salmon again.", fg="yellow")
        return 1

    codes = [answer.code for answer in answers.trackers]
    try:
        if await interaction.confirm(f"Check the connection to {', '.join(codes)} now?", default=True):
            await check_connection(cfg, codes)
    except click.Abort:
        click.echo()

    reference = template.parent / "config.reference.toml"
    click.secho(f"\nYour config is {dest}.", fg="green")
    click.echo(f"Every other setting is documented in {reference}.")
    click.secho("Run salmon again.", fg="green")
    return 0
