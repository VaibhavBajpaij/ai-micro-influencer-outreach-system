"""Command-line interface for running the micro-influencer outreach pipeline and web UI."""

from __future__ import annotations

import argparse
import asyncio
import logging

import uvicorn
from rich.console import Console
from rich.table import Table

from outreach_system.config import Settings
from outreach_system.pipeline import OutreachPipeline

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="outreach",
        description="Automated Micro-Influencer Discovery, Filtering, Enrichment & Outreach CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run-all", help="Execute full end-to-end pipeline and export artifacts")
    run_p.add_argument("--niche", default=None, help="Niche preset: technology, fashion_beauty, fitness, fintech")
    run_p.add_argument("--count", type=int, default=None, help="Target number of influencers to discover (min 50)")
    run_p.add_argument("--send-mode", choices=["simulate", "smtp"], default=None, help="Sending mode")
    run_p.add_argument("--simulate-dms", action="store_true", help="Simulate sending queued Instagram DMs")
    run_p.add_argument("--force-regen", action="store_true", help="Force regenerate personalized messages")

    batch_p = sub.add_parser("run-batched", help="Execute large-scale (500+) pipeline in bounded batches")
    batch_p.add_argument("--niche", default=None, help="Niche preset")
    batch_p.add_argument("--total", type=int, default=500, help="Total target creators across batches (1-1000)")
    batch_p.add_argument("--batch-size", type=int, default=100, help="Bounded batch size per chunk (1-200)")
    batch_p.add_argument("--send-mode", choices=["simulate", "smtp"], default=None, help="Sending mode")
    batch_p.add_argument("--simulate-dms", action="store_true", help="Simulate sending queued Instagram DMs")
    batch_p.add_argument("--force-regen", action="store_true", help="Force regenerate personalized messages")

    disc_p = sub.add_parser("discover", help="Run Discovery + Filtering + Profile Enrichment")
    disc_p.add_argument("--niche", default=None)
    disc_p.add_argument("--count", type=int, default=None)

    pers_p = sub.add_parser("personalize", help="Generate personalized Email + IG DM for shortlisted creators")
    pers_p.add_argument("--force", action="store_true", help="Regenerate even if messages already exist")

    send_p = sub.add_parser("send", help="Send or simulate outreach emails for shortlisted creators")
    send_p.add_argument("--mode", choices=["simulate", "smtp"], default=None)
    send_p.add_argument("--simulate-dms", action="store_true")

    sub.add_parser("export", help="Export SQLite records to outputs/ CSV, JSON, and Markdown")

    serve_p = sub.add_parser("serve", help="Start FastAPI Review & Automation Dashboard")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "count", None) is not None and not (1 <= args.count <= 200):
        parser.error("--count must be between 1 and 200")
    settings = Settings()
    pipeline = OutreachPipeline(settings)

    if args.command == "run-all":
        summary = asyncio.run(
            pipeline.run_full_pipeline(
                niche=args.niche,
                target_count=args.count,
                send_mode=args.send_mode,
                simulate_dms=args.simulate_dms,
                force_regenerate=args.force_regen,
            )
        )
        table = Table(title="Micro-Influencer Outreach Pipeline Summary")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        for key, val in summary.model_dump().items():
            table.add_row(key, str(val))
        console.print(table)
        return 0

    if args.command == "run-batched":
        if not (1 <= args.batch_size <= 200):
            parser.error("--batch-size must be between 1 and 200")
        if not (1 <= args.total <= 1000):
            parser.error("--total must be between 1 and 1000")
        summary = asyncio.run(
            pipeline.run_batched_pipeline(
                niche=args.niche,
                total_target=args.total,
                batch_size=args.batch_size,
                send_mode=args.send_mode,
                simulate_dms=args.simulate_dms,
                force_regenerate=args.force_regen,
            )
        )
        table = Table(title="Batched Micro-Influencer Outreach Pipeline Summary")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        for key, val in summary.model_dump().items():
            table.add_row(key, str(val))
        console.print(table)
        return 0

    if args.command == "discover":
        profiles = asyncio.run(
            pipeline.run_discovery_filter_enrich(niche=args.niche, target_count=args.count)
        )
        qualified = sum(1 for p in profiles if p.status.value == "Qualified")
        console.print(
            f"[green]Discovered {len(profiles)} creators[/green] "
            f"({qualified} Qualified, {len(profiles) - qualified} Rejected)."
        )
        return 0

    if args.command == "personalize":
        messages = asyncio.run(pipeline.run_personalization(force=args.force))
        console.print(f"[green]Generated/loaded {len(messages)} personalized outreach sets.[/green]")
        return 0

    if args.command == "send":
        res = pipeline.run_sending(mode=args.mode, simulate_dms=args.simulate_dms)
        console.print(
            f"[green]Processed {len(res['email_entries'])} email dispatches and "
            f"{len(res['dm_entries'])} Instagram DM queue items.[/green]"
        )
        return 0

    if args.command == "export":
        files = pipeline.export_all_artifacts()
        for label, path in files.items():
            console.print(f"Exported {label}: {path}")
        return 0

    if args.command == "serve":
        uvicorn.run("outreach_system.api.app:app", host=args.host, port=args.port, reload=False)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

