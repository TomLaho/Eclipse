"""Eclipse command-line interface."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    # Annotation-only; the runtime imports stay lazy inside approve() so the CLI
    # doesn't pull in notion_client unless the approval flow is actually used.
    from eclipse.notify.notion import NotionTodos
    from eclipse.notify.telegram import TelegramClient

from eclipse import __version__, log, review
from eclipse.config import Config, load_config
from eclipse.enrich.llm import OllamaEnricher
from eclipse.ingest.registry import Registry
from eclipse.ingest.watcher import scan_inbox, watch
from eclipse.pipeline import Pipeline, PipelineResult
from eclipse.transcribe.whisper import Transcriber

# Maximum characters to send in a Telegram digest message.
_TELEGRAM_DIGEST_MAX = 3500

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Eclipse - drop meeting audio, get an organized, searchable vault.",
)
console = Console()


def _cfg(verbose: bool = False) -> Config:
    log.configure(verbose=verbose)
    cfg = load_config()
    cfg.ensure_dirs()
    return cfg


def _pipeline(cfg: Config, registry: Registry) -> Pipeline:
    transcriber = Transcriber(
        cfg.whisper_model,
        cfg.whisper_device,
        cfg.whisper_compute_type,
        cfg.whisper_language,
        beam_size=cfg.whisper_beam_size,
        initial_prompt=cfg.effective_initial_prompt,
        word_timestamps=cfg.whisper_word_timestamps or cfg.diarize,
    )
    enricher = OllamaEnricher(
        cfg.ollama_base_url,
        cfg.ollama_model,
        cfg.ollama_timeout_sec,
        two_pass=cfg.two_pass_extraction,
    )
    if cfg.enrich and not enricher.available():
        console.print(
            "[yellow]! Ollama not reachable - notes will be transcribed but not "
            "enriched. Start it with `ollama serve` and `ollama pull "
            f"{cfg.ollama_model}`.[/yellow]"
        )
    return Pipeline(cfg, transcriber, enricher, registry)


def _dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return round(total / (1024 * 1024), 1)


@app.command()
def version() -> None:
    """Print the Eclipse version."""
    console.print(f"Eclipse {__version__}")


@app.command()
def init() -> None:
    """Create eclipse.toml and the working folders, and print setup steps."""
    cfg = _cfg()
    toml_path = Path("eclipse.toml")
    example = Path("eclipse.example.toml")
    if not toml_path.exists():
        if example.exists():
            shutil.copy(example, toml_path)
        else:
            toml_path.write_text(_DEFAULT_TOML, encoding="utf-8")
        console.print(f"[green]Created {toml_path}[/green]")
    else:
        console.print(f"{toml_path} already exists - leaving it untouched.")

    console.print("\n[bold]Folders ready:[/bold]")
    for label, d in (("inbox", cfg.inbox_dir), ("vault", cfg.vault_dir)):
        console.print(f"  {label}: {d}")

    console.print(
        "\n[bold]Next steps[/bold]\n"
        "  1. Point your cloud-synced recordings folder at the inbox above "
        "(set `inbox_dir` in eclipse.toml).\n"
        "  2. Install Ollama (https://ollama.com) and run: "
        f"[cyan]ollama pull {cfg.ollama_model}[/cyan]\n"
        "  3. Drop an audio file in the inbox and run: [cyan]eclipse run[/cyan]\n"
        "  4. Open the vault in Obsidian to browse your meetings."
    )


def _summarize(results: list[PipelineResult]) -> None:
    written = sum(r.status == "written" for r in results)
    skipped = sum(r.status == "skipped" for r in results)
    errored = sum(r.status == "error" for r in results)
    tail = f", [red]{errored} errored[/red]" if errored else ""
    console.print(f"\n[bold]Done.[/bold] {written} written, {skipped} skipped{tail}.")
    for r in results:
        if r.status == "written" and r.note_path:
            console.print(f"  [green]+[/green] {r.source} -> {r.note_path.name}")
        elif r.status == "error":
            console.print(f"  [red]x[/red] {r.source}: {r.error}")


@app.command()
def run(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Process every audio file currently in the inbox, once."""
    cfg = _cfg(verbose)
    files = scan_inbox(cfg.inbox_dir)
    if not files:
        console.print(f"Inbox is empty: {cfg.inbox_dir}")
        return
    console.print(f"Found {len(files)} file(s) in {cfg.inbox_dir}")
    with Registry(cfg.registry_path) as registry:
        pipeline = _pipeline(cfg, registry)
        results = pipeline.process_batch(files)
    _summarize(results)


@app.command()
def process(
    file: Annotated[Path, typer.Argument(help="Path to an audio file")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Process a single audio file (it does not need to be in the inbox)."""
    cfg = _cfg(verbose)
    if not file.exists():
        console.print(f"[red]No such file:[/red] {file}")
        raise typer.Exit(1)
    with Registry(cfg.registry_path) as registry:
        pipeline = _pipeline(cfg, registry)
        result = pipeline.process_file(file)
    _summarize([result])


def watch_cmd(  # registered below as `eclipse watch`
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Watch the inbox and process new recordings as they arrive (Ctrl-C to stop)."""
    cfg = _cfg(verbose)
    with Registry(cfg.registry_path) as registry:
        pipeline = _pipeline(cfg, registry)

        # catch up on anything already waiting
        for f in scan_inbox(cfg.inbox_dir):
            pipeline.process_file(f)

        def handler(path: Path) -> None:
            # The watcher only invokes this for files that have already settled,
            # so there's no need to wait again here.
            pipeline.process_file(path)

        console.print(f"[bold]Watching[/bold] {cfg.inbox_dir} (Ctrl-C to stop)")
        try:
            for _ in watch(cfg.inbox_dir, handler):
                pass
        except KeyboardInterrupt:
            console.print("\nStopped.")


# register watch under the clean name "watch"
app.command(name="watch")(watch_cmd)


@app.command()
def status() -> None:
    """Show configuration, readiness, and storage usage."""
    cfg = _cfg()
    enricher = OllamaEnricher(cfg.ollama_base_url, cfg.ollama_model, cfg.ollama_timeout_sec)
    with Registry(cfg.registry_path) as registry:
        processed = registry.count()
    notes = sum(1 for _ in review.iter_notes(cfg.vault_dir))

    table = Table(title="Eclipse status", show_header=False)
    table.add_row("Inbox", str(cfg.inbox_dir))
    table.add_row("Vault", str(cfg.vault_dir))
    table.add_row("Meetings processed", str(processed))
    table.add_row("Notes in vault", str(notes))
    table.add_row("Audio retention", cfg.audio_retention)
    table.add_row("Audio stored", f"{_dir_size_mb(cfg.audio_dir)} MB")
    table.add_row("Whisper model", cfg.whisper_model)
    table.add_row("LLM model", cfg.ollama_model)
    table.add_row(
        "LLM reachable", "[green]yes[/green]" if enricher.available() else "[red]no[/red]"
    )
    console.print(table)


@app.command()
def digest(
    no_briefing: Annotated[bool, typer.Option("--no-briefing")] = False,
    telegram: Annotated[bool, typer.Option("--telegram")] = False,
) -> None:
    """Roll up every open action item across the vault into a digest note."""
    cfg = _cfg()
    body = review.build_digest(cfg, with_briefing=not no_briefing)
    path = review.write_digest(cfg, with_briefing=not no_briefing, body=body)
    open_count = len(review.collect_open_actions(cfg.vault_dir))
    console.print(f"[green]Digest written[/green] -> {path}")
    console.print(f"{open_count} open action item(s) across the vault.")

    if telegram:
        from eclipse.notify.telegram import TelegramClient

        client = TelegramClient.from_secrets()
        if client is None:
            console.print("[red]Telegram not configured (missing bot token or chat id).[/red]")
            raise typer.Exit(1)
        truncated = body[:_TELEGRAM_DIGEST_MAX]
        if len(body) > _TELEGRAM_DIGEST_MAX:
            truncated += "\n…(truncated)"
        try:
            client.send_message(truncated)
            console.print("[green]Digest sent to Telegram.[/green]")
        except Exception as exc:
            console.print(f"[red]Telegram send failed:[/red] {exc}")
            raise typer.Exit(1) from exc


@app.command()
def reenrich(
    note: Annotated[Path, typer.Argument(help="Path to a note .md to re-enrich")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Re-run LLM enrichment on an existing note's transcript (no re-transcription)."""
    from eclipse.pipeline import reenrich_note

    cfg = _cfg(verbose)
    if not note.exists():
        console.print(f"[red]No such note:[/red] {note}")
        raise typer.Exit(1)
    enricher = OllamaEnricher(
        cfg.ollama_base_url,
        cfg.ollama_model,
        cfg.ollama_timeout_sec,
        two_pass=cfg.two_pass_extraction,
    )
    if not enricher.available():
        console.print("[red]Ollama not reachable. Start it with `ollama serve`.[/red]")
        raise typer.Exit(1)

    new_path, pm = reenrich_note(cfg, enricher, note)
    if pm.enriched:
        console.print(f"[green]Re-enriched[/green] -> {new_path}")
    else:
        console.print(f"[yellow]LLM failed; wrote fallback[/yellow] -> {new_path}")

    # Mirror the normal pipeline: push the summary + "may have missed" to Telegram.
    if cfg.telegram_enabled and cfg.telegram_on_process:
        from eclipse.notify.telegram import notify_meeting

        notify_meeting(pm, cfg.me_aliases)
        console.print("[dim]Sent to Telegram.[/dim]")


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="A question about your meetings")],
) -> None:
    """Ask a question across all your meetings."""
    cfg = _cfg()
    console.print(review.answer_question(cfg, question))


@app.command()
def todos() -> None:
    """Draft your open commitments to a review file (the pre-Notion approval step)."""
    cfg = _cfg()
    path, n = review.write_todo_draft(cfg)
    console.print(f"[green]{n} open commitment(s)[/green] drafted -> {path}")


@app.command(name="telegram-test")
def telegram_test() -> None:
    """Send a test message to Telegram to confirm the connection."""
    from eclipse.notify.telegram import TelegramClient

    client = TelegramClient.from_secrets()
    if client is None:
        console.print(
            "[red]Telegram not configured. "
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env[/red]"
        )
        raise typer.Exit(1)
    try:
        client.send_message("✅ Eclipse connected")
        console.print("[green]Test message sent successfully.[/green]")
    except Exception as exc:
        console.print(f"[red]Telegram send failed:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command(name="telegram-pull")
def telegram_pull(
    process: Annotated[
        bool, typer.Option("--process", "-p", help="Process pulled files immediately")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Download new audio messages from your Telegram bot into the inbox."""
    from eclipse.notify.telegram import TelegramClient, pull_audio

    cfg = _cfg(verbose)
    client = TelegramClient.from_secrets()
    if client is None:
        console.print(
            "[red]Telegram not configured. "
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env[/red]"
        )
        raise typer.Exit(1)

    state_path = cfg.registry_path.parent / "telegram_offset"
    try:
        result = pull_audio(client, cfg.inbox_dir, state_path)
    except Exception as exc:
        console.print(f"[red]Telegram pull failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    for path in result.saved:
        console.print(f"  [green]+[/green] {path.name}")
    for name in result.skipped_too_big:
        console.print(
            f"  [yellow]![/yellow] {name} skipped (over Telegram's 20 MB bot limit "
            "- record in Opus, or use Syncthing for large files)"
        )

    if not result.saved:
        console.print("No new audio in Telegram.")
        return

    console.print(f"[bold]Pulled {len(result.saved)} file(s)[/bold] -> {cfg.inbox_dir}")

    if process:
        with Registry(cfg.registry_path) as registry:
            pipeline = _pipeline(cfg, registry)
            results = pipeline.process_batch(result.saved)
        _summarize(results)
    else:
        console.print("Run [cyan]eclipse run[/cyan] to process them.")


@app.command(name="notion-setup")
def notion_setup(
    parent: Annotated[str, typer.Option("--parent", help="Notion page id to create the DB under")],
) -> None:
    """Create the Todos database in Notion and print the new database id."""
    from eclipse.notify.notion import NotionTodos

    todos = NotionTodos.from_secrets()
    if todos is None:
        console.print("[red]Notion not configured. Set NOTION_ACCESS_TOKEN in .env[/red]")
        raise typer.Exit(1)
    try:
        db_id = todos.create_database(parent)
        console.print(f"[green]Todos database created:[/green] {db_id}")
        console.print("Add this to your .env file:")
        console.print(f"  NOTION_TODOS_DB_ID={db_id}")
    except Exception as exc:
        console.print(f"[red]Notion database creation failed:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command(name="notion-push")
def notion_push(
    approved: Annotated[bool, typer.Option("--approved/--review")] = False,
) -> None:
    """Push open actions to Notion (fallback flow — no interactive approval)."""
    from eclipse.notify.notion import NotionTodos

    cfg = _cfg()
    from eclipse.secrets import load_secrets

    secrets = load_secrets()
    if not secrets.notion_todos_db_id:
        console.print("[red]NOTION_TODOS_DB_ID not set in .env[/red]")
        raise typer.Exit(1)

    todos = NotionTodos.from_secrets()
    if todos is None:
        console.print("[red]Notion not configured. Set NOTION_ACCESS_TOKEN in .env[/red]")
        raise typer.Exit(1)

    actions = review.collect_open_actions(cfg.vault_dir, cfg.me_aliases, mine_only=True)
    if not actions:
        console.print("No open actions assigned to you.")
        return

    db_id = secrets.notion_todos_db_id
    status = "Approved" if approved else "Review"
    pushed = 0
    skipped = 0
    for action in actions:
        try:
            ok = todos.push_todo(db_id, action, status=status)
            if ok:
                pushed += 1
                console.print(f"  [green]+[/green] {action.task}")
            else:
                skipped += 1
                console.print(f"  [dim]~[/dim] {action.task} (already in Notion)")
        except Exception as exc:
            console.print(f"  [red]x[/red] {action.task}: {exc}")

    console.print(f"\n[bold]Done.[/bold] {pushed} pushed, {skipped} skipped.")


def _send_approval_requests(
    tg: TelegramClient, actions: list[review.OpenAction]
) -> tuple[dict[int, review.OpenAction], dict[str, int]]:
    """Send one approve/skip message per action.

    Returns ``(pending, eid_to_mid)`` — pending maps the sent message id to its
    action, eid_to_mid maps each action's eclipse_id back to that message id
    (callbacks carry the eclipse_id, the loop needs the message id).
    """
    from eclipse.notify.notion import eclipse_id

    pending: dict[int, review.OpenAction] = {}
    eid_to_mid: dict[str, int] = {}
    for action in actions:
        eid = eclipse_id(action)
        text = (
            f"<b>{action.task}</b>\n"
            f"Client: {action.client}  |  Meeting: {action.meeting_title}"
            + (f"  |  Due: {action.due}" if action.due else "")
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{eid}"},
                    {"text": "❌ Skip", "callback_data": f"skip:{eid}"},
                ]
            ]
        }
        try:
            mid = tg.send_message(text, reply_markup=markup)
        except Exception as exc:
            console.print(f"[red]Failed to send action to Telegram:[/red] {exc}")
            continue
        pending[mid] = action
        eid_to_mid[eid] = mid
    return pending, eid_to_mid


def _collect_approvals(
    tg: TelegramClient,
    notion: NotionTodos,
    db_id: str,
    pending: dict[int, review.OpenAction],
    eid_to_mid: dict[str, int],
) -> None:
    """Long-poll Telegram, push approved actions to Notion until all are resolved.

    Each callback's data is ``"approve:<eid>"`` / ``"skip:<eid>"``. We match the
    eid back to a pending message id and act on it. Stops when everything is
    resolved or after ~90s idle (3 polls x 30s timeout).
    """
    resolved = 0
    total = len(pending)
    update_offset: int | None = None
    idle_polls = 0
    max_idle_polls = 3

    while resolved < total and idle_polls < max_idle_polls:
        try:
            updates = tg.get_updates(offset=update_offset, timeout=30)
        except Exception as exc:
            console.print(f"[yellow]Poll error (will retry):[/yellow] {exc}")
            idle_polls += 1
            continue

        if not updates:
            idle_polls += 1
            continue

        idle_polls = 0  # reset on any activity

        for update in updates:
            # Advance the offset so we don't re-process the same update.
            update_offset = update["update_id"] + 1

            cb = update.get("callback_query")
            if not cb:
                continue

            # Acknowledge immediately so the spinner clears on the phone.
            try:
                tg.answer_callback_query(cb.get("id", ""))
            except Exception:
                pass  # not fatal

            data: str = cb.get("data", "")
            if ":" not in data:
                continue
            action_type, eid = data.split(":", 1)

            mid = eid_to_mid.get(eid)
            action = pending.get(mid) if mid is not None else None
            if mid is None or action is None:
                continue

            if action_type == "approve":
                try:
                    notion.push_todo(db_id, action, status="Approved")
                    tg.edit_message_text(mid, f"✅ Added to Notion\n<i>{action.task}</i>")
                    console.print(f"  [green]✅[/green] {action.task}")
                except Exception as exc:
                    tg.edit_message_text(mid, f"⚠️ Notion push failed: {exc}\n<i>{action.task}</i>")
                    console.print(f"  [red]x[/red] {action.task}: {exc}")
            elif action_type == "skip":
                try:
                    tg.edit_message_text(mid, f"❌ Skipped\n<i>{action.task}</i>")
                except Exception:
                    pass
                console.print(f"  [dim]❌[/dim] {action.task}")

            resolved += 1
            del pending[mid]

    if pending:
        console.print(f"\n[yellow]{len(pending)} action(s) not resolved (timed out).[/yellow]")
    else:
        console.print(f"\n[bold]Done.[/bold] {total} action(s) processed.")


@app.command()
def approve() -> None:
    """Interactively approve open actions via Telegram inline buttons → push to Notion."""
    from eclipse.notify.notion import NotionTodos
    from eclipse.notify.telegram import TelegramClient
    from eclipse.secrets import load_secrets

    cfg = _cfg()
    secrets = load_secrets()

    # Gate: both Telegram and Notion must be configured.
    tg = TelegramClient.from_secrets()
    if tg is None:
        console.print("[red]Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).[/red]")
        raise typer.Exit(1)

    notion = NotionTodos.from_secrets()
    if notion is None:
        console.print("[red]Notion not configured (NOTION_ACCESS_TOKEN).[/red]")
        raise typer.Exit(1)

    if not secrets.notion_todos_db_id:
        console.print("[red]NOTION_TODOS_DB_ID not set in .env[/red]")
        raise typer.Exit(1)

    db_id = secrets.notion_todos_db_id
    actions = review.collect_open_actions(cfg.vault_dir, cfg.me_aliases, mine_only=True)
    if not actions:
        console.print("No open actions assigned to you.")
        return

    console.print(f"Sending {len(actions)} action(s) to Telegram for approval…")
    pending, eid_to_mid = _send_approval_requests(tg, actions)
    if not pending:
        return
    _collect_approvals(tg, notion, db_id, pending, eid_to_mid)


_DEFAULT_TOML = """\
# Eclipse configuration. Edit paths to taste.
# Secrets (Telegram / Notion / HuggingFace tokens) live in .env, not here.

# Point this at your cloud-synced recordings folder (Drive/OneDrive/Dropbox).
inbox_dir = "inbox"
vault_dir = "vault"
archive_dir = "archive"

# keep | archive | delete  (what to do with audio after transcription)
audio_retention = "keep"

# Transcription (faster-whisper, CPU). Options: tiny.en base.en small.en medium.en
# small.en (~1 GB) is the safe default on a 7.8 GB no-GPU box; medium.en is better
# but heavier. Whisper is unloaded before the LLM runs, so peak RAM = max(whisper, llm).
whisper_model = "small.en"
whisper_compute_type = "int8"
whisper_language = "en"
whisper_beam_size = 5
whisper_word_timestamps = false

# Local LLM via Ollama
enrich = true
ollama_model = "llama3.2:3b"
ollama_base_url = "http://localhost:11434"
# Second LLM pass for missed commitments (costs time, not peak RAM).
two_pass_extraction = true
include_transcript_in_note = true

# Names that mean "you" (for flagging your own action items)
me_aliases = ["me", "I", "Tom"]

# Notifications (Telegram) - needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env.
telegram_enabled = false
telegram_on_process = true

# Speaker diarization (HEAVY - pyannote + torch, needs HF_TOKEN). Off by default;
# this is the one feature that can strain a 7.8 GB box.
diarize = false
diarize_model = "pyannote/speaker-diarization-3.1"
"""


if __name__ == "__main__":
    app()
