"""Command-line interface for board-specific qualification operations."""

from __future__ import annotations

import argparse
import fcntl
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .bench import BenchManifest, OpenOcdBench
from .profile import load_profile

BOARD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _board_root(board_id: str) -> Path:
    if BOARD_ID.fullmatch(board_id) is None or board_id in {".", ".."}:
        raise ValueError("board ID must contain only letters, digits, dot, underscore or dash")
    return Path.home() / ".local" / "state" / "smateway" / "boards" / board_id


@contextmanager
def _board_lock(board_id: str) -> Iterator[None]:
    root = _board_root(board_id)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".bench.lock").open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _audit(board_id: str, action: str, status: dict[str, int | bool]) -> None:
    root = _board_root(board_id)
    root.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "action": action,
        "status": status,
    }
    with (root / "bench-commands.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smateway")
    commands = parser.add_subparsers(dest="command", required=True)
    bench = commands.add_parser("bench", help="control the leased static-selector image")
    bench.add_argument("--board-id", required=True)
    bench.add_argument(
        "--manifest",
        type=Path,
        default=Path("build/STM32C011F4P6/bench/pluto_bench.manifest.json"),
    )
    bench.add_argument("--openocd-config", type=Path, default=Path("openocd/rpi4-swd.cfg"))
    bench.add_argument(
        "--profile", type=Path, default=Path("profiles/fast20-v1/control_profile.json")
    )
    actions = bench.add_subparsers(dest="action", required=True)
    actions.add_parser("status")
    actions.add_parser("all-off")
    select = actions.add_parser("select")
    select.add_argument("antenna", choices=[f"ANT{index}" for index in range(1, 9)])
    select.add_argument("--lease-ms", type=int, default=1000)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command != "bench":
        raise AssertionError("unreachable command")

    with _board_lock(args.board_id):
        manifest = BenchManifest.load(args.manifest)
        controller = OpenOcdBench(manifest, args.openocd_config)
        if args.action == "status":
            action = "status"
            status = controller.status()
        elif args.action == "all-off":
            profile = load_profile(args.profile)
            action = "all-off"
            status = controller.request(profile.all_off_code, 0)
        elif args.action == "select":
            profile = load_profile(args.profile)
            selected = next(state for state in profile.states if state.name == args.antenna)
            action = f"select {args.antenna} lease_ms={args.lease_ms}"
            status = controller.request(selected.gpio_code, args.lease_ms)
        else:
            raise AssertionError("unreachable action")

        document = status.as_dict()
        _audit(args.board_id, action, document)
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
