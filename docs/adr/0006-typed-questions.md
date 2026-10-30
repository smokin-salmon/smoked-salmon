# 0006. Typed questions: the call site keeps its text and parser, the web gets a description of the options

- **Status:** Accepted
- **Date:** 2026-10-19
- **Links:** #760, #757, #759, amends [0004](0004-web-interface.md) section 2

## Context

0004 section 2 sends every question through `salmon.interaction`: `prompt`, `confirm`, `edit`,
`show_spectrals`. The terminal calls asyncclick; salmon web turns each call into a question for the browser.
A `prompt` reaches the browser as its text, its default and, only for a `click.Choice` type, a list of
choices. No question an upload asks uses `click.Choice`, so salmon web shows a bare text box for all of them:
the user types `3`, `n`, `*3 7 https://...` or `d`, exactly as in the terminal.

0.21.0 (#762) makes the web upload the common path, and an ordinary new-group WEB FLAC upload stops at about
twelve questions. Ten of them (the existing group, upload to this group, lossy master, spectral IDs, metadata
results, fields to edit, upload the torrent, fill a request, formats to convert, another tracker) need
buttons, checkboxes or cards to be usable in a browser (#757, #759).

What constrains the design:

- **The CLI must not change.** Same text, same accepted answers. 25 test files answer these questions by
  patching asyncclick's `prompt` or `confirm`: they check the text and the default they are given and
  return what a user would type.
- **The parsers do work.** #7's parser turns a pasted `torrentid=` URL into a group with a tracker request
  (`get_redirect_torrentgroupid`), reads a number above the list as a group ID, and reads `0` as the first
  recent upload. #11's `+` draws a random third. #12 scrapes each pick and URL it is given. Each loop re-asks
  on its own errors.
- **Free text collides with keys.** In #7, when deletion is offered, any reply that starts with `d` deletes
  the music folder with no second question (`uploader/__init__.py`, `except AbortAndDeleteFolder`), and a small number is a pick, not
  a group ID. A web text field labelled "Group URL or ID" that passed its text through would delete the
  folder for `delete-me` and pick result 2 for group `2`.
- `-yyy` answers these questions at the call site (`interaction.assume_defaults()`), as changed by #753 and
  #762's D7 and D8; that must not move.

Options considered:

- **New functions (`choose`, `choose_several`) that return typed values,** the terminal rendering the CLI
  text from the description and parsing the reply. One source of truth, but the rendered text must match
  today's byte for byte, every parser moves out of its call site (#7's needs a tracker request), and the
  tests that patch asyncclick pass only if the rendering is exact.
- **New functions that take the call site's own text** and parse the reply into a typed value. The text is
  safe, but the parsers still move and every call site's loop is rewritten.
- **The browser builds the CLI line itself** and sends it as a string. Less Python, but the CLI's grammar
  then lives in TypeScript too, and the server has to parse the line again to check it.

## Decision

### 1. The call site keeps its text, its default and its parser, and adds a description

`interaction.prompt` takes one more keyword, `options`, a description of the answers the call site already
accepts. `interaction.confirm` takes `labels`, the two buttons' words (`("Upload", "Re-run the metadata
step")`). The terminal ignores both: it calls asyncclick exactly as before, so the CLI and its tests cannot
change. salmon web draws the description, then turns the browser's answer into the line a terminal user
would have typed, and `prompt` returns that line to the call site's own parser. The `-yyy` branches stay
where they are.

The description is a set of frozen msgspec structs in `salmon.interaction`:

| Struct | Fields |
|---|---|
| `Options` | `items` (the options, in display order); `several` (any number may be picked); `free_text` (a `FreeText`, or none); `mark` (a `Mark`, or none) |
| `Option` | `key` (the reply the CLI accepts for it: `y`, `3`, `*`, `OPS`); `label`; `group` (a heading it is listed under: a store, "Search results"); `card` (a `Card`, or none); `action` (drawn apart, answers at once: New group, Manual, Abort, Delete music folder); `alone` (several only: a shortcut sent on its own: All `*`, None `0`, Random third `+`); `empty` (the option an empty reply means at this call site: #7's New group); `danger` (the browser asks for a second click before sending it) |
| `Card` | `fields` (label and value pairs, in order); `link` (the option's page on the tracker or store); `note` ("not an exact match", "DUPE RISK") |
| `FreeText` | `placeholder`; `ids` (a bare number is accepted as an ID) |
| `Mark` | `prefix` (`*`); `label` ("The store the files came from") |

A description is checked when it is built: keys unique and without spaces, at most one `empty`, `alone` and
`mark` only with `several`, `danger` only on an action.

### 2. The web sends a structured answer, and the server builds the line

The question keeps `kind: "prompt"` and gains `options` (the description, as plain JSON, so the egress
filter sees every string) and `preselected`. The browser answers `{"picks": [keys], "texts": [strings],
"mark": a pick, a text, or null}`. The server refuses, and asks again with the error as it does a bad text
answer today:

1. a string, for a question with options;
   *Amended 2026-10-30 (#816): an empty string is not refused. It takes the call site's default, as Enter
   does in the terminal, and asks again when there is none. The page never sends one. A client of the API
   may, and no text of its own reaches the call site's parser.*
2. a pick that is not a key, or a key picked twice;
3. an action or an `alone` key with any other pick, text or mark;
4. for a single question, anything but exactly one pick or exactly one text; for several, nothing at all;
5. a text when the question has no `free_text`; a text that is not one `http://` or `https://` URL without
   spaces or, with `ids`, a number greater than zero and than every numbered key, so a text can never be
   read as a key;
6. a mark the question has no `Mark` for, or one that is not exactly one of its picks or texts.

The line is the picks in the options' order, then the texts, the mark's prefix put before the marked one,
joined by spaces. The call site's parser then reads it as it reads a typed line, and may still ask again
(a URL from another site, a group the tracker does not know), as in the terminal.

`preselected` comes from the call site's `default`, read with the same grammar: split on spaces, the mark's
prefix stripped and noted, a token that matches a key (ignoring case: `N` is `n`) picked, anything else put
in the free text. An empty default preselects the `empty` option. There is one source for the default, and
the web never sends an empty line, so "an empty reply takes the default" never happens there.

A question without `options`, and a `confirm` without `labels`, reach the browser exactly as before: a text
box, or Yes and No. That is the fallback for every question not converted yet.

### 3. How the web draws it

- The question's text as the CLI prints it, on top: it is exact, and it is what the job's log shows.
- A single question: a button per option, or a card per option grouped under its headings; a click answers.
  The preselected option is marked as the default and has the focus, so Enter takes it.
- Several: a checkbox per option (on its card when it has one), the `alone` shortcuts as a toggle beside
  them that clears the ticks, and Send, disabled while nothing is picked.
- Free text: a field beside the options, with its own Send for a single question; for several, one row per
  URL with "another URL".
- The mark: a radio on each ticked option and each URL row, named by `Mark.label`.
- Actions: buttons in a row apart, answering at once. A `danger` option ("Delete music folder") needs a
  second click.
- A card's link is drawn only when it is `http://` or `https://`, and opens in a new tab with
  `rel="noopener noreferrer"`: store data is not trusted.

### 4. Scope and order

0.21.0 converts the ten questions of #762's D9: the eight that need no issue of their own (upload to this
group, lossy master, spectral IDs, fields to edit, upload the torrent, fill a request and its "are you
sure", formats to convert, another tracker, which converts the first tracker choice with it) with the
mechanism in one change, then #757 (the existing group) and #759 (metadata results, with
`preferred_sources`) on top. The others stay text boxes in the web and convert later, one at a time.

### 5. What proves the CLI unchanged

- No existing test changes: the converting changes only add tests.
- Per converted call site: its text and default as given to asyncclick, compared with the text on master;
  every key its description lists, sent through the terminal, accepted (a value, `Abort` or
  `AbortAndDeleteFolder`), never asked again; under `assuming_defaults()`, asyncclick not called and the
  same result as before.
- The web's preselection for each call site's defaults, each refusal above, and the line built for each
  kind.
- The test that rejects direct prompts in `src/salmon` passes unchanged: nothing in asyncclick is patched.

## Consequences

- The CLI cannot drift: its text, accepted answers and `-yyy` stay in the call site, untouched. The web's
  only knowledge of the CLI's grammar is the key on each option, the mark's prefix and the space between
  tokens, all in Python.
- A key the description lists that the parser does not accept would be a web-only bug. The per-site key test
  is what stops it, so every converted site needs one.
- A text field in the web takes only URLs and IDs that cannot be read as a key. A user who wants a reply the
  options do not offer types it in the terminal.
- A card shows only what the call site already has: metadata search results carry artist, title, year,
  track count and store, not the label.
- "Delete music folder" in the web takes two clicks where the terminal takes `d` and Enter.
- Ruled out: rendering the CLI text from the description, moving the parsers into `salmon.interaction`,
  building the line in the browser, and a second, web-only title per question.
