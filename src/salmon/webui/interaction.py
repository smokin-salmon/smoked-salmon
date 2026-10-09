"""A salmon web job's questions, put to the browser (ADR 0004, section 2).

The job's implementation of ``salmon.interaction``: each ``prompt``, ``confirm`` and ``edit`` becomes a question the
browser answers, while the job waits in its own thread. Its text comes without the terminal's colour codes, as the
job's log does. An answer is checked as the terminal checks it: the ``type`` or ``value_proc`` conversion, yes or no,
the default for an empty answer; a wrong one is asked again with the error. ``show_spectrals`` serves the job's
spectral images to the browser and waits until the user is done with them. Ported from the fork's
``WebInteraction`` (styx-techno, chodeus), which patched asyncclick instead.
"""

import inspect
import os
from collections.abc import Callable
from typing import Any, Protocol

import asyncclick as click

from salmon import interaction


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
    ) -> Any:
        convert = value_proc if value_proc is not None else click.types.convert_type(type, default)
        choices = [str(choice) for choice in type.choices] if isinstance(type, click.Choice) else None
        error: str | None = None
        while True:
            answer = await self._asker.ask(
                {
                    "kind": "prompt",
                    "text": click.unstyle(text),
                    "default": _shown_default(default) if show_default else None,
                    "choices": choices,
                    "error": error,
                }
            )
            value: Any = _text(answer)
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

    async def confirm(self, text: str, *, default: bool | None = False, abort: bool = False) -> bool:
        error: str | None = None
        while True:
            answer = await self._asker.ask(
                {"kind": "confirm", "text": click.unstyle(text), "default": default, "error": error}
            )
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
