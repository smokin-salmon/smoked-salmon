import asyncio
import json
import re
import time
from copy import deepcopy
from typing import Any

import aiohttp
import asyncclick as click
import msgspec

from salmon import cfg

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
POLL_INTERVAL_SECONDS = 5
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
BOLD_MARKDOWN_PATTERN = re.compile(r"\*\*(.+?)\*\*")

SYSTEM_PROMPT = """You research one exact music release for Smoked Salmon.

Your job is to find the most accurate metadata for this release online and return a conservative structured patch.
The selected source URL is a reference, not an exclusive authority. You may use any online sources that help identify
and verify the exact release.

Match the same release by artist, release title, release type, and track count/order whenever possible.
Only change a field when the online evidence supports the same release. If evidence is weak, conflicting, or indirect,
leave the field unchanged.

Do not change artists, release type, format, encoding, source, scene flags, comments, cover art,
or track artist credits.
Only patch: title, group_year, year, edition_title, label, catno, upc, genres, urls, and track titles.

Return a structured metadata patch only. Never return freeform prose outside the schema.
Never rewrite files directly.
Every patch field must be present in the output.
Use null for unchanged scalar fields.
Use [] for unchanged list fields like genres and urls.
Keep urls relevant and deduplicated.
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
                "required": [
                    "title",
                    "group_year",
                    "year",
                    "edition_title",
                    "label",
                    "catno",
                    "upc",
                    "genres",
                    "urls",
                ],
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
        "Research this release online and suggest corrections only where the evidence is strong.",
        "Use the selected source URL as a reference,",
        "but you may use any online sources needed to identify the exact release.",
        "",
        "Release reference JSON (do not edit these fields directly):",
        json.dumps(_build_release_reference(metadata, source_url), indent=2, ensure_ascii=False),
        "",
        "Current editable metadata JSON:",
        json.dumps(_build_editable_metadata_snapshot(metadata), indent=2, ensure_ascii=False),
        "",
        "Editable metadata from local tags before Salmon combined sources:",
        json.dumps(_build_editable_metadata_snapshot(tag_baseline), indent=2, ensure_ascii=False),
    ]
    if user_instruction:
        prompt.extend(["", "Additional user instruction for this pass:", user_instruction])
    return "\n".join(prompt)


def _build_release_reference(metadata: dict[str, Any], source_url: str | None) -> dict[str, Any]:
    artists = [
        {"name": artist_name, "role": artist_role}
        for artist_name, artist_role in metadata.get("artists", [])
        if artist_name
    ]
    tracks = metadata.get("tracks", {})
    track_count = sum(len(disc_tracks) for disc_tracks in tracks.values())

    return {
        "artists": artists,
        "release_type": metadata.get("rls_type"),
        "source": metadata.get("source"),
        "format": metadata.get("format"),
        "encoding": metadata.get("encoding"),
        "encoding_vbr": metadata.get("encoding_vbr"),
        "scene": metadata.get("scene"),
        "track_count": track_count,
        "selected_source_url": source_url,
    }


def _build_editable_metadata_snapshot(metadata: dict[str, Any]) -> dict[str, Any]:
    snapshot = {
        field: deepcopy(metadata.get(field))
        for field in TOP_LEVEL_PATCH_FIELDS
    }
    snapshot["track_titles"] = [
        {
            "disc_number": disc_number,
            "track_number": track_number,
            "title": track.get("title"),
        }
        for disc_number, disc_tracks in metadata.get("tracks", {}).items()
        for track_number, track in disc_tracks.items()
    ]
    return snapshot


def _build_request_payload(
    metadata: dict[str, Any],
    tag_baseline: dict[str, Any],
    source_url: str | None,
    user_instruction: str | None = None,
    previous_response_id: str | None = None,
    use_background: bool | None = None,
) -> dict[str, Any]:
    ai_cfg = cfg.upload.ai_review
    if use_background is None:
        use_background = ai_cfg.background
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
        "reasoning": {"effort": ai_cfg.reasoning_effort, "summary": "auto"},
    }
    if ai_cfg.use_web_search:
        payload["tools"] = [{"type": "web_search"}]
    if use_background:
        payload["background"] = True
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    return payload


def _extract_response_error(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message", error))
    return str(payload)


def _format_ai_progress(text: str) -> str:
    return " ".join(text.split())


def _style_ai_progress(text: str) -> str:
    formatted = _format_ai_progress(text)
    if not formatted:
        return ""

    parts: list[str] = []
    last_index = 0
    for match in BOLD_MARKDOWN_PATTERN.finditer(formatted):
        start, end = match.span()
        if start > last_index:
            parts.append(click.style(formatted[last_index:start], fg="bright_black"))
        parts.append(click.style(match.group(1), fg="bright_black", bold=True))
        last_index = end

    if last_index < len(formatted):
        parts.append(click.style(formatted[last_index:], fg="bright_black"))

    return "".join(parts)


def _emit_ai_progress(text: str) -> None:
    styled = _style_ai_progress(text)
    if styled:
        click.echo(styled, color=True)


async def _fetch_response(
    session: aiohttp.ClientSession, headers: dict[str, str], response_id: str
) -> dict[str, Any]:
    async with session.get(f"{OPENAI_RESPONSES_URL}/{response_id}", headers=headers) as resp:
        try:
            payload = await resp.json()
        except aiohttp.ContentTypeError:
            raw = await resp.text()
            raise RuntimeError(
                f"OpenAI API returned a non-JSON polling response (status {resp.status}): {raw[:200]}"
            ) from None
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
    seen_progress_events: set[tuple[str, str, str]] = set()
    last_reasoning_summary: str | None = None
    while time.monotonic() < deadline:
        status = current_payload.get("status")
        if status in FINAL_RESPONSE_STATUSES:
            return current_payload

        progress_lines, last_reasoning_summary = _extract_progress_updates(
            current_payload, seen_progress_events, last_reasoning_summary
        )
        for line in progress_lines:
            _emit_ai_progress(line)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        current_payload = await _fetch_response(session, headers, response_id)

    raise RuntimeError(
        f"Timed out waiting for AI metadata review to finish after {timeout_seconds}s. "
        "Increase upload.ai_review.timeout_seconds or keep background mode enabled."
    )


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


def _decode_sse_event(event_name: str | None, data_lines: list[str]) -> dict[str, Any] | None:
    if not data_lines:
        return None

    raw_data = "\n".join(data_lines).strip()
    if not raw_data or raw_data == "[DONE]":
        return None

    try:
        payload = json.loads(raw_data)
    except json.JSONDecodeError:
        payload = {"raw_data": raw_data}

    if not isinstance(payload, dict):
        payload = {"data": payload}

    if event_name and "type" not in payload:
        payload["type"] = event_name
    return payload


def _extract_response_from_stream_event(event_payload: dict[str, Any]) -> dict[str, Any] | None:
    response = event_payload.get("response")
    if isinstance(response, dict):
        return response

    if "id" in event_payload and "status" in event_payload:
        return event_payload
    return None


def _extract_reasoning_summary(payload: dict[str, Any]) -> str | None:
    summaries: list[str] = []
    for item in payload.get("output", []):
        if item.get("type") != "reasoning":
            continue
        for summary_item in item.get("summary", []):
            text = summary_item.get("text")
            if isinstance(text, str):
                cleaned = text.strip()
                if cleaned:
                    summaries.append(cleaned)
    if not summaries:
        return None
    return "\n\n".join(summaries)


def _describe_web_search_action(item: dict[str, Any]) -> str:
    action = item.get("action")
    if not isinstance(action, dict):
        return ""

    parts: list[str] = []
    action_type = action.get("type")
    if isinstance(action_type, str) and action_type.strip():
        parts.append(action_type.replace("_", " "))

    query = action.get("query")
    if isinstance(query, str) and query.strip():
        parts.append(query.strip())

    url = action.get("url")
    if isinstance(url, str) and url.strip():
        parts.append(url.strip())

    return " | ".join(parts)


def _extract_progress_updates(
    payload: dict[str, Any],
    seen_progress_events: set[tuple[str, str, str]],
    last_reasoning_summary: str | None,
) -> tuple[list[str], str | None]:
    lines: list[str] = []

    reasoning_summary = _extract_reasoning_summary(payload)
    if reasoning_summary and reasoning_summary != last_reasoning_summary:
        lines.append(f"reasoning: {reasoning_summary}")
        last_reasoning_summary = reasoning_summary

    for index, item in enumerate(payload.get("output", [])):
        if item.get("type") != "web_search_call":
            continue

        item_id = str(item.get("id") or f"web_search_call_{index}")
        status = str(item.get("status") or "unknown")
        action_detail = _describe_web_search_action(item)
        if status != "completed" or not action_detail:
            continue
        signature = (item_id, status, action_detail)
        if signature in seen_progress_events:
            continue

        seen_progress_events.add(signature)
        line = f"web_search: {status}"
        if action_detail:
            line = f"{line} | {action_detail}"
        lines.append(line)

    return lines, last_reasoning_summary


def _extract_stream_progress_updates(
    event_payload: dict[str, Any],
    seen_progress_events: set[tuple[str, str, str]],
    last_reasoning_summary: str | None,
) -> tuple[list[str], str | None]:
    lines: list[str] = []
    event_type = str(event_payload.get("type") or "")

    item = event_payload.get("item")
    if isinstance(item, dict):
        item_lines, last_reasoning_summary = _extract_progress_updates(
            {"output": [item]}, seen_progress_events, last_reasoning_summary
        )
        lines.extend(item_lines)

    if event_type.endswith("reasoning_summary_text.done"):
        text = event_payload.get("text")
        if isinstance(text, str):
            cleaned = text.strip()
            if cleaned and cleaned != last_reasoning_summary:
                lines.append(f"reasoning: {cleaned}")
                last_reasoning_summary = cleaned

    return lines, last_reasoning_summary


async def _stream_response(
    resp: aiohttp.ClientResponse,
    session: aiohttp.ClientSession,
    headers: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    response_id: str | None = None
    current_response: dict[str, Any] | None = None
    seen_progress_events: set[tuple[str, str, str]] = set()
    last_reasoning_summary: str | None = None

    event_name: str | None = None
    data_lines: list[str] = []

    async def flush_event() -> dict[str, Any] | None:
        nonlocal event_name, data_lines, response_id, current_response, last_reasoning_summary

        event_payload = _decode_sse_event(event_name, data_lines)
        event_name = None
        data_lines = []
        if not event_payload:
            return None

        response = _extract_response_from_stream_event(event_payload)
        if response:
            current_response = response
            response_id = str(response.get("id") or response_id or "")
            status = response.get("status")

            progress_lines, last_reasoning_summary = _extract_progress_updates(
                response, seen_progress_events, last_reasoning_summary
            )
            for line in progress_lines:
                _emit_ai_progress(line)

            if status in FINAL_RESPONSE_STATUSES:
                return response

        progress_lines, last_reasoning_summary = _extract_stream_progress_updates(
            event_payload, seen_progress_events, last_reasoning_summary
        )
        for line in progress_lines:
            _emit_ai_progress(line)

        return None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        try:
            raw_line = await asyncio.wait_for(
                resp.content.readline(), timeout=min(POLL_INTERVAL_SECONDS, remaining)
            )
        except TimeoutError:
            continue

        if not raw_line:
            break

        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            final_response = await flush_event()
            if final_response:
                return final_response
            continue

        if line.startswith(":"):
            continue

        if line.startswith("event:"):
            event_name = line.partition(":")[2].strip()
            continue

        if line.startswith("data:"):
            data_lines.append(line.partition(":")[2].lstrip())

    final_response = await flush_event()
    if final_response:
        return final_response

    if response_id:
        latest_payload = await _fetch_response(session, headers, response_id)
        return await _wait_for_response(session, headers, latest_payload, timeout_seconds)

    raise RuntimeError("AI metadata review stream ended before a response ID was received")


def _should_use_background() -> bool:
    ai_cfg = cfg.upload.ai_review
    return ai_cfg.background or ai_cfg.reasoning_effort in {"high", "xhigh"} or ai_cfg.use_web_search


async def _request_ai_review(
    metadata: dict[str, Any],
    tag_baseline: dict[str, Any],
    source_url: str | None,
    user_instruction: str | None,
    previous_response_id: str | None,
) -> tuple[dict[str, Any], str | None]:
    ai_cfg = cfg.upload.ai_review
    use_background = _should_use_background()
    headers = {
        "Authorization": f"Bearer {ai_cfg.api_key}",
        "Content-Type": "application/json",
        "User-Agent": cfg.upload.user_agent,
    }
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=None)
    payload = _build_request_payload(
        metadata,
        tag_baseline,
        source_url,
        user_instruction,
        previous_response_id,
        use_background=use_background,
    )

    async with aiohttp.ClientSession(timeout=timeout) as session:
        request_payload = payload
        if use_background:
            request_payload = {**payload, "stream": True}
        try:
            async with asyncio.timeout(ai_cfg.timeout_seconds):
                async with session.post(OPENAI_RESPONSES_URL, headers=headers, json=request_payload) as resp:
                    if resp.status >= 400:
                        try:
                            error_payload = await resp.json()
                        except aiohttp.ContentTypeError:
                            raw = await resp.text()
                            raise RuntimeError(
                                f"OpenAI API returned a non-JSON response (status {resp.status}): {raw[:200]}"
                            ) from None
                        raise RuntimeError(f"OpenAI API error: {_extract_response_error(error_payload)}")

                    if use_background and resp.content_type == "text/event-stream":
                        response_payload = await _stream_response(
                            resp, session, headers, ai_cfg.timeout_seconds
                        )
                    else:
                        try:
                            response_payload = await resp.json()
                        except aiohttp.ContentTypeError:
                            raw = await resp.text()
                            raise RuntimeError(
                                f"OpenAI API returned a non-JSON response (status {resp.status}): {raw[:200]}"
                            ) from None
        except TimeoutError as exc:
            raise RuntimeError(
                f"Timed out after {ai_cfg.timeout_seconds}s while submitting the AI metadata review request. "
                "Background mode is recommended for long-running reviews."
            ) from exc

        if response_payload.get("status") in {"queued", "in_progress"}:
            response_payload = await _wait_for_response(
                session, headers, response_payload, ai_cfg.timeout_seconds
            )
        status = response_payload.get("status")
        if status in {"failed", "cancelled", "incomplete"}:
            raise RuntimeError(f"AI metadata review did not complete successfully (status: {status})")

    if response_payload.get("error"):
        raise RuntimeError(f"OpenAI API error: {_extract_response_error(response_payload)}")

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
            normalized = _normalize_list(patch[field])
            if normalized:
                updated[field] = normalized
        else:
            if patch[field] is not None:
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
            if field in {"genres", "urls"}:
                normalized = _normalize_list(patch[field])
                if not normalized:
                    continue
                after = normalized
            else:
                after = patch[field]
                if after is None:
                    continue
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
    manual_review,
) -> dict[str, Any]:
    current_metadata = await manual_review(metadata, validator)

    ai_cfg = cfg.upload.ai_review
    if not ai_cfg.enabled:
        return current_metadata

    should_run = cfg.upload.yes_all or click.confirm(
        click.style("\nRun AI metadata review?", fg="magenta"),
        default=False,
    )
    if not should_run:
        return current_metadata

    current_metadata = deepcopy(current_metadata)
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
        except asyncio.CancelledError:
            click.secho("\nAI metadata review aborted by user.", fg="yellow")
            raise click.Abort() from None
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

        if not diff_lines:
            return current_metadata

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

            if choice == "k":
                return current_metadata

            if choice == "e":
                return await manual_review(current_metadata, validator)

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
                return await manual_review(updated_metadata, validator)

            click.secho(f"{choice} is not a valid AI review option.", fg="red")
