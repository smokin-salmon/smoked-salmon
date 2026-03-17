import asyncio
import json
import time
from copy import deepcopy
from typing import Any

import aiohttp
import asyncclick as click
import msgspec

from salmon import cfg

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
TOP_LEVEL_PATCH_FIELDS = (
    "title",
    "group_year",
    "year",
    "edition_title",
    "label",
    "catno",
    "upc",
    "genres",
    "urls",
)
FINAL_RESPONSE_STATUSES = {"completed", "failed", "cancelled", "incomplete"}

SYSTEM_PROMPT = """You verify music release metadata for Smoked Salmon.

Return a structured metadata patch only. Never return freeform prose outside the schema.
Never rewrite files directly.
Only suggest changes you can justify with evidence.
When web_search is available, you must use it before finalizing your answer.
Do not change artists, release type, format, encoding, source, scene flags,
comments, cover art, or track artist credits.
Only patch: title, group_year, year, edition_title, label, catno, upc, genres, urls, and track titles.
Omit patch keys that should stay unchanged. Use null only when you are confident the field should be cleared.
Keep urls relevant and deduplicated.
If you are unsure, leave the field unchanged.
"""


def _ai_review_schema() -> dict[str, Any]:
    nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "patch", "track_title_changes", "citations"],
        "properties": {
            "summary": {"type": "string"},
            "patch": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": nullable_string,
                    "group_year": nullable_string,
                    "year": nullable_string,
                    "edition_title": nullable_string,
                    "label": nullable_string,
                    "catno": nullable_string,
                    "upc": nullable_string,
                    "genres": {"type": "array", "items": {"type": "string"}},
                    "urls": {"type": "array", "items": {"type": "string"}},
                },
            },
            "track_title_changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["disc_number", "track_number", "title"],
                    "properties": {
                        "disc_number": {"type": "string", "minLength": 1},
                        "track_number": {"type": "string", "minLength": 1},
                        "title": {"type": "string", "minLength": 1},
                    },
                },
            },
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "url", "supports"],
                    "properties": {
                        "title": {"type": "string", "minLength": 1},
                        "url": {"type": "string", "minLength": 1},
                        "supports": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
        },
    }


def _format_prompt(
    metadata: dict[str, Any],
    tag_baseline: dict[str, Any],
    source_url: str | None,
    user_instruction: str | None,
) -> str:
    prompt = [
        "Review this release metadata and suggest corrections only where the evidence is strong.",
        "The current metadata is Salmon's combined metadata from scraped sources and local tags.",
        "The baseline metadata comes from the local file tags before the combine step.",
        f"Selected source URL: {source_url or '(none)'}",
        "",
        "Current metadata JSON:",
        json.dumps(metadata, indent=2, ensure_ascii=False),
        "",
        "Tag-derived baseline JSON:",
        json.dumps(tag_baseline, indent=2, ensure_ascii=False),
    ]
    if user_instruction:
        prompt.extend(["", "Additional user instruction for this pass:", user_instruction])
    return "\n".join(prompt)


def _build_request_payload(
    metadata: dict[str, Any],
    tag_baseline: dict[str, Any],
    source_url: str | None,
    user_instruction: str | None = None,
    previous_response_id: str | None = None,
) -> dict[str, Any]:
    ai_cfg = cfg.upload.ai_review
    payload: dict[str, Any] = {
        "model": ai_cfg.model,
        "store": True,
        "instructions": SYSTEM_PROMPT,
        "input": _format_prompt(metadata, tag_baseline, source_url, user_instruction),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "salmon_ai_metadata_review",
                "strict": True,
                "schema": _ai_review_schema(),
            }
        },
        "reasoning": {"effort": ai_cfg.reasoning_effort},
    }
    if ai_cfg.use_web_search:
        payload["tools"] = [{"type": "web_search"}]
    if ai_cfg.background:
        payload["background"] = True
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    return payload


def _extract_response_error(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message", error))
    return str(payload)


async def _fetch_response(
    session: aiohttp.ClientSession, headers: dict[str, str], response_id: str
) -> dict[str, Any]:
    async with session.get(f"{OPENAI_RESPONSES_URL}/{response_id}", headers=headers) as resp:
        payload = await resp.json()
        if resp.status >= 400:
            raise RuntimeError(f"OpenAI API error while polling: {_extract_response_error(payload)}")
        return payload


async def _wait_for_response(
    session: aiohttp.ClientSession,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    response_id = payload.get("id")
    status = payload.get("status")
    if not response_id or status in (None, "completed"):
        return payload

    deadline = time.monotonic() + timeout_seconds
    current_payload = payload
    while time.monotonic() < deadline:
        if current_payload.get("status") in FINAL_RESPONSE_STATUSES:
            return current_payload
        await asyncio.sleep(1)
        current_payload = await _fetch_response(session, headers, response_id)

    raise RuntimeError("Timed out waiting for AI metadata review to finish")


def _extract_output_text(payload: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts).strip()


async def _request_ai_review(
    metadata: dict[str, Any],
    tag_baseline: dict[str, Any],
    source_url: str | None,
    user_instruction: str | None,
    previous_response_id: str | None,
) -> tuple[dict[str, Any], str | None]:
    ai_cfg = cfg.upload.ai_review
    headers = {
        "Authorization": f"Bearer {ai_cfg.api_key}",
        "Content-Type": "application/json",
        "User-Agent": cfg.upload.user_agent,
    }
    timeout = aiohttp.ClientTimeout(total=ai_cfg.timeout_seconds)
    payload = _build_request_payload(metadata, tag_baseline, source_url, user_instruction, previous_response_id)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(OPENAI_RESPONSES_URL, headers=headers, json=payload) as resp:
            response_payload = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(f"OpenAI API error: {_extract_response_error(response_payload)}")

        response_payload = await _wait_for_response(session, headers, response_payload, ai_cfg.timeout_seconds)
        status = response_payload.get("status")
        if status in {"failed", "cancelled", "incomplete"}:
            raise RuntimeError(f"AI metadata review did not complete successfully (status: {status})")

    output_text = _extract_output_text(response_payload)
    if not output_text:
        raise RuntimeError("AI metadata review returned an empty response")

    try:
        review = msgspec.json.decode(output_text)
    except msgspec.DecodeError as exc:
        raise RuntimeError(f"AI metadata review returned invalid JSON: {exc}") from exc

    if not isinstance(review, dict):
        raise RuntimeError("AI metadata review returned an unexpected response shape")

    return review, response_payload.get("id")


def _normalize_list(values: list[str] | None) -> list[str]:
    if not values:
        return []

    deduped: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        trimmed = value.strip()
        if trimmed and trimmed not in deduped:
            deduped.append(trimmed)
    return deduped


def apply_ai_metadata_patch(metadata: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(metadata)
    patch = review.get("patch", {})
    if not isinstance(patch, dict):
        raise ValueError("AI patch payload is invalid")

    for field in TOP_LEVEL_PATCH_FIELDS:
        if field not in patch:
            continue
        if field in {"genres", "urls"}:
            updated[field] = _normalize_list(patch[field])
        else:
            updated[field] = patch[field]

    track_title_changes = review.get("track_title_changes", [])
    if not isinstance(track_title_changes, list):
        raise ValueError("AI track title patch payload is invalid")

    for change in track_title_changes:
        if not isinstance(change, dict):
            raise ValueError("AI track title change is invalid")

        disc_number = change["disc_number"]
        track_number = change["track_number"]
        try:
            updated["tracks"][disc_number][track_number]["title"] = change["title"].strip()
        except KeyError as exc:
            raise ValueError(
                f"AI suggested a missing track reference: disc {disc_number} track {track_number}"
            ) from exc

    return updated


def build_ai_review_diff(metadata: dict[str, Any], review: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    patch = review.get("patch", {})
    if isinstance(patch, dict):
        for field in TOP_LEVEL_PATCH_FIELDS:
            if field not in patch:
                continue
            before = metadata.get(field)
            after = _normalize_list(patch[field]) if field in {"genres", "urls"} else patch[field]
            if before != after:
                lines.append(f"{field}: {_format_diff_value(before)} -> {_format_diff_value(after)}")

    track_title_changes = review.get("track_title_changes", [])
    if isinstance(track_title_changes, list):
        for change in track_title_changes:
            if not isinstance(change, dict):
                continue
            disc_number = change.get("disc_number")
            track_number = change.get("track_number")
            if not disc_number or not track_number:
                continue
            before = metadata.get("tracks", {}).get(disc_number, {}).get(track_number, {}).get("title")
            after = change.get("title")
            if before != after:
                lines.append(
                    f"track {disc_number}-{track_number} title: "
                    f"{_format_diff_value(before)} -> {_format_diff_value(after)}"
                )
    return lines


def format_ai_review_citations(review: dict[str, Any]) -> list[str]:
    citations = review.get("citations", [])
    if not isinstance(citations, list) or not citations:
        return ["No citations were returned."]

    lines: list[str] = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        title = citation.get("title", "Untitled source")
        url = citation.get("url", "")
        supports = citation.get("supports", [])
        support_text = f" ({', '.join(supports)})" if supports else ""
        lines.append(f"- {title}{support_text}: {url}")
    return lines or ["No citations were returned."]


def _format_diff_value(value: Any) -> str:
    if value is None:
        return "(empty)"
    if isinstance(value, list):
        return ", ".join(value) if value else "(empty)"
    text = str(value).strip()
    return text if text else "(empty)"


async def review_metadata_with_ai(
    metadata: dict[str, Any],
    tag_baseline: dict[str, Any],
    source_url: str | None,
    validator,
) -> dict[str, Any]:
    ai_cfg = cfg.upload.ai_review
    if not ai_cfg.enabled:
        return metadata

    should_run = cfg.upload.yes_all or await click.confirm(
        click.style("\nRun AI metadata review?", fg="magenta"),
        default=False,
    )
    if not should_run:
        return metadata

    current_metadata = deepcopy(metadata)
    previous_response_id = None
    user_instruction = None

    while True:
        click.secho("\nRunning AI metadata review...", fg="cyan", bold=True)
        try:
            review, previous_response_id = await _request_ai_review(
                current_metadata,
                tag_baseline,
                source_url,
                user_instruction,
                previous_response_id,
            )
        except Exception as exc:
            click.secho(f"AI metadata review failed: {exc}", fg="red")
            return current_metadata

        diff_lines = build_ai_review_diff(current_metadata, review)
        summary = review.get("summary")
        if isinstance(summary, str) and summary.strip():
            click.secho(f"\nAI summary: {summary.strip()}", fg="yellow")

        if diff_lines:
            click.secho("\nAI suggested metadata updates:", fg="yellow", bold=True)
            for line in diff_lines:
                click.echo(f"> {line}")
        else:
            click.secho("\nAI did not suggest metadata changes.", fg="yellow")

        citations = format_ai_review_citations(review)
        if citations and citations[0] != "No citations were returned.":
            click.echo(f"> sources used: {', '.join(line.split(': ', 1)[0][2:] for line in citations)}")

        while True:
            choice = await click.prompt(
                click.style(
                    "\n[a]pply suggestions, [k]eep original, [e]dit manually, "
                    "[p]rompt model and rerun, [v]iew citations",
                    fg="magenta",
                ),
                type=click.STRING,
            )
            choice = choice.strip().lower()[:1]

            if choice == "v":
                click.secho("\nAI citations:", fg="yellow", bold=True)
                for line in citations:
                    click.echo(line)
                continue

            if choice == "p":
                user_instruction = await click.prompt(
                    click.style("What should the model change or prioritize?", fg="magenta"),
                    type=click.STRING,
                )
                break

            if choice in {"k", "e"}:
                return current_metadata

            if choice == "a":
                if not diff_lines:
                    click.secho("There are no AI changes to apply.", fg="yellow")
                    continue

                try:
                    updated_metadata = apply_ai_metadata_patch(current_metadata, review)
                    validator(updated_metadata)
                except Exception as exc:
                    click.secho(f"AI suggestions were rejected: {exc}", fg="red")
                    continue

                click.secho("Applied AI metadata suggestions.", fg="green")
                return updated_metadata

            click.secho(f"{choice} is not a valid AI review option.", fg="red")
