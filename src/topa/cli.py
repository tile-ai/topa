"""The `topa` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from topa import __version__, ledger
from topa import config as config_module


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _setup(args: argparse.Namespace) -> int:
    from topa.setup_cmd import setup

    for line in setup(config_module.load()):
        print(line)
    return 0


def _hunt(args: argparse.Namespace) -> int:
    from topa.hunt import hunt, table

    page, rows = hunt(config_module.load(), refresh=not args.cached)
    if args.json:
        _print_json({"wiki": page.url, "nightly_commit": page.commit, "ops": rows})
        return 0
    print(f"{page.url}  nightly run {page.run_id}, commit {page.commit[:9]}")
    print()
    print(table(rows))
    return 0


def _round_start(args: argparse.Namespace) -> int:
    from topa.round import start

    meta = start(config_module.load(), args.op, issue=args.issue, dry_run=args.dry_run, force=args.force)
    if args.dry_run:
        _print_json(meta)
        return 0
    work = meta.get("op") or f"#{meta['issue']} ({meta['kind']})"
    print(f"round   {meta['round']}  {work}  {meta['branch']}  gpu {meta['gpu']}")
    print(f"dir     {meta['round_dir']}")
    print(f"pane    {meta['pane']}")
    print(f"watch   herdr agent read {meta['pane']}")
    print(f"say     topa round say {meta['round']} \"...\"")
    print(f"stop    topa round stop {meta['round']} [--rm]")
    return 0


def _round_say(args: argparse.Namespace) -> int:
    from topa.round import say

    confirmed = say(config_module.load(), args.round, " ".join(args.text))
    return 0 if confirmed else 1


def _round_status(args: argparse.Namespace) -> int:
    from topa.round import status

    rows = status(config_module.load(), every=args.all)
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("no rounds")
    for row in rows:
        pr = row["pr"]
        pr_text = f"#{pr['number']} {pr['state'].lower()}" if pr else "-"
        print(f"{row['round']:16} {row['state']:9} {row['op'] or '-':24} gpu {row['gpu'] if row['gpu'] is not None else '-':<2} "
              f"agent {row['agent'] or '-':8} pr {pr_text:14} {row['branch'] or ''}")
    return 0


def _round_stop(args: argparse.Namespace) -> int:
    from topa.round import stop

    for line in stop(config_module.load(), args.round, remove=args.rm):
        print(line)
    return 0


def _round_log(args: argparse.Namespace) -> int:
    for record in ledger.events():
        if args.round in (None, record.get("round")):
            print(json.dumps(record, ensure_ascii=False))
    return 0


def _run(args: argparse.Namespace) -> int:
    from topa.run_cmd import run

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    return run(config_module.load(), args.worktree, command)


def _gate(args: argparse.Namespace) -> int:
    from topa.pr import gate

    gate(config_module.load(), args.round)
    return 0


def _pr(args: argparse.Namespace) -> int:
    from topa.pr import open_pr

    open_pr(config_module.load(), args.round, reviewed=args.reviewed, watch=not args.no_watch)
    return 0


def _review_prepare(args: argparse.Namespace) -> int:
    from topa.review import prepare

    _print_json(prepare(config_module.load(), args.pr))
    return 0


def _review_post(args: argparse.Namespace) -> int:
    from topa.review import post

    _print_json(post(config_module.load(), args.pr))
    return 0


def _hook(args: argparse.Namespace) -> int:
    from topa.hook import pre_tool_use

    return pre_tool_use()


def _config(args: argparse.Namespace) -> int:
    if args.default:
        print(config_module.default_toml(), end="")
        return 0
    loaded = config_module.load()
    _print_json({name: getattr(loaded, name) for name in config_module.DEFAULTS})
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="topa", description="Agents build TileOPs kernels with TileFoundry.")
    top.add_argument("--version", action="version", version=f"topa {__version__}")
    sub = top.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="check tools, build the agent image, wire SSH")
    p.set_defaults(func=_setup)

    p = sub.add_parser("config", help="print the settings in effect")
    p.add_argument("--default", action="store_true", help="print a default config.toml")
    p.set_defaults(func=_config)

    p = sub.add_parser("hunt", help="the wiki's perf queue, with each op's rows and rounds")
    p.add_argument("--cached", action="store_true", help="read the cached wiki clone without pulling it")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_hunt)

    rounds = sub.add_parser("round", help="start, steer, inspect and stop rounds").add_subparsers(
        dest="round_command", required=True)
    p = rounds.add_parser("start", help="start a round on an op of the wiki queue, or on a TileOPs issue")
    p.add_argument("op", nargs="?")
    p.add_argument("--issue", type=int, help="work this TileOPs issue instead of an op")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="start beyond max_rounds")
    p.set_defaults(func=_round_start)
    p = rounds.add_parser("say", help="send the round's agent a message")
    p.add_argument("round")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=_round_say)
    p = rounds.add_parser("status", help="every round, its agent and its PR")
    p.add_argument("--all", action="store_true", help="include removed rounds")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_round_status)
    p = rounds.add_parser("stop", help="stop the agent and archive its session")
    p.add_argument("round")
    p.add_argument("--rm", action="store_true", help="also remove container, worktree and checkout")
    p.set_defaults(func=_round_stop)
    p = rounds.add_parser("log", help="print ledger events")
    p.add_argument("round", nargs="?")
    p.set_defaults(func=_round_log)

    p = sub.add_parser("run", help="run one command against a TileOPs worktree in a throwaway container")
    p.add_argument("--worktree", type=Path, default=Path.cwd())
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=_run)

    p = sub.add_parser("gate", help="check the round's PR evidence")
    p.add_argument("--round", help="round slug; default: the round directory holding the cwd")
    p.set_defaults(func=_gate)

    p = sub.add_parser("pr", help="gate, then push to the fork and open or update the PR")
    p.add_argument("--round", help="round slug; default: the round directory holding the cwd")
    p.add_argument("--reviewed", action="store_true")
    p.add_argument("--no-watch", action="store_true", help="do not wait on the PR checks")
    p.set_defaults(func=_pr)

    review = sub.add_parser("review", help="review a TileOPs PR once").add_subparsers(
        dest="review_command", required=True)
    p = review.add_parser("prepare", help="write the PR's review inputs and check out its head")
    p.add_argument("pr", type=int)
    p.set_defaults(func=_review_prepare)
    p = review.add_parser("post", help="post findings.json as one COMMENT review")
    p.add_argument("pr", type=int)
    p.set_defaults(func=_review_post)

    hook = sub.add_parser("hook", help="Claude Code hooks").add_subparsers(dest="hook_command", required=True)
    p = hook.add_parser("pre-tool-use", help="route a round's shell commands into its container")
    p.set_defaults(func=_hook)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
