# SekerinShotto

Deterministic screenshot extractor. Images in, Markdown notes out: OCR text, QR payloads, and URLs,
with every URL marked by how it was verified. No model inside the tool; the calling LLM drives it
through `schema --json`, and the user reads the notes in Obsidian.

The shared format with the vault wrapper is in [FORMAT.md](FORMAT.md).

## Requirements

macOS 13+ (Apple Vision does the OCR and barcode detection), Python 3.12+, [uv](https://docs.astral.sh/uv/).

## Use

```bash
uv sync
export SEKERINSHOTTO_CONTENT=~/Notes/SekerinShotto      # where notes go (inside a vault)
uv run sekerinshotto schema --json                      # the full contract
uv run sekerinshotto domains update --commit            # once: reference lists for URL correction
uv run sekerinshotto ingest ~/Screenshots --json        # plan: what would be extracted
uv run sekerinshotto ingest ~/Screenshots --commit      # extract and write notes
uv run sekerinshotto organize --commit                  # re-apply rules/groups after editing rules.toml
uv run sekerinshotto cleanup --commit                   # route images: quarantine / attachments / held
uv run sekerinshotto purge --commit                     # delete quarantined images whose 7 days are up
uv run sekerinshotto retry --commit                     # re-extract held images
uv run sekerinshotto domains suggest                    # unverified domains holding images back
uv run sekerinshotto domains allow a.my,b.com --commit  # vouch for real sites; releases held images
uv run sekerinshotto status
uv run sekerinshotto reindex --commit                   # rebuild the index from manifests + notes
```

OCR misreads such as `docs.qoogle.com` or `Inkd.in` are corrected to the real domain when the evidence
points to exactly one candidate; the raw reading is always kept beside it. `domains update` is the only
command that touches the network, and it downloads reference lists, never a captured URL.

Notes are sorted into `notes/<category>/` by explainable rules (English and Malay keywords, then the app),
and screenshots with the same content — a crop, a re-screenshot, the same page in a viewer — are grouped
with a hub note in `groups/`, ranked so rank 1 is the most complete copy. Edit `<state>/rules.toml` to change
the rules; categories set by an LLM or by you are never overridden.

After extraction the images themselves are not kept: `cleanup` quarantines them for exactly 7 days, then
`purge` deletes them. Photos and other visual images are kept in the vault as attachments; images that could
not be read reliably are held and retried. You never review images; `AUDIT.md` lists what was held and why.
`restore` brings a quarantined image back; `keep` saves a diagram the visual rule missed.

State (index, manifests, signed journal) lives in `~/.local/share/sekerinshotto/`, outside any vault and
never in a synced folder. Rerunning `ingest` skips images already extracted with the current extractor.

## Asking many notes at once (optional)

```bash
uv tool install --editable '.[laya]'   # adds Laya, a small local model (Apple Silicon)
sekerinshotto ask hackathon --question '{"type":"noul","instructions":"Is this an event I can register for?"}'
```

Laya answers one typed question per candidate note, locally, and returns ranked suggestions with
confidence (about 4 in 5 right on topics in testing). It never changes a note; categories still need
`tag --quote`.

## Panels

```bash
uv tool install --editable .          # puts `sekerinshotto` on PATH
sekerinshotto panels install --commit # creates the ss* panels via `panvim new`
panvim popup ss                       # home panel; h c k g a p n jump between panels
```

Panels are read-only and never show screenshot text. Uppercase keys run the plan and ask you to type
`yes` before committing.

## Exit codes

`0` ok · `1` error (envelope carries `error`) · `2` ran, answer is "no" (e.g. notes not overwritten
because a human removed their generated markers).

## Tests

```bash
uv run pytest -q          # end-to-end tests use Apple Vision, so they run on macOS
```
