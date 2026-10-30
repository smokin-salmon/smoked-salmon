"""What a command can ask the user, and who answers.

Every question salmon asks goes through this module: `prompt`, `confirm`, `edit` and `show_spectrals`, plus
`assume_defaults`, which tells a step whether this run answers its questions with their defaults (`-yyy`).
The implementation that answers is held in a context variable, so a long-lived process can give each job its
own. The default is the terminal one, which calls asyncclick as the code did before this module existed.

`-yyy` is a context value too, set for the length of `assuming_defaults()`: it never outlives the run that set it,
and nothing writes the config at runtime.

A `prompt` may describe the answers it already accepts (`options`, an `Options`), and a `confirm` may name its two
buttons (`labels`), as ADR 0006 decides. The terminal ignores both: asyncclick gets exactly the call's other
arguments. salmon web draws the description, and turns what the browser picked into the line a terminal user would
have typed, which the call site's own parser then reads.

A test (tests/test_interaction_calls.py) fails on any direct call to asyncclick's `prompt`, `confirm` or `edit`,
or to `input()`, outside the terminal implementation below.

The guided first run (config/first_run.py) asks through this module before a config exists, while `salmon` is
still importing: nothing here reads `salmon.cfg` or imports a module that does until a question needs it.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

import asyncclick as click
import msgspec

import salmon


class Card(msgspec.Struct, frozen=True):
    """What the web shows of an option on a card of its own."""

    # (label, value) pairs, shown in order.
    fields: tuple[tuple[str, str], ...] = ()
    # The option's page on the tracker or store. The web shows only an http:// or https:// one.
    link: str | None = None
    # "Not an exact group match", "DUPE RISK: ...".
    note: str | None = None


class Option(msgspec.Struct, frozen=True):
    """One answer a prompt accepts."""

    # The reply the CLI accepts for it: "y", "3", "*", "OPS".
    key: str
    label: str
    # The heading it is listed under: a store, "Requests on RED".
    group: str | None = None
    card: Card | None = None
    # Drawn apart from the others, and answers at once: New group, Abort, Delete music folder.
    action: bool = False
    # Several only: a shortcut sent on its own (All "*", None "0", Random third "+").
    alone: bool = False
    # The option an empty reply means at this call site.
    empty: bool = False
    # Action only: the web asks for a second click before sending it.
    danger: bool = False


class FreeText(msgspec.Struct, frozen=True):
    """A typed answer the prompt takes beside its options: a URL, and with `ids` a number above every numbered key."""

    placeholder: str
    ids: bool = False


class Mark(msgspec.Struct, frozen=True):
    """A prefix the reply puts before one of its picks or texts, as `*` marks the store the files came from."""

    prefix: str
    label: str


class Options(msgspec.Struct, frozen=True):
    """The answers a prompt accepts, for salmon web to draw (ADR 0006). The terminal ignores it."""

    # In display order, which is also the order picks are put in the line.
    items: tuple[Option, ...]
    # Any number of options may be picked.
    several: bool = False
    free_text: FreeText | None = None
    mark: Mark | None = None

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for option in self.items:
            if not option.key or any(c.isspace() for c in option.key):
                raise ValueError(f"The key {option.key!r} is empty or holds a space.")
            # The default is read against the keys ignoring case, so two keys must differ in more than case.
            if option.key.lower() in seen:
                raise ValueError(f"The key {option.key!r} appears twice.")
            seen.add(option.key.lower())
            if option.alone and not self.several:
                raise ValueError(f"{option.key!r} is alone, which only a question taking several answers has.")
            if option.danger and not option.action:
                raise ValueError(f"{option.key!r} is danger, which only an action is.")
            if self.mark is not None and self.mark.prefix and option.key.startswith(self.mark.prefix):
                raise ValueError(f"The key {option.key!r} starts with the mark's prefix.")
        if sum(option.empty for option in self.items) > 1:
            raise ValueError("More than one option is the empty reply.")
        if self.mark is not None:
            if not self.several:
                raise ValueError("A mark goes only on a question taking several answers.")
            if not self.mark.prefix or any(c.isspace() for c in self.mark.prefix):
                raise ValueError(f"The mark's prefix {self.mark.prefix!r} is empty or holds a space.")


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
        options: Options | None = None,
    ) -> Any:
        """Ask for a line of text, and return it converted by `type` or `value_proc`.

        `options` describes the replies the caller accepts, for an implementation that draws them; the reply is
        still a line the caller parses.
        """
        ...

    async def confirm(
        self,
        text: str,
        *,
        default: bool | None = False,
        abort: bool = False,
        labels: tuple[str, str] | None = None,
    ) -> bool:
        """Ask a yes or no question. With `abort`, a no raises `click.Abort`.

        `labels` names the yes and no buttons, for an implementation that draws them.
        """
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
    return _assumed.get() or salmon.cfg.upload.yes_all


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
        options: Options | None = None,
    ) -> Any:
        # The options are for the web: the terminal asks exactly as before.
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
        labels: tuple[str, str] | None = None,
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
        # Imported here: salmon.common reads the config when it is imported.
        from salmon.common import prompt_async

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
    options: Options | None = None,
) -> Any:
    """Ask for a line of text. See `Interaction.prompt`."""
    return await current().prompt(
        text, default=default, type=type, value_proc=value_proc, show_default=show_default, options=options
    )


async def confirm(
    text: str, *, default: bool | None = False, abort: bool = False, labels: tuple[str, str] | None = None
) -> bool:
    """Ask a yes or no question. See `Interaction.confirm`."""
    return await current().confirm(text, default=default, abort=abort, labels=labels)


async def edit(text: str, *, editor: str | None = None, extension: str = ".txt") -> str | None:
    """Let the user edit a text. See `Interaction.edit`."""
    return await current().edit(text, editor=editor, extension=extension)


async def show_spectrals(spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
    """Show the spectral images and wait until the user has looked at them. See `Interaction.show_spectrals`."""
    await current().show_spectrals(spectrals_path, all_spectral_ids)


async def assume_defaults() -> bool:
    """Whether this run answers a question with its default instead of asking. See `Interaction.assume_defaults`."""
    return await current().assume_defaults()
