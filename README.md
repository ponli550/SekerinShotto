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
uv run sekerinshotto ingest ~/Screenshots --json        # plan: what would be extracted
uv run sekerinshotto ingest ~/Screenshots --commit      # extract and write notes
uv run sekerinshotto status
uv run sekerinshotto reindex --commit                   # rebuild the index from manifests + notes
```

State (index, manifests, signed journal) lives in `~/.local/share/sekerinshotto/`, outside any vault and
never in a synced folder. Rerunning `ingest` skips images already extracted with the current extractor.

## Exit codes

`0` ok · `1` error (envelope carries `error`) · `2` ran, answer is "no" (e.g. notes not overwritten
because a human removed their generated markers).

## Tests

```bash
uv run pytest -q          # end-to-end tests use Apple Vision, so they run on macOS
```
