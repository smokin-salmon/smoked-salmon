"""What a command can ask the user, and who answers.

Every question salmon asks goes through this module: `prompt`, `confirm`, `edit` and `show_spectrals`, plus
`assume_defaults`, which tells a step whether this run answers its questions with their defaults (`-yyy`).
The implementation that answers is held in a context variable, so a long-lived process can give each job its
own. The default is the terminal one, which calls asyncclick as the code did before this module existed.

`-yyy` is a context value too, set for the length of `assuming_defaults()`: it never outlives the run that set it,
and nothing writes the config at runtime.

A test (tests/test_interaction_calls.py) fails on any direct call to asyncclick's `prompt`, `confirm` or `edit`,
or to `input()`, outside the terminal implementation below and `setup_config`.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

import asyncclick as click

from salmon import cfg
from salmon.common import prompt_async


class Interaction(Protocol):
    """What answers a command's questions."""

    async def prompt(
        self,
        text: str,
        *,
        default: Any = None,
        type: Any = None,
        value_proc: Callable[[str], Any] | None = None,
        show_default: bool = True,
    ) -> Any:
        """Ask for a line of text, and return it converted by `type` or `value_proc`."""
        ...

    async def confirm(
        self,
        text: str,
        *,
        default: bool | None = False,
        abort: bool = False,
    ) -> bool:
        """Ask a yes or no question. With `abort`, a no raises `click.Abort`."""
        ...

    async def edit(
        self,
        text: str,
        *,
        editor: str | None = None,
        extension: str = ".txt",
    ) -> str | None:
        """Let the user edit `text`, and return the edited text (None if they left without saving)."""
        ...

    async def show_spectrals(self, spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
        """Show the spectral images in `spectrals_path` and return once the user has looked at them."""
        ...

    async def assume_defaults(self) -> bool:
        """Whether this run answers a question with its default instead of asking (`-yyy`)."""
        ...


# Whether this run was started with `-yyy`.
_assumed: ContextVar[bool] = ContextVar("assume_defaults", default=False)


@contextmanager
def assuming_defaults(on: bool = True) -> Iterator[None]:
    """Run the block, and every task it starts, as a run that answers its questions with their defaults (if `on`)."""
    if not on:
        yield
        return
    token = _assumed.set(True)
    try:
        yield
    finally:
        _assumed.reset(token)


def run_assumes_defaults() -> bool:
    """Whether this run answers its questions with their defaults: inside `assuming_defaults()`, or `yes_all`."""
    return _assumed.get() or cfg.upload.yes_all


class TerminalInteraction:
    """Ask in the terminal, through asyncclick."""

    async def prompt(
        self,
        text: str,
        *,
        default: Any = None,
        type: Any = None,
        value_proc: Callable[[str], Any] | None = None,
        show_default: bool = True,
    ) -> Any:
        return await click.prompt(
            text,
            default=default,
            type=type,
            value_proc=value_proc,
            show_default=show_default,
        )

    async def confirm(
        self,
        text: str,
        *,
        default: bool | None = False,
        abort: bool = False,
    ) -> bool:
        return click.confirm(text, default=default, abort=abort)

    async def edit(
        self,
        text: str,
        *,
        editor: str | None = None,
        extension: str = ".txt",
    ) -> str | None:
        edited = click.edit(text, editor=editor, extension=extension)
        # A str goes in, so a str (or None) comes back.
        return edited if isinstance(edited, str) else None

    async def show_spectrals(self, spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
        # Imported here: the spectrals module asks its questions through this one.
        from salmon.uploader.spectrals import view_spectrals

        await view_spectrals(spectrals_path, all_spectral_ids, wait_for_enter=self._wait_for_enter)

    async def assume_defaults(self) -> bool:
        return run_assumes_defaults()

    @staticmethod
    async def _wait_for_enter(text: str) -> None:
        await prompt_async(text, end=" ", flush=True)


_terminal = TerminalInteraction()
_current: ContextVar[Interaction] = ContextVar("interaction", default=_terminal)


@contextmanager
def using(interaction: Interaction) -> Iterator[None]:
    """Let `interaction` answer the block's questions, and those of every task it starts."""
    token = _current.set(interaction)
    try:
        yield
    finally:
        _current.reset(token)


def current() -> Interaction:
    """The implementation that answers in this context."""
    return _current.get()


async def prompt(
    text: str,
    *,
    default: Any = None,
    type: Any = None,
    value_proc: Callable[[str], Any] | None = None,
    show_default: bool = True,
) -> Any:
    """Ask for a line of text. See `Interaction.prompt`."""
    return await current().prompt(text, default=default, type=type, value_proc=value_proc, show_default=show_default)


async def confirm(text: str, *, default: bool | None = False, abort: bool = False) -> bool:
    """Ask a yes or no question. See `Interaction.confirm`."""
    return await current().confirm(text, default=default, abort=abort)


async def edit(text: str, *, editor: str | None = None, extension: str = ".txt") -> str | None:
    """Let the user edit a text. See `Interaction.edit`."""
    return await current().edit(text, editor=editor, extension=extension)


async def show_spectrals(spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
    """Show the spectral images and wait until the user has looked at them. See `Interaction.show_spectrals`."""
    await current().show_spectrals(spectrals_path, all_spectral_ids)


async def assume_defaults() -> bool:
    """Whether this run answers a question with its default instead of asking. See `Interaction.assume_defaults`."""
    return await current().assume_defaults()
