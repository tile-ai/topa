"""`topa hook pre-tool-use`: run a round's shell commands inside its container.

The agent runs on the host, where its own credentials work and where the round,
the TileOPs worktree and the TileFoundry source already live. Only execution
needs the container: the CUDA stack, the GPU and the installed `tilefoundry`.
Those directories are mounted into the container at the paths they have here, so
a command carries over unchanged -- only its interpreter moves.

Reads one PreToolUse event on stdin and answers on stdout. `topa round start`
installs it in the round's `.claude/settings.json`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import TextIO

# These act on files, on GitHub, or on the round itself, not on the compute stack.
# git, gh and the round's reviewer (`round.json`) use the host credentials; topa drives the
# host. The text tools stay with them, so `gh issue view 1 | head` is not moved into the
# container for its pipe.
HOST_COMMANDS = frozenset({
    "cd", "gh", "git", "ls", "cat", "pwd", "mkdir", "rm", "cp", "mv", "topa",
    "echo", "printf", "head", "tail", "wc", "grep", "sort", "uniq", "cut", "tr", "tee",
})


def _meta(round_dir: Path) -> dict:
    """What `topa round start` recorded in `round.json`, or nothing outside a round."""
    try:
        return json.loads((round_dir / "round.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# The lexer returns a run of these characters as one token. A run is a redirection
# when it is spelled as one, such as the `>&` of `2>&1`; any other run, `;`, `&&`, `|`
# or a newline among them, ends one command and starts the next.
PUNCTUATION = ";&|<>\n"
REDIRECTIONS = frozenset({"<", ">", ">>", "<<", "<<<", "<>", ">&", "<&", "&>", "&>>", ">|"})


def _commands(command: str) -> list[list[str]]:
    """The words of each command on the line, split at control operators outside quotes."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    commands: list[list[str]] = [[]]
    for token in lexer:
        if token and set(token) <= set(PUNCTUATION) and token not in REDIRECTIONS:
            commands.append([])
        else:
            commands[-1].append(token)
    return [words for words in commands if words]


# Finding where a substitution ends needs a shell parser, so any line with one goes to the container.
SUBSTITUTION = re.compile(r"[$<>]\(|`")


def runs_on_the_host(command: str, host: frozenset[str] = HOST_COMMANDS) -> bool:
    """Whether every program *command* invokes is a host-side one.

    `cd work && pytest` names a host command first and a container command
    second, so the whole line belongs in the container: one stray segment is
    enough to move it. A program is named by its basename, so the absolute
    `/.../.venv/bin/topa` the brief writes counts as `topa`. An operator inside
    quotes, as in `grep "a\\|b"`, does not split the line.
    """
    # A line continuation can split `$(`, but `\\` before a newline is not one: test both texts.
    if SUBSTITUTION.search(command) or SUBSTITUTION.search(command.replace("\\\n", "")):
        return False
    try:
        commands = _commands(command)
    except ValueError:  # unbalanced quotes: let the container decide
        return False
    return all(Path(words[0]).name in host for words in commands)


def decide(event: dict, round_dir: Path) -> dict | None:
    """The hook's answer to *event*, or None to let the command run as written."""
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    meta = _meta(round_dir)
    alias = meta.get("alias")
    host = (HOST_COMMANDS | {meta["reviewer"]}) if "reviewer" in meta else HOST_COMMANDS
    if alias is None or runs_on_the_host(command, host):
        return None
    cwd = event.get("cwd") or str(round_dir)
    remote = f"cd {shlex.quote(cwd)} && {command}"
    updated = dict(tool_input)
    updated["command"] = f"ssh -T {shlex.quote(alias)} {shlex.quote(remote)}"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
        }
    }


def pre_tool_use(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    event = json.load(stdin)
    round_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".")
    answer = decide(event, round_dir)
    if answer is not None:
        json.dump(answer, stdout)
    return 0
