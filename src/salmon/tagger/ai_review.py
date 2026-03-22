import asyncio
import inspect
import json
import re
import time
from copy import deepcopy
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import asyncclick as click
import msgspec
import requests
from bs4 import BeautifulSoup

from salmon import cfg
from salmon.constants import ARTIST_IMPORTANCES
from salmon.errors import InvalidMetadataError

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
POLL_INTERVAL_SECONDS = 5
TOP_LEVEL_PATCH_FIELDS = (
    "artists",
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
NORMALIZED_METADATA_FIELDS = {"genres", "urls"}
ARTIST_ROLE_VALUES = list(ARTIST_IMPORTANCES)
ARTIST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "role"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "role": {"type": "string", "enum": ARTIST_ROLE_VALUES},
    },
}
FINAL_RESPONSE_STATUSES = {"completed", "failed", "cancelled", "incomplete"}
BOLD_MARKDOWN_PATTERN = re.compile(r"\*\*(.+?)\*\*")
WHITESPACE_PATTERN = re.compile(r"\s+")
NULLABLE_STRING_SCHEMA = {"anyOf": [{"type": "string"}, {"type": "null"}]}
LIST_OF_STRINGS_SCHEMA = {"type": "array", "items": {"type": "string"}}
LABEL_EVIDENCE_MARKERS = (
    "label",
    "record label",
    "imprint",
    "released by",
    "under exclusive license to",
    "licensed to",
)
METADATA_SCHEMA_PROPERTIES = {
    **{
        field: NULLABLE_STRING_SCHEMA
        for field in TOP_LEVEL_PATCH_FIELDS
        if field not in NORMALIZED_METADATA_FIELDS and field != "artists"
    },
    **{field: LIST_OF_STRINGS_SCHEMA for field in NORMALIZED_METADATA_FIELDS},
    "artists": {"type": "array", "items": ARTIST_SCHEMA},
}
CITATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "url", "supports"],
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "url": {"type": "string", "minLength": 1},
        "supports": {"type": "array", "items": {"type": "string", "minLength": 1}},
    },
}

SYSTEM_PROMPT = """You research one exact music release for Smoked Salmon.

Build the album-level metadata from scratch using online evidence.
The selected_source_url is your starting anchor for identifying the release, not a source you must obey.
The prompt may include local Salmon metadata snapshots; treat them as search hints, not authoritative evidence.

Focus only on these album-level fields:
artists, title, group_year, year, edition_title, label, catno, upc, genres, urls

Ignore track titles, track order, and track pages unless album-level identification is ambiguous.
For singles or small releases, you may inspect the release-page tracklist when needed to resolve
artist/title splits, mix-name placement, or label-vs-artist confusion on the anchor page.
Do not browse unrelated track pages unless album-level identification is otherwise impossible.
Artists means release-level artist entries only. Do not return per-track artist edits in this review.

If selected_source_url is provided, your first web action must be opening that exact URL.
Do not search before opening it.
Do not conclude the release is unverified, or clear supported metadata, until after inspecting selected_source_url.
If the anchor page already clearly identifies the release,
only do extra web searches for fields that are still missing or conflicting.
Prefer release-level pages over artist bios, reviews, videos, lyrics pages, playlists, marketplaces, and fan sites.
Search-result snippets and result titles are only leads. Do not use them as evidence for metadata
changes or citations until you open the page.
Use 4 to 6 web actions total.
Aim to finish in 4 when the anchor page is clear, but spend up to 6 when needed to resolve
missing or conflicting release-level fields.
Stop as soon as you have enough evidence for the album-level fields.

When normalizing metadata, follow RED's upload, tagging, capitalization, and editing standards:
- Prefer the actual release title, not packaging noise, scene-style naming, or store-specific fluff.
- Do not keep a catalog number in title unless release-level sources clearly show it is part of
  the title. If a title starts with a bracketed or prefixed catalog code, prefer moving that code
  to catno instead of leaving it in title.
- Use standard capitalization and title case for English titles and labels unless release-level
  evidence clearly shows intentional stylization.
- edition_title is only for edition-specific descriptors. Do not put the album title, store name,
  DSP name unless it is an exclusive edition, format, bitrate, bit depth, sample rate, or generic
  descriptors like Digital, Original, or First Pressing in edition_title. If the only extra info
  you have is WEB quality such as 24-bit/44.1 kHz, leave edition_title blank.
- Prefer the credited release label or imprint. Do not infer label from the artist name, store,
  seller, distributor, or parent company alone, and omit corporate suffixes when the release-level
  source clearly uses the shorter imprint name.
- Normalize label values to RED style instead of copying vendor rights text verbatim. When a
  release-level source shows a compound line such as "Label A - Rights Holder under exclusive
  license to Label B" or similar distributor/licensor wording, prefer the primary credited
  label or imprint entity and drop the rights/licensing/distribution tail unless the page clearly
  presents multiple entities as true co-labels. If a sub-label or imprint appears alongside a
  parent label, prefer only the sub-label or imprint.
- Only use a slash-separated multi-label value like `Label 1 / Label 2` when an opened
  release-level page clearly presents multiple legitimate labels or imprints as parallel release
  labels. Do not invent a slash-separated label from a compound rights/licensing string; if the
  evidence is a vendor line like `Label A - Rights Holder under exclusive license to Label B`,
  prefer `Label A` unless the page explicitly frames both entities as labels/imprints.
- Be aggressive about collapsing compound DSP rights strings down to the clean RED label. If one
  opened page shows a long vendor string and another opened page shows only one clean candidate
  label/imprint, prefer the single clean imprint instead of synthesizing a slash label. For
  example, treat `ITModels - Doli & Penn Under exclusive license to NMC United Entertainment Ltd.`
  as strong evidence for `ITModels`, not for `ITModels / Doli & Penn`, unless the opened pages
  explicitly present both `ITModels` and `Doli & Penn` as peer labels or imprints.
- RED distinguishes "no label involved" from an unknown omitted label. If local metadata already
  has a plausible no-label marker such as "Self-Released" or "Not on Label", do not clear it
  merely because an official store page omits a named label; only replace it when release-level
  evidence supports a specific different label.
- If the only plausible normalized label would be exactly the same as a credited release artist
  name, prefer treating that as self-released/no-label rather than promoting the artist name to
  label. Only replace "Self-Released" or "Not on Label" with the artist name when an opened
  release-level page clearly presents that exact artist-name string as a distinct label or imprint
  for the release, not merely as the release artist in a rights/licensing line.
- Artists must follow RED's multiple-artists rules. List each credited release artist separately as
  a {name, role} entry. Use only supported roles: main, guest, remixer, composer, conductor,
  djcompiler, producer.
- For contributing artists who only appear on certain tracks, prefer guest instead of promoting them
  to main unless the release-level source clearly credits them as release artists. Do not drop a
  supported release-level guest artist merely because that artist only appears on some tracks.
- When individual artists are known on a compilation or split release, do not use "Various Artists"
  as a release artist entry. List the individual artists instead.
- Use catno only when a release-level source supports it. For WEB, if there is no definitive catno
  but there is an explicit UPC and it is the only supported release identifier, you may use that UPC
  as catno while also keeping it in upc.
- Be conservative with UPC and catno changes. Do not replace an existing UPC or catno from the
  chosen source or another exact release page with a weaker value from a search snippet, artist
  discography page, broad release list, or other non-exact index page. For identifiers, prefer
  exact release pages and exact release records only. MusicBrainz artist release-list pages are too
  weak to override a stronger exact-release DSP identifier.
- Only copy a UPC into catno when the same exact release page or exact release record explicitly
  supports that UPC for this release.
- Genres must behave like RED tags: keep only explicit source-supported genres, prefer specific
  genres over vague umbrellas, and never add artists, labels, formats, bitrates, release types,
  or store names as genres.
- Discogs and MusicBrainz are useful cross-checks, but they are not authoritative over a clearer
  official release page.

Never infer label from the artist name, a store name, or a seller.
Only set label when a release-level source explicitly names a label or imprint for this release.
Do not treat a bare ℗ or © rights line as label evidence unless the source also explicitly presents
that entity as the release label or imprint.
If no opened release-level page explicitly names a label or imprint, do not replace the current
label with a new guess.
Use group_year for the earliest supported release year of the release group.
Use year for the exact edition or source you identified.
If you cannot distinguish them, set both to the same supported year.
Prefer preserving a plausible non-empty local value when you find no
contradictory release-level evidence within the allowed web budget.
Normalize genres into human-readable title case when possible.
Only include genres or tags that are explicitly supported by the consulted sources.
Include only release-level URLs that directly identify this exact release.
If selected_source_url is provided, it must remain in urls.
You may add more release-level URLs, but do not remove existing URLs.
In your summary and citations, mention only pages you actually opened during this review.

Return only the schema. Never return freeform prose outside the schema. Never rewrite files directly.
"""


def _ai_review_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "metadata", "citations"],
        "properties": {
            "summary": {"type": "string"},
            "metadata": {
                "type": "object",
                "additionalProperties": False,
                "required": list(TOP_LEVEL_PATCH_FIELDS),
                "properties": METADATA_SCHEMA_PROPERTIES,
            },
            "citations": {"type": "array", "items": CITATION_SCHEMA},
        },
    }


def _format_prompt(
    metadata: dict[str, Any],
    tag_baseline: dict[str, Any],
    source_url: str | None,
    user_instruction: str | None,
) -> str:
    prompt = [
        "Research this release online and build the album-level metadata from scratch.",
        "The local metadata snapshots below are untrusted hints for identification and search targeting.",
        "Do not blindly patch from them, but do not discard plausible non-empty values without contradictory evidence.",
    ]
    if source_url:
        prompt.extend(
            [
                "",
                "Selected source URL:",
                source_url,
                "Open that exact page first before doing any search.",
            ]
        )
    prompt.extend(
        [
            "",
            "Release reference JSON:",
            json.dumps(_build_release_reference(metadata, source_url), indent=2, ensure_ascii=False),
            "",
            "Current editable album metadata JSON:",
            json.dumps(_build_album_metadata_snapshot(metadata), indent=2, ensure_ascii=False),
            "",
            "Tag-derived album metadata baseline JSON:",
            json.dumps(_build_album_metadata_snapshot(tag_baseline), indent=2, ensure_ascii=False),
        ]
    )
    if user_instruction:
        prompt.extend(["", "Additional user instruction for this pass:", user_instruction])
    return "\n".join(prompt)


def _build_release_reference(metadata: dict[str, Any], source_url: str | None) -> dict[str, Any]:
    artists = [
        {"name": artist_name, "role": artist_role}
        for artist_name, artist_role in metadata.get("artists", [])
        if artist_name
    ]

    return {
        "artists": artists,
        "release_title_hint": metadata.get("title"),
        "release_type_hint": metadata.get("rls_type"),
        "source": metadata.get("source"),
        "format": metadata.get("format"),
        "encoding": metadata.get("encoding"),
        "selected_source_url": source_url,
    }


def _build_album_metadata_snapshot(metadata: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for field in TOP_LEVEL_PATCH_FIELDS:
        snapshot[field] = _normalize_review_metadata_value(field, deepcopy(metadata.get(field)))
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
        "store": False,
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


def _emit_ai_progress_lines(lines: list[str]) -> None:
    for line in lines:
        _emit_ai_progress(line)


async def _load_json_response(resp: aiohttp.ClientResponse, *, non_json_error: str) -> dict[str, Any]:
    try:
        payload = await resp.json()
    except aiohttp.ContentTypeError:
        raw = await resp.text()
        raise RuntimeError(f"{non_json_error} (status {resp.status}): {raw[:200]}") from None
    return payload


async def _fetch_response(
    session: aiohttp.ClientSession, headers: dict[str, str], response_id: str
) -> dict[str, Any]:
    async with session.get(f"{OPENAI_RESPONSES_URL}/{response_id}", headers=headers) as resp:
        payload = await _load_json_response(
            resp,
            non_json_error="OpenAI API returned a non-JSON polling response",
        )
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

        last_reasoning_summary = _emit_progress_from_payload(
            current_payload,
            seen_progress_events,
            last_reasoning_summary,
        )

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
    return summaries[-1]


def _describe_web_search_action(item: dict[str, Any]) -> str:
    action = item.get("action")
    if not isinstance(action, dict):
        return ""

    action_type = action.get("type")
    if not isinstance(action_type, str):
        return ""
    action_type = action_type.strip()

    query = action.get("query")
    url = action.get("url")
    if action_type == "search" and isinstance(query, str) and query.strip():
        return f"search | {query.strip()}"
    if action_type == "open_page" and isinstance(url, str) and url.strip():
        return f"open page | {url.strip()}"
    return ""


def _normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_evidence_url(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None

    trimmed = url.strip()
    if not trimmed:
        return None

    parts = urlsplit(trimmed)
    if not parts.scheme or not parts.netloc:
        return None

    path = parts.path or ""
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def _choose_ai_anchor_url(metadata: dict[str, Any], source_url: str | None) -> str | None:
    normalized_source_url = _normalize_optional_text(source_url)
    if normalized_source_url:
        return normalized_source_url

    for url in _normalize_list(metadata.get("urls")):
        normalized_url = _normalize_optional_text(url)
        if normalized_url:
            return normalized_url
    return None


def _extract_opened_page_urls(payload: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    for item in payload.get("output", []):
        if item.get("type") != "web_search_call" or item.get("status") != "completed":
            continue
        action = item.get("action")
        if not isinstance(action, dict) or action.get("type") != "open_page":
            continue
        normalized = _normalize_evidence_url(action.get("url"))
        if normalized:
            urls.add(normalized)
    return urls


def _iter_field_citations(review: dict[str, Any], field: str):
    citations = review.get("citations", [])
    if not isinstance(citations, list):
        return

    normalized_field = field.strip().lower()
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        supports = citation.get("supports", [])
        if not isinstance(supports, list):
            continue
        for support in supports:
            normalized_support = WHITESPACE_PATTERN.sub(" ", str(support).strip()).lower()
            if not normalized_support:
                continue
            if (
                normalized_support == normalized_field
                or normalized_support.startswith(f"{normalized_field} ")
                or normalized_support.startswith(f"{normalized_field}(")
            ):
                yield citation
                break


def _page_explicitly_names_label(page_text: str, label: str) -> bool:
    normalized_text = WHITESPACE_PATTERN.sub(" ", page_text).casefold()
    normalized_label = WHITESPACE_PATTERN.sub(" ", label).strip().casefold()
    if not normalized_text or not normalized_label:
        return False

    escaped_label = re.escape(normalized_label)
    marker_pattern = "|".join(re.escape(marker.casefold()) for marker in LABEL_EVIDENCE_MARKERS)
    patterns = (
        rf"(?:{marker_pattern})[^.!?\n]{{0,120}}{escaped_label}",
        rf"{escaped_label}[^.!?\n]{{0,120}}(?:{marker_pattern})",
    )
    return any(re.search(pattern, normalized_text) for pattern in patterns)


@lru_cache(maxsize=64)
def _fetch_release_page_text(url: str) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": cfg.upload.user_agent},
        timeout=15,
    )
    response.raise_for_status()
    html = response.text
    soup = BeautifulSoup(html, "lxml")
    return f"{soup.get_text(' ', strip=True)} {html}"


def _url_explicitly_names_label(url: str, label: str) -> bool:
    try:
        page_text = _fetch_release_page_text(url)
    except (requests.RequestException, ValueError):
        return False
    return _page_explicitly_names_label(page_text, label)


def _guard_ai_artist_change(metadata: dict[str, Any], review: dict[str, Any]) -> str | None:
    review_metadata = review.get("metadata")
    if not isinstance(review_metadata, dict) or "artists" not in review_metadata:
        return None

    current_artists = _normalize_artist_entries(metadata.get("artists"))
    proposed_artists = _normalize_artist_entries(review_metadata.get("artists"))
    if not current_artists or not proposed_artists:
        return None

    proposed_names = {artist["name"].casefold() for artist in proposed_artists}
    missing_guests = [
        artist
        for artist in current_artists
        if artist["role"] == "guest" and artist["name"].casefold() not in proposed_names
    ]
    if not missing_guests:
        return None

    review_metadata["artists"] = [*proposed_artists, *missing_guests]
    guest_names = ", ".join(artist["name"] for artist in missing_guests)
    return f"Preserved existing guest artists that the AI tried to remove: {guest_names}."


def _guard_ai_url_change(
    metadata: dict[str, Any], review: dict[str, Any], source_url: str | None, opened_page_urls: set[str]
) -> str | None:
    review_metadata = review.get("metadata")
    if not isinstance(review_metadata, dict) or "urls" not in review_metadata:
        return None

    preserved_urls = _normalize_list([*_normalize_list(metadata.get("urls")), *([source_url] if source_url else [])])
    preserved_normalized = {
        normalized_url for url in preserved_urls if (normalized_url := _normalize_evidence_url(url))
    }
    proposed_urls = _resolve_review_metadata_value(
        "urls",
        metadata.get("urls"),
        review_metadata.get("urls"),
        source_url,
    )

    desired_urls = list(preserved_urls)
    dropped_additions: list[str] = []
    for url in proposed_urls:
        normalized_url = _normalize_evidence_url(url)
        if not normalized_url:
            continue
        if normalized_url in preserved_normalized or normalized_url in opened_page_urls:
            desired_urls.append(url)
            preserved_normalized.add(normalized_url)
        else:
            dropped_additions.append(url)

    review_metadata["urls"] = _normalize_list(desired_urls)
    if not dropped_additions:
        return None

    return (
        "Ignored AI URL additions that were not opened during review: "
        + ", ".join(dropped_additions)
        + "."
    )


def _guard_ai_label_change(
    metadata: dict[str, Any],
    review: dict[str, Any],
    source_url: str | None,
    opened_page_urls: set[str],
) -> str | None:
    review_metadata = review.get("metadata")
    if not isinstance(review_metadata, dict) or "label" not in review_metadata:
        return None

    current_label = _normalize_optional_text(metadata.get("label"))
    proposed_label = _normalize_optional_text(
        _resolve_review_metadata_value("label", metadata.get("label"), review_metadata.get("label"), source_url)
    )
    if current_label == proposed_label:
        return None

    review_metadata["label"] = metadata.get("label")
    if proposed_label is None:
        return "Ignored AI label removal because clearing label values requires manual review."

    opened_label_urls = {
        normalized_url
        for citation in _iter_field_citations(review, "label")
        if (normalized_url := _normalize_evidence_url(citation.get("url"))) in opened_page_urls
    }
    if not opened_label_urls:
        return (
            f'Ignored AI label change to "{proposed_label}" because no opened citation explicitly '
            "supported the label field."
        )

    if any(_url_explicitly_names_label(url, proposed_label) for url in opened_label_urls):
        review_metadata["label"] = proposed_label
        return None

    return (
        f'Ignored AI label change to "{proposed_label}" because none of the opened cited pages '
        "explicitly named it as a label or imprint."
    )


def _apply_ai_review_guardrails(
    metadata: dict[str, Any],
    review: dict[str, Any],
    source_url: str | None,
) -> tuple[dict[str, Any], list[str]]:
    sanitized_review = deepcopy(review)
    warnings = list(sanitized_review.get("_local_warnings", []))
    opened_page_urls = {
        normalized_url
        for url in sanitized_review.get("_opened_page_urls", [])
        if (normalized_url := _normalize_evidence_url(url))
    }

    artist_warning = _guard_ai_artist_change(metadata, sanitized_review)
    if artist_warning:
        warnings.append(artist_warning)

    url_warning = _guard_ai_url_change(metadata, sanitized_review, source_url, opened_page_urls)
    if url_warning:
        warnings.append(url_warning)

    label_warning = _guard_ai_label_change(metadata, sanitized_review, source_url, opened_page_urls)
    if label_warning:
        warnings.append(label_warning)

    sanitized_review["_opened_page_urls"] = sorted(opened_page_urls)
    sanitized_review["_local_warnings"] = warnings
    return sanitized_review, warnings


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


def _emit_progress_from_payload(
    payload: dict[str, Any], seen_progress_events: set[tuple[str, str, str]], last_reasoning_summary: str | None
) -> str | None:
    progress_lines, last_reasoning_summary = _extract_progress_updates(
        payload, seen_progress_events, last_reasoning_summary
    )
    _emit_ai_progress_lines(progress_lines)
    return last_reasoning_summary


async def _stream_response(
    resp: aiohttp.ClientResponse,
    session: aiohttp.ClientSession,
    headers: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    response_id: str | None = None
    seen_progress_events: set[tuple[str, str, str]] = set()
    last_reasoning_summary: str | None = None

    event_name: str | None = None
    data_lines: list[str] = []

    async def flush_event() -> dict[str, Any] | None:
        nonlocal event_name, data_lines, response_id, last_reasoning_summary

        event_payload = _decode_sse_event(event_name, data_lines)
        event_name = None
        data_lines = []
        if not event_payload:
            return None

        response = event_payload.get("response")
        if not isinstance(response, dict) and "id" in event_payload and "status" in event_payload:
            response = event_payload
        if response:
            response_id = str(response.get("id") or response_id or "")
            status = response.get("status")

            last_reasoning_summary = _emit_progress_from_payload(
                response,
                seen_progress_events,
                last_reasoning_summary,
            )

            if status in FINAL_RESPONSE_STATUSES:
                return response

        progress_lines: list[str] = []
        item = event_payload.get("item")
        if isinstance(item, dict):
            progress_lines, last_reasoning_summary = _extract_progress_updates(
                {"output": [item]},
                seen_progress_events,
                last_reasoning_summary,
            )

        event_type = str(event_payload.get("type") or "")
        if event_type.endswith("reasoning_summary_text.done"):
            text = event_payload.get("text")
            if isinstance(text, str):
                cleaned = text.strip()
                if cleaned and cleaned != last_reasoning_summary:
                    progress_lines.append(f"reasoning: {cleaned}")
                    last_reasoning_summary = cleaned

        _emit_ai_progress_lines(progress_lines)

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

    if response_id is None:
        raise RuntimeError("AI metadata review stream ended before a response ID was received")

    latest_payload = await _fetch_response(session, headers, response_id)
    return await _wait_for_response(session, headers, latest_payload, timeout_seconds)


def _should_use_background() -> bool:
    ai_cfg = cfg.upload.ai_review
    return ai_cfg.background or ai_cfg.reasoning_effort in {"high", "xhigh"} or ai_cfg.use_web_search


async def _request_ai_review(
    metadata: dict[str, Any],
    _tag_baseline: dict[str, Any],
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
        _tag_baseline,
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
                        error_payload = await _load_json_response(
                            resp,
                            non_json_error="OpenAI API returned a non-JSON response",
                        )
                        raise RuntimeError(f"OpenAI API error: {_extract_response_error(error_payload)}")

                    if use_background and resp.content_type == "text/event-stream":
                        response_payload = await _stream_response(
                            resp, session, headers, ai_cfg.timeout_seconds
                        )
                    else:
                        response_payload = await _load_json_response(
                            resp,
                            non_json_error="OpenAI API returned a non-JSON response",
                        )
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

    review["_opened_page_urls"] = sorted(_extract_opened_page_urls(response_payload))
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


def _normalize_artist_entries(values: Any) -> list[dict[str, str]]:
    if not values:
        return []

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if isinstance(value, dict):
            name = value.get("name")
            role = value.get("role")
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            name, role = value[0], value[1]
        else:
            continue

        if not isinstance(name, str) or not isinstance(role, str):
            continue
        normalized_name = name.strip()
        normalized_role = role.strip().lower()
        if not normalized_name or normalized_role not in ARTIST_IMPORTANCES:
            continue

        signature = (normalized_name, normalized_role)
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append({"name": normalized_name, "role": normalized_role})

    return deduped


def _normalize_artist_tuples(values: Any) -> list[tuple[str, str]]:
    return [(artist["name"], artist["role"]) for artist in _normalize_artist_entries(values)]


def _iter_review_metadata(review: dict[str, Any]):
    result_metadata = review.get("metadata", {})
    if not isinstance(result_metadata, dict):
        raise ValueError("AI metadata payload is invalid")

    for field in TOP_LEVEL_PATCH_FIELDS:
        if field in result_metadata:
            yield field, result_metadata[field]


def _normalize_review_metadata_value(field: str, value: Any) -> Any:
    if field == "artists":
        return _normalize_artist_entries(value)
    if field in NORMALIZED_METADATA_FIELDS:
        return _normalize_list(value)
    return value


def _resolve_review_metadata_value(
    field: str,
    before: Any,
    value: Any,
    source_url: str | None = None,
) -> Any:
    if field == "artists":
        return _normalize_artist_tuples(value)
    if field != "urls" or not source_url:
        return _normalize_review_metadata_value(field, value)

    merged_urls = _normalize_list(before)
    merged_urls = _normalize_list([*merged_urls, source_url])
    return _normalize_list([*merged_urls, *_normalize_list(value)])


def apply_ai_metadata_result(
    metadata: dict[str, Any],
    review: dict[str, Any],
    source_url: str | None = None,
) -> dict[str, Any]:
    updated = deepcopy(metadata)

    for field, value in _iter_review_metadata(review):
        updated[field] = _resolve_review_metadata_value(field, updated.get(field), value, source_url)

    return updated


def build_ai_review_diff(
    metadata: dict[str, Any],
    review: dict[str, Any],
    source_url: str | None = None,
) -> list[str]:
    lines: list[str] = []
    try:
        review_items = list(_iter_review_metadata(review))
    except ValueError:
        return lines

    for field, value in review_items:
        before = metadata.get(field)
        if field in NORMALIZED_METADATA_FIELDS:
            before = _normalize_list(before)
        after = _resolve_review_metadata_value(field, before, value, source_url)
        before_text = _format_diff_value(before)
        after_text = _format_diff_value(after)
        if before_text != after_text:
            lines.append(f"{field}: {before_text} -> {after_text}")
    return lines


def format_ai_review_citations(review: dict[str, Any]) -> list[str]:
    citations = review.get("citations", [])
    if not isinstance(citations, list) or not citations:
        return ["No citations were returned."]

    opened_page_urls = {
        normalized_url
        for url in review.get("_opened_page_urls", [])
        if (normalized_url := _normalize_evidence_url(url))
    }
    lines: list[str] = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        title = citation.get("title", "Untitled source")
        url = citation.get("url", "")
        supports = citation.get("supports", [])
        support_text = f" ({', '.join(supports)})" if supports else ""
        opened_text = ""
        if normalized_url := _normalize_evidence_url(url):
            opened_text = " [opened]" if normalized_url in opened_page_urls else " [not opened]"
        lines.append(f"- {title}{support_text}{opened_text}: {url}")
    return lines or ["No citations were returned."]


def _format_diff_value(value: Any) -> str:
    if value is None:
        return "(empty)"
    if isinstance(value, list):
        if value and all(isinstance(item, (dict, list, tuple)) for item in value):
            artists = _normalize_artist_entries(value)
            return ", ".join(f"{artist['name']} [{artist['role']}]" for artist in artists) if artists else "(empty)"
        return ", ".join(value) if value else "(empty)"
    text = str(value).strip()
    return text if text else "(empty)"


async def _finalize_manual_review(metadata: dict[str, Any], validator, manual_review) -> dict[str, Any]:
    try:
        validator(metadata)
    except InvalidMetadataError:
        return await _run_manual_review(metadata, validator, manual_review)
    return metadata


async def _apply_ai_review(
    metadata: dict[str, Any],
    review: dict[str, Any],
    source_url: str | None,
    validator,
) -> dict[str, Any] | None:
    try:
        sanitized_review, _warnings = _apply_ai_review_guardrails(metadata, review, source_url)
        updated_metadata = apply_ai_metadata_result(metadata, sanitized_review, source_url)
        validator(updated_metadata)
    except Exception as exc:
        click.secho(f"AI suggestions were rejected: {exc}", fg="red")
        return None

    click.secho("Applied AI metadata suggestions.", fg="green")
    return updated_metadata


async def _run_manual_review(
    metadata: dict[str, Any],
    validator,
    manual_review,
    *,
    enforce_required_fields: bool = True,
) -> dict[str, Any]:
    if "enforce_required_fields" in inspect.signature(manual_review).parameters:
        return await manual_review(
            metadata,
            validator,
            enforce_required_fields=enforce_required_fields,
        )
    return await manual_review(metadata, validator)


async def review_metadata_with_ai(
    metadata: dict[str, Any],
    tag_baseline: dict[str, Any],
    source_url: str | None,
    validator,
    manual_review,
    *,
    skip_initial_review: bool = False,
    apply_suggestions: bool = False,
) -> dict[str, Any]:
    ai_cfg = cfg.upload.ai_review
    if not ai_cfg.enabled:
        return await _run_manual_review(metadata, validator, manual_review)

    if cfg.upload.yes_all or skip_initial_review:
        current_metadata = deepcopy(metadata)
    else:
        current_metadata = await _run_manual_review(
            metadata,
            validator,
            manual_review,
            enforce_required_fields=False,
        )

    should_run = cfg.upload.yes_all or apply_suggestions or click.confirm(
        click.style("\nRun AI metadata review?", fg="magenta"),
        default=None,
    )
    if not should_run:
        return await _finalize_manual_review(current_metadata, validator, manual_review)

    current_metadata = deepcopy(current_metadata)
    previous_response_id = None
    user_instruction = None

    while True:
        click.secho("\nRunning AI metadata review...", fg="cyan", bold=True)
        try:
            ai_anchor_url = _choose_ai_anchor_url(current_metadata, source_url)
            review, previous_response_id = await _request_ai_review(
                current_metadata,
                tag_baseline,
                ai_anchor_url,
                user_instruction,
                previous_response_id,
            )
        except asyncio.CancelledError:
            click.secho("\nAI metadata review aborted by user.", fg="yellow")
            raise click.Abort() from None
        except Exception as exc:
            click.secho(f"AI metadata review failed: {exc}", fg="red")
            return current_metadata

        review, guardrail_warnings = _apply_ai_review_guardrails(current_metadata, review, source_url)
        diff_lines = build_ai_review_diff(current_metadata, review, source_url)
        summary = review.get("summary")
        if isinstance(summary, str) and summary.strip():
            click.echo(
                "\n"
                + click.style("AI summary:", fg="yellow")
                + f" {summary.strip()}",
                color=True,
            )

        for warning in guardrail_warnings:
            click.secho(f"AI guardrail: {warning}", fg="yellow")

        if diff_lines:
            click.secho("\nAI suggested metadata updates:", fg="yellow", bold=True)
            for line in diff_lines:
                click.echo(f"> {line}")
        else:
            click.secho("\nAI did not suggest metadata changes.", fg="yellow")

        citations = format_ai_review_citations(review)
        if citations and citations != ["No citations were returned."]:
            click.secho("\nAI citations:", fg="yellow", bold=True)
            for line in citations:
                click.echo(line)

        if not diff_lines:
            return await _finalize_manual_review(current_metadata, validator, manual_review)

        if cfg.upload.yes_all or apply_suggestions:
            applied_metadata = await _apply_ai_review(
                current_metadata,
                review,
                source_url,
                validator,
            )
            if applied_metadata is not None:
                return applied_metadata
            return await _finalize_manual_review(current_metadata, validator, manual_review)

        while True:
            choice = await click.prompt(
                click.style(
                    "\n[a]pply suggestions, [k]eep original, [p]rompt model and rerun",
                    fg="magenta",
                ),
                type=click.STRING,
            )
            choice = choice.strip().lower()[:1]

            if choice == "p":
                user_instruction = await click.prompt(
                    click.style("What should the model change or prioritize?", fg="magenta"),
                    type=click.STRING,
                )
                break

            if choice == "k":
                return await _finalize_manual_review(current_metadata, validator, manual_review)

            if choice == "a":
                if not diff_lines:
                    click.secho("There are no AI changes to apply.", fg="yellow")
                    continue

                applied_metadata = await _apply_ai_review(
                    current_metadata,
                    review,
                    source_url,
                    validator,
                )
                if applied_metadata is not None:
                    return await _run_manual_review(applied_metadata, validator, manual_review)
                continue

            click.secho(f"{choice} is not a valid AI review option.", fg="red")
