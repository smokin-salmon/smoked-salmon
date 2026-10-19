# 0005. Config keys change with one release of warnings, never silently

- **Status:** Accepted
- **Date:** 2026-10-19
- **Links:** #761, #762

## Context

salmon decodes `config.toml` into the msgspec schema in `config/validations.py`. A key the schema does not
know is ignored with a yellow warning (#681). So when a key is renamed or moved, an old config still loads,
the old key is silently dropped, and salmon behaves differently with only that warning to say so. The
support case behind #681 shows users miss it.

0.21.0 (#761) reworks the config for new users: about 145 keys, a 390-line template with placeholder
values that salmon actually sends (three tracker cookies, a Discogs token, a Beatport login, four image
host keys), keys filed in the wrong section, and defaults picked before anyone looked at what an ordinary
upload needs (#762). It moves eight keys, removes two that did nothing, and changes two defaults. Until
now AGENTS.md said a new setting must not change behaviour for existing configs, which kept the config's
shape as it was.

Options considered:

- **Refuse every old key at once.** The simplest code and never silent, but a Docker `salmon web` that
  pulls the new image stays down until its config is edited.
- **Rewrite the user's file.** No edit needed, but it takes a TOML writer that keeps comments, and salmon
  would be writing the user's config.
- **Keep the old defaults and give new users the new ones through the template only.** Existing configs
  never change, but existing users never get the better default, and the schema and the template
  disagree for good.
- **Change a default and print a notice on every start while the key is unset.** Noise on every run for
  users who are happy with the new default.

## Decision

1. **One table of old keys** (`config/compat.py`), applied to the decoded TOML before the typed decode.
   Each entry gives the old path; the new path, with a value conversion when the type changes (the old
   `default_tracker = "RED"` becomes `default_trackers = ["RED"]`), or "removed" with the reason; and
   `refused_from`, the next minor version. salmon compares `refused_from` with its own version, so the
   switch from warning to refusal happens by itself at that release. An entry stays in the table for at
   least one release after `refused_from`, so a user who skips releases gets the message, not a silent drop.
   *Amended 2026-10-20 (#767): only renamed and moved keys have a `refused_from`. A removed key has none:
   its warning, or its refusal (point 3), lasts for as long as its entry stays in the table.*
2. **A renamed or moved key** is read under its old name, with a warning naming the new key and its
   section, until `refused_from`; from then on salmon refuses to start, with the same message. A config
   that sets both the old and the new name is refused: salmon does not guess which one is meant.
3. **A removed key** gets a warning when ignoring it changes nothing (it holds its old default, or the
   setting already did nothing). Otherwise salmon refuses to start, naming what replaces it or why it is
   gone.
4. **A value left over from an old template** (a placeholder such as `'get-from-site-cookie'`) counts as
   unset, with a warning naming the key. The list of old placeholders lives in code.
5. **A default may change for everyone** when the new one suits users better. That is a breaking change:
   the release notes list it with the line that keeps the old behaviour, and the PR that changes it says
   so.
6. **salmon never rewrites the user's config file.**
7. **Two shipped files.** `config.default.toml` is the short file a new user starts from.
   `config.reference.toml` documents every key, commented out, under uncommented section headers, so an
   uncommented key always lands in its own table. A test uncomments every key, decodes the result against
   the schema with no unknown key, and checks that every schema key appears.
8. **Where users see it.** Every warning is printed at start and shown on salmon web's Dashboard. The
   release notes open with the breaking changes, and a wiki page lists every rename and removal; the
   warnings link to it.

## Consequences

- An upgrade never changes behaviour silently: a key that stops working says so for one release, then
  stops salmon with the same message.
  *Amended 2026-10-20 (#767): a removed key that only warns keeps warning; it is never refused by version.*
- A changed default reaches every user who never set the key, and only the release notes tell them. That
  is the price of not carrying old defaults forever, so each one needs its release-notes line.
- The rule that a new setting must not change behaviour for existing configs still holds for new
  settings. Renames, removals and changed defaults follow this record instead.
- The reference file cannot drift from the schema, and the short template no longer has to document
  every key.
- Code that reads a moved key reads only the new name: the old one exists only in the table.
