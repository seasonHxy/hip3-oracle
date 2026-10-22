from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .control import control, status
from .diagnostics import preflight
from .monitoring import MonitoringServer
from .operations import operate
from .publisher import DryRunPublisher, PublishError, SdkPublisher
from .readback import ReadbackError
from .replay import replay_journal
from .service import OracleService
from .sources import SourceError, build_sources
from .state import JsonStateStore, StateError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hip3-oracle", description="HIP-3 multi-source oracle")
    parser.add_argument("--config", default="config/example.json", help="path to JSON configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="validate configuration without fetching or publishing")
    subparsers.add_parser("status", help="inspect the local state snapshot without network access or writes")
    subparsers.add_parser(
        "preflight", help="collect quotes and check publication prerequisites without signing or saving"
    )
    pause = subparsers.add_parser("pause", help="persist a local publisher pause; does not halt DEX trading")
    pause.add_argument("--reason", required=True)
    resume = subparsers.add_parser(
        "resume", help="clear only the specified local pause; pending requests remain blocked"
    )
    resume.add_argument("--reason", required=True)
    resume.add_argument("--pause-id", required=True)
    once = subparsers.add_parser("once", help="run one aggregation and publishing cycle")
    once.add_argument("--live", action="store_true", help="publish through the official Hyperliquid SDK")
    run = subparsers.add_parser("run", help="run continuously")
    run.add_argument("--live", action="store_true", help="publish through the official Hyperliquid SDK")
    for operation in ("bootstrap", "reconcile"):
        command = subparsers.add_parser(
            operation, help="initialize or reconcile live state through read-only info queries"
        )
        command.add_argument("--reason", required=True, help="reason recorded in the local audit journal")
    replay = subparsers.add_parser("replay", help="verify decisions recorded in a JSONL journal without network calls")
    replay.add_argument("--journal", required=True)
    return parser


def _make_service(config_path: str, *, live_flag: bool) -> OracleService:
    config = load_config(config_path)
    sources = build_sources(config.sources)
    if live_flag and config.dry_run:
        raise PublishError("live publishing requires both --live and dryRun=false in the config")
    if not live_flag and not config.dry_run:
        raise PublishError("config has dryRun=false; add --live to confirm publishing or restore dryRun=true")
    publisher = SdkPublisher.from_environment(config) if live_flag else DryRunPublisher(emit=False)
    state = JsonStateStore(config.state_file, scope=config.state_scope)
    return OracleService(config, sources, publisher, state)


async def _once(config_path: str, live: bool) -> int:
    service = _make_service(config_path, live_flag=live)
    try:
        result = await service.cycle()
    finally:
        service.audit.close()
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
    return 0 if result.published else 2


async def _run(config_path: str, live: bool) -> int:
    service = _make_service(config_path, live_flag=live)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    try:
        with MonitoringServer(service.monitor):
            await service.run_forever(stop)
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        service.audit.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            config = load_config(args.config)
            build_sources(config.sources)
            print(
                json.dumps(
                    {
                        "valid": True,
                        "config": str(Path(args.config).resolve()),
                        "network": config.network,
                        "dex": config.dex,
                        "feeds": [feed.coin for feed in config.feeds],
                        "dryRun": config.dry_run,
                        "stateFile": str(config.state_file),
                        "stateScope": config.state_scope,
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "once":
            return asyncio.run(_once(args.config, args.live))
        if args.command == "run":
            return asyncio.run(_run(args.config, args.live))
        if args.command == "status":
            print(json.dumps(status(load_config(args.config)), sort_keys=True))
            return 0
        if args.command == "preflight":
            result = asyncio.run(preflight(load_config(args.config)))
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["ok"] else 2
        if args.command in {"pause", "resume"}:
            result = control(
                load_config(args.config), args.command, reason=args.reason, pause_id=getattr(args, "pause_id", None)
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command in {"bootstrap", "reconcile"}:
            print(json.dumps(operate(load_config(args.config), args.command, reason=args.reason), sort_keys=True))
            return 0
        if args.command == "replay":
            result = replay_journal(Path(args.journal))
            print(json.dumps(result, sort_keys=True))
            return 0 if result["matches"] else 2
        return 1
    except KeyboardInterrupt:
        return 130
    except (ConfigError, SourceError, StateError, PublishError, ReadbackError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
