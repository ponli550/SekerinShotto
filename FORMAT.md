# Shared format — SekerinShotto ⇄ vault wrapper

Two independent tools, one format. SekerinShotto turns images into notes; the
vault wrapper manages an Obsidian vault. Neither imports the other. They meet
only at the files and the JSON described here. Any future ingester (PDF, web
clip, …) that writes this format plugs into the same wrapper.

Status: draft v0.12.0 — 2026-09-23.

## 1. CLI contract (both tools)

Base: `pan`'s contract (`pan schema --json`), not panvim's — pan wraps every
command and its exit 2 means a valid "no", which the cleanup gate needs.

- `--json` → exactly one JSON object on stdout, success or failure, every command.
- Envelope: `command` (string), `ok` (bool), `version` (semver, = schema_version),
  and exactly one of `data` (object, when `ok`) or `error` (string, when not `ok`).
- `<tool> schema --json` → every command, flags, output shape, exit codes, and
  an `agent_contract` text. Derived from the live command registry, never hand-kept.
- Exit codes:
  | code | name | meaning |
  |---|---|---|
  | 0 | ok | did what was asked |
  | 1 | error | could not run; `ok:false`, `error` says why |
  | 2 | violation | ran, answer is "no" (e.g. images held back by the cleanup gate); `ok:true`, verdict in `data` |
- Every write is a dry-run plan until `--commit`. The plan is the same shape as the commit result.
- Unknown names are rejected with the valid options listed; the tool never guesses.
- Versioning: patch = field added, minor = command added, major = a meaning changed or a field removed.

## 2. Manifest (the hand-off)

Each SekerinShotto batch writes one file into the output root:

`.sekerinshotto/batches/<batch_id>.jsonl` — one JSON record per line, one line per source item.

```json
{
  "format": "shared-note/0.1",
  "id": "sha256:9f2c…e1",
  "ingester": "sekerinshotto",
  "ingester_version": "0.1.0",
  "batch_id": "2026-09-23T09-40-00Z",
  "source_type": "image",
  "source_path": "/Users/…/Screenshots/IMG_0412.png",
  "source_state": "quarantined",
  "quarantined_at": "2026-09-23T09:40:17Z",
  "purge_after": "2026-09-30T09:40:17Z",
  "note_path": "screenshots/event/2026-09-23-example-com-event.md",
  "category": "event",
  "decided_by": "rule",
  "why": "text 'Hackathon'; and '22 January'",
  "group": "grp-3f96021a",
  "rank": 1,
  "group_size": 3,
  "toks": [1234, 5678],
  "sig": [9012, 3456],
  "dhash": "d1c3a5b7e9f0c2a4",
  "content_tokens": 118,
  "entities": {
    "qr":   [{"type": "url", "payload": "https://example.com/event"}],
    "urls": [{"raw": "https://example.com/event", "url": "https://example.com/event",
              "verified_by": "qr", "confidence": 1.0}],
    "domains": ["example.com"]
  },
  "text_chars": 412,
  "ocr_confidence": 0.94
}
```

Rules:
- `id` is the content hash of the source. It is the identity everywhere; paths are not.
- `source_state`: `present` | `held` | `attached` | `quarantined` | `purged`. After `purged`, the note is the only record.
- Quarantine lasts exactly 7 days to the second: `purge_after = quarantined_at + 604800 s`, UTC, ISO 8601 with seconds. Purge is eligible only when `now >= purge_after`; it still needs `--commit`.
- `held`: the image failed the confidence gate. It is kept, never quarantined, and its clock has not started. It becomes eligible only after its confidence is raised or it is confirmed (see §6).
- `decided_by`: `rule` | `laya` | `llm` | `user`. `verified_by`: `qr` | `crossref` | `known` | `allowed` | `none`
  (`reocr` and `dns` reserved). URL records may also carry `corrected`, `reason`, `joined` (extra lines
  merged) and `flag`: `truncated` | `invalid_tld` | `invalid_host`.
- Secrets (Wi-Fi passwords) are redacted before this file is written: `"payload": "WIFI:S:home;T:WPA;P:<redacted>"`.
- Unknown fields must be ignored by readers, never rejected.

## 3. Note file

Plain Markdown with YAML frontmatter. Obsidian needs nothing else.

```markdown
---
id: sha256:9f2c…e1
ingester: sekerinshotto
source_type: image
ingested: 2026-09-23T09:40:00Z
category: event
decided_by: rule
group: grp-0042
rank: 1
urls:
  - https://example.com/event
urls_unverified: [examp1e.com/docs]
domains: [example.com, examp1e.com]
tags: [sekerinshotto, event]
---

<!-- generated:start — owned by the ingester, rewritten on re-run -->
## Text
…OCR text…

## Links
- [example.com/event](https://example.com/event) · from QR
- `https://examp1e.com/docs` · read by OCR, unverified — not a link

## Group
[[grp-0042]] · rank 1 of 3
<!-- generated:end -->

## Notes
Anything here is owned by the user and never touched by any tool.
```

URL rendering rule:
- Links are made only for `qr`, `crossref`, `known` and `allowed` URLs without a flag. Everything else is code text,
  and sits in `urls_unverified` without a scheme so neither the reading view nor the Properties panel
  makes it clickable.
- A corrected URL is a link to the correction, with the raw reading and the reason beside it, and is
  listed in `urls_corrected` as `raw -> url`. The raw reading is never discarded.

URL correction (deterministic, offline after `domains update`):
- Candidates: the raw host with up to 2 OCR-confusion substitutions (I→l, |→l/i, 1↔l, q↔g, 0↔o, rn↔m, vv→w, 5↔s, cl→d).
- Evidence: `crossref` = the candidate's registrable domain (Public Suffix List eTLD+1) was decoded from a
  QR code anywhere in the index or batch; `known` = it ranks in the Tranco top 1M.
- Correct only if one candidate wins (best rank, then fewest edits; runner-up ≥ 10× worse unless same
  domain) and it beats the raw reading by ≥ 100× in rank, or the raw is unranked or not a valid host.
- A raw reading ranked in the top 100k is trusted as-is. Lookalikes can be real, ranked sites
  (`inkd.in` ≈ 794k vs `lnkd.in` ≈ 2k), hence the margin rather than "raw exists → keep".
- Known cost: a genuine phishing lookalike shown in a screenshot (`paypaI.com`) is mapped to the real
  domain. The raw reading stays next to the link so it remains visible.
- Hosts with a TLD that does not exist (`www.ome`, `register.gotow`) are flagged `invalid_tld`: almost always
  cut off on screen. A URL followed by `…` is flagged `truncated`. Neither is guessed.
- Wrapped URLs are joined across lines when the URL ends its line and the next line, in reading order and
  within 1.5 line heights, is a single URL-shaped token (not a word, a date, or a new URL).
- Measured on the 182-screenshot sample: 8 corrections, 0 wrong; 2 wrapped URLs fully recovered;
  2 cut-off URLs flagged. Not recoverable: paths faded out by the browser, and OCR misreads inside a path.

Personal allowlist (`<state>/domains/allow.txt`), for real sites too small for the Tranco top 1M:
- `domains suggest` ranks unverified OCR domains by how many held images they would release.
- `domains allow D[,D…] --commit` stores registrable domains (Public Suffix List eTLD+1, so
  `27a.onrender.com` vouches for that app only, not all of onrender.com). Refused without the PSL,
  for public suffixes (`com.my`), and for TLDs that do not exist.
- An allowed domain verifies OCR URLs (`verified_by: allowed`, linked) and is evidence for correcting a
  lookalike (`hackfest2O26.my` → `hackfest2026.my`).
- Allowing and `domains update` re-verify every stored URL without reading an image, rewrite the notes
  that changed, and release held images whose last unverified URL is now covered.
- `domains unallow` reverts those URLs to `none`; images already quarantined or purged stay where they are.
- Measured on the sample: allowing 8 suggested domains released 9 of 11 held images (7 quarantined,
  2 attached); the 2 left are the domains deliberately not allowed.

Reference lists: `domains update --commit` downloads Tranco top-1M, the Public Suffix List and the IANA TLD
list into `<state>/domains/`. It is the tool's only network access and fetches reference lists only,
never a URL read from a screenshot. Without it, correction falls back to QR crossref alone.

QR types (`qr` in frontmatter, `type` in the manifest): `url`, `payment`, `wifi`, `contact`, `mailto`,
`tel`, `smsto`, `geo`, `text`. `payment` = EMVCo merchant QR (DuitNow, PayNow, ...), accepted only if the
whole payload parses as tag-length-value and its CRC-16/CCITT matches; real DuitNow codes use format
indicator `02`, so no prefix check. Payment payloads carry personal names and account identifiers.

Ownership:
- The ingester may rewrite only the frontmatter keys it wrote and the text between the `generated` markers.
- Everything outside the markers belongs to the user. No tool overwrites it.
- Group hub notes (`grp-0042.md`) follow the same shape, with `source_type: group`.

## 4. Who does what

| Concern | SekerinShotto | Vault wrapper |
|---|---|---|
| Extract, dedupe, rank, classify, quarantine | ✔ | — |
| Write notes + manifest to an output root | ✔ | — |
| Know that Obsidian exists | ✘ | ✔ |
| Reconcile moved/renamed notes (by `id`) | — | ✔ |
| Orphans, broken links, search | — | ✔ |
| Uncategorized → LLM → `category` write-back | ✔ `tag` (quote-grounded) | ✔ may do the same by editing frontmatter |

The wrapper should reuse `ov` or the official Obsidian CLI where they already cover a row.

## 5. Output root (owned by SekerinShotto)

SekerinShotto manages two folders, split by one rule: what the user reads goes where Obsidian
can see it; working state and bulky images stay out of the vault.

```
<content>/                   readable results — lives inside the vault
  notes/<category>/*.md      one note per image
  groups/grp-*.md            one hub note per duplicate group
  attachments/               visual images kept forever (diagrams, slides), embedded in their note
  AUDIT.md                   human-readable audit of failures, regenerated each run

<state>/                     working state — never in a vault, never synced
  inbox/                     default drop folder (any folder can also be passed in)
  held/                      images that failed to read, kept (no clock), retried automatically
  quarantine/<batch_id>/     extracted images, purged exactly 604800 s after quarantined_at
  index.sqlite               hashes, text (FTS), entities, state, tombstones
  binding.json               which content root this state writes to
  domains/                   Tranco ranks (SQLite), Public Suffix List, IANA TLDs, info.json (list id)
  journal.key                HMAC key for the journal chain (0600); losing it makes old journals unverifiable
  batches/*.jsonl            manifests (§2)
  journal/*.jsonl            every file operation, for undo
  audit/*.jsonl              one line per failed or special-cased image
```

Defaults:
- A state folder is bound to one content root on first commit, in `<state>/binding.json` (not the DB,
  which is disposable). Passing a different `--content` later is an error.
- `<state>` = `~/.local/share/sekerinshotto/` (override `--state PATH`). Local disk, not synced. Backed up only if Time Machine (or similar) covers it.
- `<content>` = the path given by `--content PATH`. Until the main memory vault exists, that folder is
  opened in Obsidian as its own vault. Later it is moved (a real folder, not a symlink) into the main
  vault. Moving it is safe: identity is the `id` hash, and the wrapper reconciles paths.

Why this split:
- Concept notes (built by the wrapper + LLM) must link to image notes, and Obsidian cannot link across
  vaults. So image notes belong inside the main vault, not in a vault of their own forever.
- Quarantine and held images would otherwise sit in the vault: indexed by Obsidian, and hundreds of MB
  per batch pushed through vault sync, for files that are deleted a week later.
- The DB stays off any sync path by default, which removes the corruption risk below.
- `AUDIT.md` rows point at held images with `file://` links, so failures open in Preview from the
  vault without the images living in it.

Database — `<state>/index.sqlite`:
- SQLite (stdlib, no server), WAL mode, FTS5 for full-text search over OCR text.
- It is a derived index, never the only copy. Source of truth = notes + manifests + journal.
  `sekerinshotto reindex --commit` rebuilds it from those; a corrupt or deleted DB loses nothing.
  Tombstones (hashes of purged images) are recoverable because manifests record `source_state: purged`.
- One writer at a time (lock file). The vault wrapper never writes the DB; it edits frontmatter,
  and SekerinShotto picks the change up on its next reconcile.
- Must not live in a sync-managed folder (iCloud, Dropbox, Obsidian Sync): sync copies `index.sqlite`,
  `-wal` and `-shm` separately and corrupts it. The default `<state>` location already avoids this.

## 6. Image lifecycle — no manual review (implemented)

The user does not review images. They read results: the notes, and `AUDIT.md` for failures.
Nothing waits on a human, and every non-standard outcome is logged. `cleanup` moves the original files.

| Outcome | Rule | Image goes to | Clock |
|---|---|---|---|
| Read well | none of the below | `<state>/quarantine/<batch>/`, then purged | exactly 604800 s after `quarantined_at` |
| Visual | text covers < 8 % of the content area, < 300 chars, ≥ 180 gray levels, edge share ≥ 0.03, no decoded QR, category not `system` | `<content>/attachments/`, embedded at the top of its note | none, kept forever |
| Held | extraction failed, or an OCR URL is still `verified_by: none` without a cut-off flag | `<state>/held/` | none |
| Kept | `keep <id> --commit` (a diagram the visual rule misses) | `<content>/attachments/` | none |

- Visual rule measured on the sample: catches photos, video frames, camera feeds (14 of 182). It does NOT
  catch text-heavy diagrams or slides; those are quarantined unless `keep` is used.
- Held images keep their note (`source_state: held`, tag `sekerinshotto/held`), are re-extracted by `retry`
  (counted in `attempts`), are never deleted automatically, and leave `held/` when they pass or when the
  caller runs `confirm <id> --by llm|user --commit`. `confirm` and `keep` move only their target.
- `purge --commit` deletes only images with `now >= purge_after`, and only files whose resolved path is
  inside `<state>/quarantine/` (symlinks and `..` refused). The note then says it is the only record.
- `restore <id|batch> --commit` moves quarantined images back to their original path, never over an
  existing file. Purged images cannot be restored.
- An image routed by cleanup is never re-extracted from a new copy: its hash stays in the index.
- Every move is a signed journal row (`quarantine`, `attach`, `hold`, `restore`, `delete`).
- `ingest --cleanup` runs cleanup right after a commit. `cleanup`, `retry`, `ingest --cleanup` exit 2 while
  images are held: a valid answer, not a failure.
- Known gap: a byte-identical copy of an already-extracted image is skipped by ingest and left where it is.

Note frontmatter gains `source_state` and `purge_after`; the Source section states where the image is.

Audit:
- `<state>/audit/<batch>.jsonl`, one line per held, attached or redacted image: `id`, `source_path`,
  `outcome`, `reason`, `ocr_confidence`, `text_chars`, `qr_detected`, `attempts`, `at`, `note_path`.
- `<content>/AUDIT.md`, regenerated on every ingest, cleanup, purge, restore and retry: counts per state,
  next purge time, held images with reason, attempts and a `file://` link to open the image, attachments,
  and redacted Wi-Fi codes.
- `status --json` reports held and quarantine bytes and the next purge time.

Measured round trip on a copy of the 182-screenshot sample: 155 quarantined, 14 attached, 13 held (all
unverified URLs); with the clock pinned 1 s before the deadline 0 were due, at the deadline all were;
154 purged, the restored one untouched; journal intact.

## 6a. Classification, duplicate groups, ranking

Categories (built-in rules, first match wins): content first — `payment`, `event` (needs a keyword and a
date), `form`, `learning`, `health`, `shopping` — then by app — `travel`, `game`, `chat`, `email`, `social`,
`document`, `system`, `web` — else `uncategorized`. Every note records the rule's reason, e.g.
`text 'Pendaftaran'; and '20 DECEMBER'`. English and Malay keywords.

- Rules live in `rules_default.toml` inside the package; `<state>/rules.toml` replaces them.
  `organize --commit` re-applies them without re-extracting.
- `decided_by`: `rule` for the rules; a note whose `decided_by` is `llm`, `user` or `laya` keeps its
  category, and its folder follows that category. Rules never override a caller.
- Notes live at `notes/<category>/<date>-<app>-<id8>.md`. A category change moves the file (journal op
  `move`); the filename never changes, so Obsidian `[[links]]` keep working. Emptied folders are removed.

Duplicate groups ("the same one"):
- Content tokens = words and figures from OCR lines, excluding the status bar (top 4.5 %) and gesture bar
  (bottom 4 %), which would make every screenshot look alike.
- Two screenshots are grouped if they share a QR payload, or their exact token Jaccard ≥ 0.80, or the
  smaller one's tokens are ≥ 85 % contained in the other (a crop, or the same page in a viewer), or
  Jaccard ≥ 0.50 with a near-identical content-area image hash (dHash distance ≤ 6). Groups are
  transitive. MinHash LSH (32 bands × 2 rows) only picks candidate pairs; decisions use exact sets.
- Figures count as content so same-template screens stay apart: on the sample, sleep reports from
  different days scored ≤ 0.70 containment; true duplicates 0.80–0.97.
- Group ids (`grp-<id8>`) persist: a group keeps its id when members join; merged groups keep the id most
  members had. A dissolved group's hub note is deleted only if the user wrote nothing in it.
- Rank 1 = most information: 3·QR + 2·linkable URLs + min(chars/300, 4) + 2·OCR confidence + resolution,
  ties to the newer capture. Rank is informational here; cleanup uses it later.
- Hub note `groups/<gid>.md` lists members in rank order as `[[note]]` links; each member note has a
  `## Group` section linking back. Same ownership markers as image notes.

Measured on the 182-screenshot sample: 6 uncategorized (3 %); 5 groups, 12 screenshots, all 5 correct on
visual check (a re-screenshot in the gallery, a toast-only change, a certificate in a viewer vs a crop, a
document page vs a crop of one section, one calendar shown four ways). Two fresh runs produce identical
categories, group ids and files.

## 7. Laya — query layer, after everything

Laya is not a pipeline step. It never changes extraction, folders, or cleanup. It runs only after a batch is
extracted and tidied, as a tool the calling LLM uses directly over the finished notes.

- Purpose: the LLM asks a typed question (`choice`, `score`, `noul`) across many notes; Laya answers each note
  locally in milliseconds; the LLM reads only the top results instead of every note's text.
- Flow: search the index first (SQLite FTS on OCR text, or filters such as category/domain) to pick candidates,
  then ask Laya over those candidates. Searching the folder directly is also exposed, without Laya.
- Instructions for the LLM live in the schema's `agent_contract`: how to phrase typed questions, that Laya reads
  only the first 512–1024 tokens of a note, that answers are suggestions with a confidence, and that it never
  edits notes itself.
- Laya never sees the DB, paths, or SQL. SekerinShotto selects candidates, builds each note's "state",
  calls Laya, and stores the answers. Laya is a pure function: (state, questions) → answers.
- State is built structured-fields-first (category, domains, URLs, QR type, then OCR text) so the
  512–1024-token cut drops the text tail, not the facts.
- Answers are cached in the DB keyed by (note `id`, question hash, `model_rev`); a new model rev
  invalidates the cache automatically.
- The LLM gets filters (category, domain, date range, group, full-text), never raw SQL — raw SQL would
  bypass dry-run/`--commit`.
- Write-back: if the LLM acts on a Laya answer, it writes through the normal `--commit` path with
  `decided_by: laya` (or `llm`), and the note records `model_rev`.
- Optional install; if missing, the query commands exit `1` with a clear reason. Nothing else depends on it.

## 7a. Redaction and text commands (implemented)

- Notes, manifests and the index keep full text. The vault is the user's own memory.
- Every command that returns text toward a calling LLM redacts it first: `search` and `list` excerpts,
  `show` text, URL raw readings and QR payloads. Scrubbed: NRIC (only with a plausible YYMMDD), names
  (honorific, "Prepared by" cue, capitalised words around bin/binti/a/l/a/p, at most 4 each side), email,
  Malaysian phone numbers including `+60 12-…`, and payment QR payloads (replaced whole; merchant name →
  `[NAME]`). Ported from VeriPay `redact()`, with the patronymic rule bounded so it no longer swallows the
  rest of an OCR line. Known gaps: a bare name with no cue, an all-lowercase name in a chat.
- Wi-Fi QR passwords are redacted before anything is written, everywhere.
- Redaction is a boundary, not a vault guarantee: an LLM that reads the note files directly sees full text.

Commands:
- `search [QUERY] [filters]` — every word must match (SQLite FTS5; operators neutralised), plus filters
  `--category --domain --app --since --until --group --source-state --limit --offset`. Excerpts mark
  matches with «…».
- `list [--uncategorized] [filters]` — newest first; the uncategorized list is the LLM's tagging queue.
- `show ID` — one item: text, URLs with `verified_by` and correction reasons, QR, category and reason,
  group and rank, image state.
- `tag ID --category C --quote Q [--by llm|user] --commit` — a caller's category. For `llm`, `--quote`
  (≥ 8 chars) must appear verbatim in the OCR text, raw or redacted, ignoring case and whitespace; else
  exit 1. The quote is stored as `decided_evidence`, the note moves to `notes/C/`, and rules never
  override it. Grounding pattern adapted from VeriPay `_fact_in_source()`.
- IDs everywhere accept a full id, a ≥ 8-hex prefix, or a note filename; ambiguity is an error.

## 8. Panels (human surface, via panvim)

Two surfaces, same data: `--json` for the calling LLM, panvim panels for the user. SekerinShotto never
depends on panvim; panels are registry rows (`~/.config/panvim/popups.conf`) + keys files that call it.

Mechanism (panvim's, not ours): a panel re-runs its render command every `--interval` seconds into
`$PANVIM_OUT`; `--row` is a Lua pattern that pulls an id from the cursor line; key actions substitute `{row}`.

Render contract:
- `sekerinshotto panel <view>` prints plain aligned text, one item per line, id first (short hash, category
  or term) so `--row` can match it.
- Renderers read the index only, never OCR or Laya: a render runs every few seconds and must finish in
  well under 100 ms at 10k+ notes.
- Writes from keys go plan-first: the key runs the dry-run in `term-hold`, a second key commits.
  Purge never has a one-key commit.

| Panel | Rows | Keys (sketch) |
|---|---|---|
| `ss` home | batch status, counts per outcome, held size, next purge | `popup` to every panel below; run extract (plan, then commit) |
| `ss-class` | category, count, decided_by mix | open notes list filtered to category |
| `ss-concepts` | key term / concept, note count, linked concept note | open concept note; `stdin:` ask Laya about this term |
| `ss-groups` | duplicate group, members, rank-1 pick | open group hub note |
| `ss-audit` | failed/visual image, reason, attempts | open image, open note, retry (plan → commit), confirm |
| `ss-quarantine` | image, purge countdown to the second | restore (plan → commit), purge due (plan → commit) |
| `ss-notes` | note, category, domains | open note beside the board, `open` its URL |

Panels are scaffolded with `panvim new NAME --render ...` (dry run, then `--commit`) only once the
`panel` subcommand exists; a registry row pointing at a missing command would break `panvim popups`.

## 9. Decided, pending evidence

- Confidence methods: chosen by trial and error during the build. Each method (preprocessing,
  URL-safe OCR pass, second engine, cross-evidence, calibrated threshold) ships as a toggle, and the
  labeled test set decides which stay on.
- Concept notes: built by the vault wrapper + calling LLM from key terms and Laya queries. SekerinShotto
  only proposes key terms.
