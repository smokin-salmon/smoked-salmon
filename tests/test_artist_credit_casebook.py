"""The artist-credit casebook (#869 step 1): what salmon gives today for real albums, store by store and merged.

Each file in tests/fixtures/artist_credits/ is one album: `today` (enforced here), `right` where it is settled,
`known_wrong` (text for people), the files' tags (constructed from one store's data) and what each store sent.
The fixtures' README says how to read a case, add one and update `today`.

Stages, each recorded as the release's artists ("name (role)") and its track count:
- <store>: `Scraper().scrape_release(url)`, its fetch replaced by the stored response.
- merge: every store's parse through `_combine_scraped` over the files' release data (`construct_rls_data` on the
  tags), with the files' own store as the chosen URL, then `remove_various_artists`: `salmon up` with every source.
  Also each track's credits, as "disc.track: name (role), ...", so a change on one track is seen.
- output, output-guests-in-title (a case with `output = true`, and the constructed title cases): what is written
  from the merge, with cfg.upload.formatting.guests_in_track_title off and on: ALBUMARTIST, and per track
  "ARTIST | TITLE", with "| REMIXER ..." when one is written.
- output-salmon-tags (a case with `[salmon_tags]`): the output stage over the same merge with the files' ARTIST and
  TITLE as salmon itself writes them, a control for the shape of the case's tags.
- merge-picard, output-picard (a case with `[[picard_track]]`; output-picard only with `output = true`): the merge,
  and the output with guests_in_track_title off, over the Picard-tagged files instead of `[[track]]`. Recorded only
  where they differ from merge and output; where not recorded, they must equal them.
"""

import asyncio
import copy
import json
import re
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

import msgspec
import pytest
from bs4 import BeautifulSoup
from mutagen.flac import FLAC, VCFLACDict

from salmon import cfg
from salmon.tagger.metadata import _combine_scraped, remove_various_artists
from salmon.tagger.pre_data import construct_rls_data
from salmon.tagger.retagger import (
    _generate_album_artist,
    append_guests_to_track_titles,
    create_artist_str,
    create_remixer_list,
    metadata_to_track_list,
)
from salmon.tagger.sources import METASOURCES
from salmon.tagger.tagfile import TAG_FIELDS, TagFile

CASES = Path(__file__).parent / "fixtures" / "artist_credits"
MUSICBRAINZ = Path(__file__).parent / "fixtures" / "stores" / "musicbrainz"
MBID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
# Each store's name in METASOURCES, what the merge keys on.
STORES = {
    "deezer": "Deezer",
    "musicbrainz": "MusicBrainz",
    "discogs": "Discogs",
    "bandcamp": "Bandcamp",
    "qobuz": "Qobuz",
}
# The written stages: guests_in_track_title off, then on.
OUTPUTS = {"output": False, "output-guests-in-title": True}
# The control stage: the output, guests_in_track_title off, from files in salmon's own ARTIST and TITLE shape.
CONTROL = "output-salmon-tags"
# The stages over the Picard-tagged files, each with the stage over the Deezer-shaped files it is recorded apart from
# only where the two differ.
PICARD = {"merge-picard": "merge", "output-picard": "output"}


def _record(case: Path) -> dict[str, Any]:
    return tomllib.loads(case.read_text(encoding="utf-8"))


def _cases() -> list[Path]:
    return sorted(CASES.glob("*.toml"))


def _stores(record: dict[str, Any]) -> list[str]:
    return [store for store in STORES if store in record.get("stores", {})]


def _stages(record: dict[str, Any]) -> list[str]:
    if record.get("constructed"):
        return list(OUTPUTS)
    control = [CONTROL] if "salmon_tags" in record else []
    picard = [s for s in PICARD if "picard_track" in record and (s == "merge-picard" or record.get("output"))]
    return [*_stores(record), "merge", *(OUTPUTS if record.get("output") else []), *control, *picard]


def picard_today(today: dict[str, Any], stage: str) -> dict[str, Any]:
    """A Picard stage's recorded value, or its Deezer stage's when the two are the same."""
    return today.get(stage, today[PICARD[stage]])


def _update_message(case_name: str, stage: str, got: dict[str, Any]) -> str:
    shown = "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in got.items())
    return f"today changed: if the change is meant, set [today.{stage}] in {case_name} to:\n{shown}"


def picard_verdict(case_name: str, stage: str, got: dict[str, Any], today: dict[str, Any]) -> str | None:
    """Why a Picard stage fails, or None: it is recorded only where it differs from its Deezer stage."""
    if got == today[PICARD[stage]]:
        same = f"[today.{stage}] is the same as [today.{PICARD[stage]}]: remove [today.{stage}] from {case_name}"
        return same if stage in today else None
    return _update_message(case_name, stage, got) if got != today.get(stage) else None


async def _returning(value: Any) -> Any:
    return value


def parse(record: dict[str, Any], store: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The store's parse of its stored response."""
    sent = record["stores"][store]
    scraper: Any = METASOURCES[STORES[store]].Scraper()
    if store == "musicbrainz":
        data = json.loads((MUSICBRAINZ / "release" / f"{sent}.json").read_text(encoding="utf-8"))
        url = f"https://musicbrainz.org/release/{data['id']}"
    elif store == "bandcamp":
        data, url = BeautifulSoup(sent, "lxml"), record["bandcamp_url"]
    else:
        data = json.loads(sent)
        page = {"deezer": "https://www.deezer.com/album", "discogs": "https://www.discogs.com/release"}
        url = f"{page.get(store, 'https://www.qobuz.com/album/case')}/{data['id']}"
    fetch = "fetch_page" if store == "bandcamp" else "fetch_data"
    monkeypatch.setattr(scraper, fetch, lambda *a, **k: _returning(data))
    return asyncio.run(scraper.scrape_release(url))


def _flac(tags: dict[str, list[str]]) -> TagFile:
    """A TagFile over an in-memory FLAC holding these tags, so salmon's own getattr reads them."""
    flac = FLAC.__new__(FLAC)  # no file behind it: FLAC() without a filename is deprecated
    flac.tags = VCFLACDict()
    for key, values in tags.items():
        flac[key] = values
    tag_file = TagFile.__new__(TagFile)
    object.__setattr__(tag_file, "mut", flac)
    return tag_file


def salmon_tagged(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The case's tags with each track's ARTIST and TITLE replaced by `[salmon_tags]`, as salmon writes them."""
    shape = record["salmon_tags"]
    return [
        {**track, "artist": [artist], "title": [title]}
        for track, artist, title in zip(record["track"], shape["artist"], shape["title"], strict=True)
    ]


def files(tracks: list[dict[str, Any]]) -> dict[str, Any]:
    """The release data `construct_rls_data` builds from these tags."""
    tags = {t["file"]: _flac({k: v for k, v in t.items() if k != "file"}) for t in tracks}
    audio = {f: {"duration": 200, "precision": 16} for f in tags}
    return asyncio.run(construct_rls_data(tags, audio, "WEB", None))


def merge(
    record: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tracks: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Every store's parse over the files' release data, the files being the case's `[[track]]` unless given."""
    stores = _stores(record)
    parses = [parse(record, store, monkeypatch) for store in stores]
    chosen = parses[stores.index(record["files_from"])]["url"]
    release = files(record["track"] if tracks is None else tracks)
    merged = _combine_scraped([STORES[s] for s in stores], parses, release, chosen)
    remove_various_artists(merged)
    return merged


def constructed(record: dict[str, Any]) -> dict[str, Any]:
    """A constructed case's tracks as merged metadata: each `[[title_track]]` with its title and credits."""
    tracks = {
        str(i): {"title": t["title"], "artists": [tuple(a) for a in t["artists"]]}
        for i, t in enumerate(record["title_track"], 1)
    }
    return {"artists": [], "tracks": {"1": tracks}}


def written(meta: dict[str, Any], guests_in_title: bool, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What salmon writes from the metadata, as `_track_changes` does: ALBUMARTIST, and ARTIST, TITLE, REMIXER."""
    formatting = msgspec.structs.replace(cfg.upload.formatting, guests_in_track_title=guests_in_title)
    monkeypatch.setattr(cfg.upload, "formatting", formatting)
    lines = []
    for track in metadata_to_track_list(meta["tracks"]):
        title = append_guests_to_track_titles(copy.deepcopy(track)) if guests_in_title else track["title"]
        remixers = create_remixer_list(track)
        line = [create_artist_str(track["artists"]), title, *([f"REMIXER {'; '.join(remixers)}"] if remixers else [])]
        lines.append(" | ".join(line))
    return {"albumartist": _generate_album_artist(meta["artists"]), "tracks": lines}


def summary(meta: dict[str, Any], per_track: bool = False) -> dict[str, Any]:
    tracks = sum(len(disc) for disc in meta["tracks"].values())
    got = {"release": [f"{name} ({role})" for name, role in meta["artists"]], "tracks": tracks}
    if per_track:
        got["credits"] = [
            f"{d}.{t}: " + ", ".join(f"{name} ({role})" for name, role in track["artists"])
            for d, disc in sorted(meta["tracks"].items(), key=lambda item: int(item[0]))
            for t, track in sorted(disc.items(), key=lambda item: int(item[0]))
        ]
    return got


def judge(right: dict[str, Any], today: dict[str, Any]) -> list[str] | None:
    """What is wrong in `today` by the case's settled `right`: [] when right, None when nothing is settled."""
    if not {"release_main", "performers", "not_main"} & right.keys():
        return None
    forms = {alt: name for name, alts in right.get("name_forms", {}).items() for alt in alts}
    if "albumartist" in today:
        return _judge_written(right, today, forms)
    credits = [(forms.get(n, n), r.rstrip(")")) for n, r in (c.rsplit(" (", 1) for c in today["release"])]
    mains = [n for n, r in credits if r == "main"]
    wrong = []
    if today["tracks"] == 0:
        wrong.append("no tracks")
    if "release_main" in right:
        want = right["release_main"]
        wrong += ["a right main missing"] if set(want) - set(mains) else []
        wrong += ["an extra main"] if set(mains) - set(want) else []
        wrong += ["mains in another order"] if set(mains) == set(want) and mains != want else []
    if "performers" in right:
        performing = {n for n, r in credits if r in {"main", "conductor"}}
        wrong += ["a performer missing"] if set(right["performers"]) - performing else []
        wrong += ["an extra main or conductor"] if performing - set(right["performers"]) else []
    wrong += ["a never-main name as main"] if set(right.get("not_main", [])) & set(mains) else []
    wrong += ["main and guest at once"] if {n for n, r in credits if r == "guest"} & set(mains) else []
    if "remixers" in right:
        wrong += ["an invented remixer"] if {n for n, r in credits if r == "remixer"} != set(right["remixers"]) else []
    return wrong


def _judge_written(right: dict[str, Any], today: dict[str, Any], forms: dict[str, str]) -> list[str]:
    """A written stage by `right`: the one release main as ALBUMARTIST and in every track's ARTIST; no `not_main`
    name among a track's ARTIST before its "(feat. ...)"."""
    wrong = []
    main = right.get("release_main", [])
    leads = [line.split(" | ")[0].split(" (feat. ")[0] for line in today["tracks"]]
    if len(main) == 1:
        wrong += (
            ["ALBUMARTIST not the release's main"]
            if forms.get(today["albumartist"], today["albumartist"]) != main[0]
            else []
        )
        wrong += (
            ["a track's ARTIST without the release's main"]
            if any(not _credited(main[0], lead, forms) for lead in leads)
            else []
        )
    wrong += (
        ["a never-main name in a track's ARTIST"]
        if any(_credited(name, lead, forms) for name in right.get("not_main", []) for lead in leads)
        else []
    )
    return wrong


def _credited(name: str, lead: str, forms: dict[str, str]) -> bool:
    """Whether `lead` ("A, B & C") credits `name`, in any of its forms, as a whole name: "Tyler, The Creator" holds
    a ", " of its own, so the lead isn't split first."""
    spellings = [name, *(alt for alt, canonical in forms.items() if canonical == name)]
    return any(re.search(rf"(?:^|, | & ){re.escape(s)}(?=$|, | & )", lead) for s in spellings)


def _params() -> list[Any]:
    return [
        pytest.param(case, stage, id=f"{case.stem}/{stage}") for case in _cases() for stage in _stages(_record(case))
    ]


@pytest.mark.parametrize(("case", "stage"), _params())
def test_today(case: Path, stage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    record = _record(case)
    if stage in PICARD:
        meta = merge(record, monkeypatch, record["picard_track"])
        got = summary(meta, per_track=True) if stage == "merge-picard" else written(meta, False, monkeypatch)
        verdict = picard_verdict(case.name, stage, got, record["today"])
        assert verdict is None, verdict
        return
    if stage == CONTROL:
        got = written(merge(record, monkeypatch, salmon_tagged(record)), False, monkeypatch)
    elif stage in OUTPUTS:
        meta = constructed(record) if record.get("constructed") else merge(record, monkeypatch)
        got = written(meta, OUTPUTS[stage], monkeypatch)
    elif stage == "merge":
        got = summary(merge(record, monkeypatch), per_track=True)
    else:
        got = summary(parse(record, stage, monkeypatch))
    assert got == record["today"].get(stage), _update_message(case.name, stage, got)


def test_each_case_records_exactly_the_stages_it_runs() -> None:
    """A [today] entry for a stage no longer run would be counted by the scoreboard but enforced by nothing.

    A Picard stage is recorded only where it differs from its Deezer stage, so it may be absent."""
    for case in _cases():
        record = _record(case)
        stages = set(_stages(record))
        assert stages - set(PICARD) <= set(record["today"]) <= stages, case.name


def test_each_musicbrainz_case_names_a_recorded_release() -> None:
    """A case holds its MusicBrainz release by MBID; the answer itself is in the shared recordings."""
    for case in _cases():
        mbid = _record(case).get("stores", {}).get("musicbrainz")
        if mbid is None:
            continue
        assert MBID.fullmatch(mbid), case.name
        assert (MUSICBRAINZ / "release" / f"{mbid}.json").is_file(), case.name


def test_each_picard_track_block_has_its_musicbrainz_releases_tracks() -> None:
    """A [[picard_track]] block holds only tags salmon reads, as many as the recorded release's tracks, one file each.

    The release is `picard_release` where the files are another edition from `stores.musicbrainz`.
    Its structure only: the values are not checked against the release, nor which cases have a block."""
    cases = [(case, record) for case in _cases() if "picard_track" in (record := _record(case))]
    read = {*TAG_FIELDS["FLAC"].values(), "file"}
    for case, record in cases:
        mbid = record.get("picard_release", record["stores"].get("musicbrainz"))
        recorded = MUSICBRAINZ / "release" / f"{mbid}.json"
        release = json.loads(recorded.read_text(encoding="utf-8"))
        tracks = record["picard_track"]
        assert len(tracks) == sum(len(medium["tracks"]) for medium in release["media"]), case.name
        assert all(set(track) <= read for track in tracks), case.name
        built = files(tracks)
        assert sum(len(disc) for disc in built["tracks"].values()) == len(tracks), case.name


def test_picard_stages_run_only_where_there_are_picard_files() -> None:
    plain = {"stores": {"deezer": "{}"}, "today": {}}
    assert "merge-picard" not in _stages(plain)
    picard = {**plain, "picard_track": [{}]}
    assert _stages(picard)[-1:] == ["merge-picard"]
    assert _stages({**picard, "output": True})[-2:] == ["merge-picard", "output-picard"]
    assert _stages({"constructed": True, "picard_track": [{}]}) == list(OUTPUTS)


def test_a_picard_stage_is_recorded_only_where_it_differs() -> None:
    a, b = {"tracks": 1}, {"tracks": 2}
    assert picard_verdict("case.toml", "merge-picard", a, {"merge": a}) is None
    assert "set [today.merge-picard]" in str(picard_verdict("case.toml", "merge-picard", b, {"merge": a}))
    redundant = picard_verdict("case.toml", "merge-picard", a, {"merge": a, "merge-picard": a})
    assert "remove [today.merge-picard]" in str(redundant)
    assert picard_verdict("case.toml", "merge-picard", b, {"merge": a, "merge-picard": b}) is None
    assert "set [today.merge-picard]" in str(
        picard_verdict("case.toml", "merge-picard", a, {"merge": b, "merge-picard": b})
    )


def test_a_name_holding_a_separator_is_judged_whole() -> None:
    right = {"release_main": ["Tyler, The Creator"], "not_main": ["Earth, Wind & Fire"]}
    output = {"albumartist": "Tyler, The Creator", "tracks": ["Tyler, The Creator", "Tyler, The Creator & Kali Uchis"]}
    assert judge(right, output) == []
    guest_as_main = {**output, "tracks": ["Tyler, The Creator & Earth, Wind & Fire"]}
    assert judge(right, guest_as_main) == ["a never-main name in a track's ARTIST"]


def test_scoreboard() -> None:
    """Today against right, per case and stage, grouped by what is wrong. Printed, never failing on wrongness.

    The output stages are counted apart from the parse and merge stages: an output mostly repeats its merge."""
    lines, kinds = [], Counter()
    tallies = {"parse and merge": [0, 0], "output": [0, 0]}  # right, judged
    for case in _cases():
        record = _record(case)
        verdicts = {stage: judge(record["right"], today) for stage, today in record["today"].items()}
        for stage, wrong in verdicts.items():
            if wrong is not None:
                tally = tallies["output" if stage.startswith("output") else "parse and merge"]
                tally[0] += not wrong
                tally[1] += 1
                kinds.update(wrong)
        shown = "; ".join(f"{s} {'open' if w is None else ', '.join(w) or 'right'}" for s, w in verdicts.items())
        lines.append(f"  {case.stem}: {shown}")
    lines[:0] = [
        "artist-credit casebook, by what is settled:",
        *(f"  {group}: right on {right} of {judged} judged stages" for group, (right, judged) in tallies.items()),
    ]
    lines += ["by what is wrong:"] + [f"  {kind}: {n}" for kind, n in kinds.most_common()]
    print("\n" + "\n".join(lines))
    assert tallies["parse and merge"][1], "no case has a settled right"
