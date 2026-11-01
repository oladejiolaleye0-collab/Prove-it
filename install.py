#!/usr/bin/env python3
"""Install or remove the Prove it hooks in Claude Code settings.

    python3 install.py                     # all your projects (~/.claude/settings.json)
    python3 install.py --project PATH      # one project (PATH/.claude/settings.json)
    python3 install.py --uninstall         # remove the hooks again
    python3 install.py --dry-run           # show the result without writing

The hooks run this checkout's scripts/stop_hook.py with the Python running
this installer, using absolute paths, so they work on Windows too. Other
hooks and settings are kept; a backup of the old file is written first.
Use this or the Claude Code plugin, not both.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_SCRIPT = HERE / "scripts" / "stop_hook.py"
TOOL_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"
EVENTS = {
    "UserPromptSubmit": None,
    "PreToolUse": TOOL_MATCHER,
    "PostToolUse": TOOL_MATCHER,
    "Stop": None,
}


def hook_command(python: str = sys.executable, script: Path = HOOK_SCRIPT) -> str:
    return f'"{Path(python).as_posix()}" "{script.as_posix()}"'


def is_ours(hook: dict) -> bool:
    command = hook.get("command") if isinstance(hook, dict) else None
    return isinstance(command, str) and "stop_hook.py" in command and "prove" in command.lower()


def remove_hooks(settings: dict) -> int:
    """Drop every Prove it hook. Returns how many were removed."""
    removed = 0
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept.append(group)
                continue
            mine = [h for h in group["hooks"] if is_ours(h)]
            removed += len(mine)
            rest = [h for h in group["hooks"] if not is_ours(h)]
            if rest:
                kept.append({**group, "hooks": rest})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return removed


def add_hooks(settings: dict, command: str) -> None:
    """Add the Prove it hooks, replacing any earlier ones."""
    remove_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    for event, matcher in EVENTS.items():
        group = {"hooks": [{"type": "command", "command": command}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)


def settings_path(project) -> Path:
    if project:
        return Path(project).expanduser().resolve() / ".claude" / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError as exc:
        raise SystemExit(f"install: {path} is not valid JSON ({exc}); fix it first, nothing was changed")
    if not isinstance(data, dict):
        raise SystemExit(f"install: {path} must contain a JSON object; nothing was changed")
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install the Prove it hooks for Claude Code.")
    parser.add_argument("--project", metavar="PATH", help="install for one project instead of for all")
    parser.add_argument("--uninstall", action="store_true", help="remove the Prove it hooks")
    parser.add_argument("--dry-run", action="store_true", help="print the new settings without writing")
    args = parser.parse_args(argv)

    if not HOOK_SCRIPT.is_file():
        raise SystemExit(f"install: {HOOK_SCRIPT} is missing; run this from a full Prove it checkout")
    path = settings_path(args.project)
    if args.project and not path.parent.parent.is_dir():
        raise SystemExit(f"install: project folder not found: {args.project}")
    settings = load(path)

    if args.uninstall:
        count = remove_hooks(settings)
        action = f"removed {count} Prove it hook(s) from"
    else:
        add_hooks(settings, hook_command())
        action = "installed the Prove it hooks in"

    text = json.dumps(settings, indent=2) + "\n"
    if args.dry_run:
        print(text, end="")
        print(f"(dry run: would have {action} {path})", file=sys.stderr)
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(str(path), str(path) + ".bak")
    path.write_text(text, encoding="utf-8")
    print(f"Prove it: {action} {path}")
    if not args.uninstall:
        print(f"  hook command: {hook_command()}")
        print("  Restart Claude Code (or open a new session) for the hooks to take effect.")
        skill = Path.home() / ".claude" / "skills" / "prove-it"
        if not (skill / "SKILL.md").exists() and HERE != skill.resolve():
            print(f"  The skill is not in {skill}. Clone or copy this folder there to install it too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
