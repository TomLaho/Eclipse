# Eclipse

**A local-first meeting intelligence vault.** Drop a recording, get an organized,
searchable, action-tracked note — and a running answer to *"am I on top of everything?"*
Everything runs on your machine. No cloud, no accounts, your audio never leaves the PC.

```
 phone recording ──▶ inbox ──▶ transcribe ──▶ understand ──▶ vault note (Markdown)
   (cloud-synced)            (faster-whisper)   (local LLM)    + auto file & tags
                                                                     │
                                          ask · digest · todos ◀─────┘
```

## Why it exists

Existing local tools (Meetily, Hyprnote) are **live-capture** apps that sit on a call.
Eclipse is for the other workflow: you record meetings **in person, on your phone**, and
want the value *afterwards* — clean transcripts, automatic filing, and a cross-meeting
brain that surfaces every open commitment so nothing slips.

## What it does

- **Zero-touch ingest** — watches an inbox folder (synced from your phone). Drop and forget;
  identical files are never processed twice.
- **Local transcription** — [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on CPU, handles phone `.m4a` natively.
- **Automatic organization** — a local LLM extracts title, client, attendees, summary,
  decisions, and action items, then files the note under `vault/<client>/<date>-<title>.md`
  with tags. No manual tagging.
- **Obsidian-ready vault** — plain Markdown + YAML frontmatter; action items are real
  checkboxes. Browse, search, and link in [Obsidian](https://obsidian.md) — you own the files.
- **"Nothing missed" digest** — rolls up every open action item across all meetings, flags
  the ones that are yours, and (if the LLM is up) writes a short briefing of what's slipping.
- **Ask your meetings** — `eclipse ask "what did I promise Acme on pricing?"`
- **Private by default** — no network calls except to your local Ollama.

## Setup

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/), and (for enrichment)
[Ollama](https://ollama.com).

```bash
uv sync                       # install dependencies
uv run eclipse init           # create eclipse.toml + folders
ollama pull llama3.2:3b       # the local LLM (one-time, ~2 GB)
```

Then edit `eclipse.toml` and point `inbox_dir` at your synced recordings folder.

### Capture on Android (your setup)

1. Install a voice recorder that saves to a folder (e.g. the built-in Recorder, or
   *Easy Voice Recorder*).
2. Install the **Google Drive / OneDrive / Dropbox** desktop client on the PC and sync one
   folder, e.g. `Recordings/`.
3. On the phone, set the recorder to save into that same synced folder (or drop files into it).
4. Set `inbox_dir = "<that synced folder on the PC>"` in `eclipse.toml`.

Record → it syncs to the PC → Eclipse picks it up. The audio is then moved into the vault
(or deleted, per `audio_retention`), so the synced folder — and your phone — stay clean.

## Usage

```bash
uv run eclipse run            # process everything waiting in the inbox
uv run eclipse watch          # run continuously, processing new arrivals
uv run eclipse process FILE   # process one file anywhere on disk
uv run eclipse status         # config, readiness, storage usage
uv run eclipse digest         # roll up all open action items
uv run eclipse ask "..."      # question across all meetings
uv run eclipse todos          # draft your open commitments for review
```

## Configuration

All settings live in `eclipse.toml` (see `eclipse.example.toml`). Highlights:

| Setting | Default | Notes |
|---|---|---|
| `inbox_dir` | `inbox` | Point at your cloud-synced recordings folder |
| `audio_retention` | `keep` | `keep` / `archive` / `delete` after transcription |
| `whisper_model` | `small.en` | `tiny.en` → `medium.en` (bigger = better, slower) |
| `ollama_model` | `llama3.2:3b` | Any local Ollama model; swap up as RAM allows |

If Ollama isn't running, Eclipse still transcribes and files notes (marked `unenriched`)
and you can re-run enrichment later.

## Privacy

Audio, transcripts, and notes stay on disk. The only network traffic is to your local
Ollama server. `vault/`, `inbox/`, and `archive/` are git-ignored so meeting content is
never committed.

## Roadmap

- Speaker diarization ("who said what") — optional, heavier on RAM.
- Notion todo push (draft → approve → push) — `todos` already drafts the approval file.
- Semantic search via local embeddings for large vaults.
- Optional local web dashboard.

## Development

```bash
uv run pytest -q              # tests (no models/network needed — engine is mocked)
uv run ruff check . && uv run ruff format --check .
uv run mypy eclipse
```
