# CLAUDE.md — Eclipse

Canonical state for a fresh session. Detail lives in `README.md`; this is the map + the decisions.

## What this is

A **local-first meeting intelligence vault**. Tom records meetings on his phone, the audio
syncs into an inbox, and Eclipse transcribes → understands → files each one as an
Obsidian-compatible Markdown note, then answers *"am I on top of everything?"* across all of
them. 100% local; the only network call is to a local Ollama server.

Pipeline: `inbox → transcribe (faster-whisper) → enrich (Ollama) → vault note + auto-tags → ask/digest/todos`.

## Architecture (one job per module)

| Module | Role |
|---|---|
| `eclipse/config.py` | `eclipse.toml` + `ECLIPSE_` env settings (pydantic-settings) |
| `eclipse/ingest/registry.py` | SHA-256 dedupe registry (sqlite) — idempotent ingest |
| `eclipse/ingest/watcher.py` | inbox scan + watchdog watch, cloud-sync "settle" wait |
| `eclipse/transcribe/whisper.py` | faster-whisper wrapper (CPU/int8, lazy-loaded) |
| `eclipse/enrich/llm.py` | Ollama client: structured extraction + safe fallback |
| `eclipse/vault/writer.py` | Markdown + YAML frontmatter, filed `vault/<client>/<date>-<slug>.md` |
| `eclipse/pipeline.py` | one-file orchestration + audio retention + quarantine |
| `eclipse/review.py` | cross-meeting layer: open-action rollup, digest, ask |
| `eclipse/cli.py` | typer CLI |

## Key decisions (don't re-litigate)

- **Capture = Android → cloud-synced folder** (Tom's choice). The tool just watches a local
  folder; it's cloud-provider-agnostic. After processing, audio is **moved out of the inbox**
  (into the vault `_audio/`, or archived/deleted per `audio_retention`) so the synced folder
  and phone stay clean. Tom confirmed audio may be deleted post-transcription; disk is ample (291 GB free).
- **Fully local, small model** on a constrained box (**7.8 GB RAM, no GPU**). Default
  `llama3.2:3b`. CPU generation measured at **~3 tokens/sec** → this is a **background/batch**
  tool (~2-5 min/meeting), not interactive. `keep_alive` avoids per-file reloads.
- **Enrichment uses `format:"json"`, NOT a JSON-schema grammar.** The nested-schema
  grammar-constrained decode hung past 300 s on this CPU; plain JSON + pydantic-validate +
  retry is fast and reliable. Output capped via `num_predict`.
- **Vault = plain Markdown** (Obsidian-compatible). No DB, no lock-in. Action items are real
  `- [ ]` checkboxes; ticking one in Obsidian removes it from the digest. ASCII `|` separators
  (not `—`/`·`) for portability.
- **Open-action rollup is deterministic** (parses checkboxes); the LLM only writes the optional
  digest *briefing* and answers `ask`. So the "nothing missed" core works even if Ollama is down.
- Pipeline is **synchronous** (batch tool — no async needed).

## Conventions

- Python 3.13, `uv`, **mypy strict**, **ruff** (check + format), **pytest**. pydantic v2,
  typer, structlog, httpx, faster-whisper, watchdog, python-frontmatter.
- 80+ tests, all green; `uv run pytest -q` is the gate. mypy/ruff clean.
- Engine is tested with mocked transcriber/enricher — tests need no models or network.
- `vault/ inbox/ archive/ data/ *.sqlite eclipse.toml` are git-ignored (meeting content
  never committed). Commit `eclipse.example.toml`.

## Commands

```bash
uv sync
uv run eclipse init                 # eclipse.toml + folders + setup steps
ollama pull llama3.2:3b             # one-time
uv run eclipse run                  # process inbox once
uv run eclipse watch                # process new arrivals continuously
uv run eclipse digest               # roll up open action items (+ LLM briefing)
uv run eclipse ask "..."            # Q&A across all meetings
uv run eclipse status               # readiness + storage
uv run pytest -q && uv run ruff check . && uv run mypy eclipse
```

## Built since v1 (now shipped)

- Speaker diarization (`diarize.py`, optional `[diarization]` extra; off by default, heavy).
- Notion todo push (`notion-push` + interactive `approve` via Telegram inline buttons).
- Telegram notifications + `telegram-pull` (phone → inbox transfer).
- Map-reduce enrichment for very long meetings (`llm._condense`, >30k chars).
- Standing **context profile** (`config.context_profile`): `context_profile.md`
  (git-ignored; redacted `context_profile.example.md` committed) is prepended to every
  LLM system prompt via `OllamaEnricher._with_profile`.
- **Semantic search** (`eclipse/search.py`): SQLite-backed embedding index over note
  chunks (Ollama `nomic-embed-text`), incremental refresh, pure-Python cosine. `ask`
  retrieves top-k chunks via `review._retrieve_corpus` and falls back to the compact
  summary corpus when the embed model isn't pulled. Build with `eclipse index`.

## What's next (not yet built)

- Optional local web dashboard (Tom chose Obsidian for v1).
```
