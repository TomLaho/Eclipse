# Eclipse roadmap (queued — not yet built)

Guiding change from v1: **quality > speed.** Tom doesn't care if a meeting takes minutes to
process; he cares about transcription accuracy and the quality of insights/missed-item capture.
Phases are ordered by value; dashboard is parked.

---

## Phase 1 — Quality pass (transcription + insights)  ← top priority

### 1a. Transcription accuracy
- Default model **`small.en` → `medium.en`** (markedly better on names, numbers, accents).
  Optionally **`large-v3`** for best accuracy (see RAM note below).
- **`beam_size` 1 → 5** (v1 hardcoded 1 for speed). Real accuracy win; make it configurable.
- **Glossary / `initial_prompt`**: seed Whisper with a config list of client names, colleagues,
  acronyms, and jargon so proper nouns are spelled right. High-leverage for consulting calls.
- Enable `word_timestamps` (also needed for Phase 4 diarization).

### 1b. RAM-smart staging (unlocks bigger models on 7.8 GB)
- Process in **two stages: transcribe-all → release Whisper → enrich-all.** Ollama runs as a
  separate daemon, so freeing the Whisper model (in the Python process) before enrichment frees
  ~1-3 GB. Peak RAM becomes `max(whisper, llm)` instead of the sum.
- This lets `large-v3` transcription **and** a 7-8B insight model each fit, sequentially.

### 1c. Insight quality
- Upgrade local model **`llama3.2:3b` → `qwen2.5:7b` or `llama3.1:8b`** (much stronger extraction
  and reasoning). Pluggable via config.
- **Two-pass extraction**: (1) extract structured insights; (2) re-read the transcript asking
  "what action items, commitments, decisions, risks, or follow-ups were missed?" then merge.
  Directly serves Tom's "things I may have missed."
- **Relative-date resolver** *(was action #2)*: deterministically convert "Friday / next week /
  EOM" into ISO dates relative to the meeting date (fixes the 3B's date math). `dateparser`.
- Map-reduce summarization for long meetings (replaces the current ~9k-char truncation).

### 1d. Decision — biggest quality lever (privacy tradeoff)
v1 is **fully local** by Tom's original hard constraint. The single largest quality jump is to
make the **insight provider pluggable** (`ollama` | `claude`): audio + transcript always stay
local; only the transcript *text* optionally goes to the Claude API for best-in-class extraction,
with a local fallback. **This relaxes "fully local" for the text step — Tom's call.**
Alternative that keeps it 100% local: a **RAM upgrade (16-32 GB, ~A$80-150)** to run 8B+ models
comfortably. → *needs a decision before building 1c.*

---

## Phase 2 — Telegram (outbound)
- On each processed meeting: push a short summary + action items to Telegram.
- Scheduled **"open / missed / overdue" digest** (daily or weekly) to the phone.
- "You may have missed" alerts from the Phase-1c second pass.
- Tech: `python-telegram-bot` (async) as a small companion process (`eclipse telegram`), or raw
  Bot API long-polling (no public URL needed — single-user bot on the always-on PC).
- **Needs from Tom:** bot token (from @BotFather) + chat ID. Stored in `.env` (git-ignored).

---

## Phase 3 — Notion todo push + validation  *(was action #1)*
- App gets its **own Notion internal-integration token + Todos DB ID** (distinct from Claude
  Code's Notion access). I'll structure the DB for you via my access.
- **Primary flow:** extracted todos → Telegram with **✅ Approve / ✏️ Edit / ❌ Skip** inline
  buttons → on Approve, write to Notion (dedup against existing). Validate *before* it hits the list.
- **Fallback flow:** write todos to Notion with a `Review` status; Tom flips to `Approved` in Notion.
- Owner-aware: only *Tom's* action items are offered for push by default.
- **OpenClaw not required** — handled entirely inside Eclipse (no Hetzner dependency).
- Tech: `notion-client`; reuses the Phase-2 bot for callbacks.

---

## Phase 4 — Speaker diarization ("who said what")  *(was action #3)*
- Label speakers so action items attribute to the right person automatically.
- Tech: WhisperX or `pyannote.audio` (needs a HF token + PyTorch — heaviest dependency; RAM-tight,
  benefits from the Phase-1b staging). Optional/degradable if resources are tight.

---

## Phase 5 — Local web dashboard  *(was action #4 — PARKED)*
Tom: "not useful at this stage." Obsidian is the v1 surface. Revisit only if Obsidian limits us.

---

## What I'll need from Tom (when we build)
| Item | For | Notes |
|---|---|---|
| Decision: local-only vs optional-Claude insights | Phase 1c/1d | Quality vs "fully local" |
| Decision: validate via Telegram vs Notion review | Phase 3 | Recommend Telegram-approve |
| Telegram bot token + chat ID | Phase 2 | @BotFather; `.env` |
| Notion integration token + Todos DB ID | Phase 3 | App's own token; I can build the DB |
| (Optional) client/jargon glossary | Phase 1a | Improves name spelling |

## Suggested build order
1 (quality) → 2 (Telegram out) → 3 (Notion + approve) → 4 (diarization) → 5 (dashboard, parked).
Phase 1 needs no new credentials and delivers the most value, so it goes first.
