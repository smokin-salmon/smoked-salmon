"""Local Chinese display catalog. API values and generated upload descriptions stay unchanged."""

import json
import os
import re
from functools import lru_cache
from pathlib import Path


def enabled() -> bool:
    return os.environ.get("SALMON_LANGUAGE", "").lower() in {"zh", "zh-cn", "zh_cn"}


@lru_cache(maxsize=1)
def _catalog() -> dict[str, str]:
    return json.loads((Path(__file__).parent / "data" / "zh-CN.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _patterns() -> list[tuple[re.Pattern[str], str, list[str]]]:
    patterns = []
    for source, translated in _catalog().items():
        tokens = re.findall(r"ZXQ\d+QXZ", source)
        if not tokens or not re.search(r"[A-Za-z]{3}", re.sub(r"ZXQ\d+QXZ", "", source)):
            continue
        pieces = re.split(r"(ZXQ\d+QXZ)", source)
        pattern = "".join("(.*?)" if p in tokens else re.escape(p).replace(r"\ ", r"\s+") for p in pieces)
        patterns.append((re.compile(pattern, re.DOTALL), translated, tokens))
    patterns.sort(key=lambda item: len(re.sub(r"ZXQ\d+QXZ", "", item[0].pattern)), reverse=True)
    return patterns


@lru_cache(maxsize=4096)
def _translate(value: str) -> str:
    stripped = value.strip()
    key = re.sub(r"\s+", " ", stripped)
    found = _catalog().get(key)
    if found is None:
        for pattern, translated, tokens in _patterns():
            match = pattern.fullmatch(stripped)
            if match is None:
                continue
            substitutions = dict(zip(tokens, match.groups(), strict=True))
            found = re.sub(r"ZXQ\d+QXZ", lambda m, values=substitutions: values[m.group()], translated)
            break
    if found is None:
        return value
    leading = value[: len(value) - len(value.lstrip())]
    trailing = value[len(value.rstrip()) :]
    return leading + found + trailing


def text(value: str) -> str:
    return _translate(value) if enabled() else value


def localize_help(command) -> None:
    """Click reads command docstrings as help after decorators have registered them."""
    if not enabled():
        return
    import sys

    for name, module in tuple(sys.modules.items()):
        if name.startswith("asyncclick.") and callable(getattr(module, "_", None)):
            vars(module)["_"] = text
    if command.help:
        command.help = text(command.help)
    if command.short_help:
        command.short_help = text(command.short_help)
    for param in command.params:
        if getattr(param, "help", None):
            param.help = text(param.help)
    for child in getattr(command, "commands", {}).values():
        localize_help(child)
