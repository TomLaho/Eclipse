"""Eclipse command-line interface."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from eclipse import __version__, log, review
from eclipse.config import Config, load_config
from eclipse.enrich.llm import OllamaEnricher
from eclipse.ingest.registry import Registry
from eclipse.ingest.watcher import scan_inbox, wait_until_stable, watch
from eclipse.pipeline import Pipeline, PipelineResult
from eclipse.transcribe.whisper import Transcriber

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
            if wait_until_stable(path):
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
) -> None:
    """Roll up every open action item across the vault into a digest note."""
    cfg = _cfg()
    path = review.write_digest(cfg, with_briefing=not no_briefing)
    open_count = len(review.collect_open_actions(cfg.vault_dir))
    console.print(f"[green]Digest written[/green] -> {path}")
    console.print(f"{open_count} open action item(s) across the vault.")


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


_DEFAULT_TOML = """\
# Eclipse configuration. Edit paths to taste.

# Point this at your cloud-synced recordings folder (Drive/OneDrive/Dropbox).
inbox_dir = "inbox"
vault_dir = "vault"
archive_dir = "archive"

# keep | archive | delete  (what to do with audio after transcription)
audio_retention = "keep"

# Transcription (faster-whisper, CPU). Options: tiny.en small.en medium.en
whisper_model = "small.en"
whisper_compute_type = "int8"

# Local LLM via Ollama
ollama_model = "llama3.2:3b"

# Names that mean "you" (for flagging your own action items)
me_aliases = ["me", "I", "Tom"]
"""


if __name__ == "__main__":
    app()
