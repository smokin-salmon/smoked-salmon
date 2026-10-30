"""A salmon web job's questions, put to the browser (ADR 0004, section 2).

The job's implementation of ``salmon.interaction``: each ``prompt``, ``confirm`` and ``edit`` becomes a question the
browser answers, while the job waits in its own thread. Its text comes without the terminal's colour codes, as the
job's log does. An answer is checked as the terminal checks it: the ``type`` or ``value_proc`` conversion, yes or no,
the default for an empty answer; a wrong one is asked again with the error. ``show_spectrals`` serves the job's
spectral images to the browser and waits until the user is done with them. Ported from the fork's
``WebInteraction`` (styx-techno, chodeus), which patched asyncclick instead.

A ``prompt`` with ``options`` (ADR 0006) is sent with its description and what its default preselects. The browser
answers with a ``Picked``, which is checked against the description and turned into the line a terminal user would
have typed: that line is what the call site's parser reads. Any other text is refused, except an empty answer,
which takes the default as before.
"""

import inspect
import os
import re
from collections.abc import Callable
from typing import Any, Protocol

import asyncclick as click
import msgspec

from salmon import interaction
from salmon.interaction import Options

# An http:// or https:// URL, in one piece: the only link a card shows, and the only text a typed question takes
# besides an ID.
_WEB_URL = re.compile(r"https?://[^\s\x00-\x1f\x7f/?#]+[^\s\x00-\x1f\x7f]*", re.IGNORECASE)
_NUMBER = re.compile(r"[0-9]+")


class Asker(Protocol):
    """Where a job's questions go: the job manager."""

    async def ask(self, question: dict[str, Any]) -> Any:
        """Put `question` to the browser and return the answer sent back."""
        ...

    def show_spectrals(self, folder: str, files: list[str]) -> None:
        """Serve these images of `folder` to the browser, as this job's spectrals."""
        ...


class NoAnswerError(click.Abort):
    """A question went unanswered for too long: the job stops."""


class Picked(msgspec.Struct, forbid_unknown_fields=True):
    """The browser's answer to a question with options: the keys picked, the texts typed, and the one marked."""

    picks: list[str] = []
    texts: list[str] = []
    mark: str | None = None


def _shown_options(options: Options) -> dict[str, Any]:
    """The description as plain JSON (the egress filter walks dicts and lists), without a card link that is not
    http:// or https://: store data is not trusted."""
    # Through JSON, which makes the tuples lists.
    shown = msgspec.json.decode(msgspec.json.encode(options))
    for item in shown["items"]:
        card = item["card"]
        if card is not None and card["link"] is not None and not _WEB_URL.fullmatch(card["link"]):
            card["link"] = None
    return shown


def _preselected(options: Options, default: Any) -> dict[str, Any]:
    """What the call site's default picks, read as the terminal reads a typed line (ADR 0006, section 2)."""
    chosen: list[tuple[str, str]] = []  # ("pick" or "text", value), in the default's order
    mark: str | None = None
    if default is not None:
        tokens = str(default).split()
        if not tokens:
            chosen += [("pick", option.key) for option in options.items if option.empty]
        keys = {option.key.lower(): option.key for option in options.items}
        for token in tokens:
            marked = options.mark is not None and token.startswith(options.mark.prefix)
            if options.mark is not None and marked:
                token = token[len(options.mark.prefix) :]
            if token.lower() in keys:
                value = ("pick", keys[token.lower()])
            elif token and options.free_text is not None:
                value = ("text", token)
            else:
                continue
            if value in chosen:
                continue
            chosen.append(value)
            if marked and mark is None:
                mark = value[1]
    alone = {option.key for option in options.items if option.alone}
    if not options.several:
        chosen = chosen[:1]
    elif first_alone := next((value for value in chosen if value[0] == "pick" and value[1] in alone), None):
        chosen = [first_alone]
    picks = [value for kind, value in chosen if kind == "pick"]
    texts = [value for kind, value in chosen if kind == "text"]
    return {"picks": picks, "texts": texts, "mark": mark if mark in picks + texts else None}


def _line(options: Options, answer: Any) -> tuple[str, str | None]:
    """The line a terminal user would have typed for `answer`, or the error to ask again with (ADR 0006, section 2)."""
    if not isinstance(answer, Picked):
        return "", "Pick from the options."
    by_key = {option.key: option for option in options.items}
    for i, pick in enumerate(answer.picks):
        if pick not in by_key:
            return "", f"{pick} is not one of the options."
        if pick in answer.picks[:i]:
            return "", f"{by_key[pick].label} is picked twice."
    for pick in answer.picks:
        option = by_key[pick]
        if (option.action or option.alone) and (len(answer.picks) > 1 or answer.texts or answer.mark is not None):
            return "", f"{option.label} goes on its own."
    if options.several:
        if not answer.picks and not answer.texts:
            return "", "Pick at least one option."
    elif len(answer.picks) + len(answer.texts) != 1:
        return "", "Pick one option." if options.free_text is None else "Pick one option, or type one answer."
    if answer.texts and options.free_text is None:
        return "", "This question takes no typed answer: pick from the options."
    for text in answer.texts:
        if error := _text_error(options, text):
            return "", error
    if answer.mark is not None:
        if options.mark is None:
            return "", "This question takes no mark."
        if [*answer.picks, *answer.texts].count(answer.mark) != 1:
            return "", f"Mark one of the answers sent as {options.mark.label}."
    tokens = [option.key for option in options.items if option.key in answer.picks] + answer.texts
    if options.mark is not None and answer.mark is not None:
        tokens = [options.mark.prefix + token if token == answer.mark else token for token in tokens]
    return " ".join(tokens), None


def _text_error(options: Options, text: str) -> str | None:
    """Why `text` cannot be sent as a typed answer, if it cannot: it must never read as one of the keys."""
    if _WEB_URL.fullmatch(text):
        return None
    if options.free_text is None or not options.free_text.ids:
        return f"{text} is not a URL: paste one that starts with http:// or https://."
    if not _NUMBER.fullmatch(text):
        return f"{text} is neither a URL nor an ID."
    numbered = [int(option.key) for option in options.items if _NUMBER.fullmatch(option.key)]
    number = int(text)
    if number > 0 and number > max(numbered, default=0):
        return None
    if number in numbered:
        return f"{number} is result {number} above: pick it there, or paste the URL."
    return f"An ID is a number above {max(numbered, default=0)}: paste the URL, or pick from the options."


def _text(answer: Any) -> str:
    """An answer as the line the user typed."""
    if answer is None:
        return ""
    return answer if isinstance(answer, str) else str(answer)


def _shown_default(default: Any) -> str | bool | None:
    if default is None or isinstance(default, bool | str):
        return default
    return str(default)


class WebInteraction:
    """Asks a job's questions in the browser."""

    def __init__(self, asker: Asker) -> None:
        self._asker = asker

    async def prompt(
        self,
        text: str,
        *,
        default: Any = None,
        type: Any = None,
        value_proc: Callable[[str], Any] | None = None,
        show_default: bool = True,
        options: Options | None = None,
    ) -> Any:
        convert = value_proc if value_proc is not None else click.types.convert_type(type, default)
        choices = [str(choice) for choice in type.choices] if isinstance(type, click.Choice) else None
        error: str | None = None
        while True:
            question: dict[str, Any] = {
                "kind": "prompt",
                "text": click.unstyle(text),
                "default": _shown_default(default) if show_default else None,
                "choices": choices,
                "error": error,
            }
            if options is not None:
                question["options"] = _shown_options(options)
                question["preselected"] = _preselected(options, default)
            answer = await self._asker.ask(question)
            value: Any
            refused: str | None = None
            if options is not None and (answer is None or answer == ""):
                # An empty reply takes the call site's own default, as Enter does in the terminal. The page never
                # sends one (it sends picks), but a client of the API may; no text of its own reaches the parser.
                value = ""
                if default is None:
                    refused = "Pick from the options."
            elif options is not None:
                value, refused = _line(options, answer)
            elif isinstance(answer, Picked):
                value, refused = "", "Type the answer."
            else:
                value = _text(answer)
            if refused is not None:
                error = f"Error: {refused}"
                click.echo(error)
                continue
            if not value:
                if default is None:
                    # The terminal asks again, without a word.
                    continue
                value = default
            try:
                result = convert(value)
                if inspect.isawaitable(result):
                    result = await result
            except click.UsageError as e:
                error = f"Error: {e.message}"
                click.echo(error)
                continue
            return result

    async def confirm(
        self,
        text: str,
        *,
        default: bool | None = False,
        abort: bool = False,
        labels: tuple[str, str] | None = None,
    ) -> bool:
        error: str | None = None
        while True:
            question: dict[str, Any] = {
                "kind": "confirm",
                "text": click.unstyle(text),
                "default": default,
                "error": error,
            }
            if labels is not None:
                question["labels"] = list(labels)
            answer = await self._asker.ask(question)
            reply = _text(answer).strip().lower()
            if answer is True or reply in ("y", "yes"):
                value = True
            elif answer is False or reply in ("n", "no"):
                value = False
            elif default is not None and reply == "":
                value = default
            else:
                error = "Error: invalid input"
                click.echo(error)
                continue
            break
        if abort and not value:
            raise click.Abort()
        return value

    async def edit(self, text: str, *, editor: str | None = None, extension: str = ".txt") -> str | None:
        error: str | None = None
        while True:
            answer = await self._asker.ask(
                {
                    "kind": "edit",
                    "text": "Edit the text, then save it.",
                    "initial": text,
                    "extension": extension,
                    "error": error,
                }
            )
            # None: left unchanged, as when the user quits the editor without saving.
            if answer is None or isinstance(answer, str):
                return answer
            error = "Send the edited text, or nothing to leave it unchanged."

    async def show_spectrals(self, spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
        files = sorted(
            entry.name
            for entry in os.scandir(spectrals_path)
            if entry.name.lower().endswith(".png") and entry.is_file(follow_symlinks=False)
        )
        self._asker.show_spectrals(spectrals_path, files)
        await self._asker.ask(
            {
                "kind": "spectrals",
                "text": "Look at the spectrals, then go on.",
                "files": files,
                "tracks": {f"{spectral_id:02d}": name for spectral_id, name in all_spectral_ids.items()},
            }
        )

    async def assume_defaults(self) -> bool:
        return interaction.run_assumes_defaults()
