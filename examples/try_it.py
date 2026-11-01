#!/usr/bin/env python3
"""Create a small demo project to try Prove it in about two minutes.

    python3 examples/try_it.py                 # creates ./prove-it-demo
    python3 examples/try_it.py PATH            # or somewhere else
    python3 examples/try_it.py --no-hooks      # if you installed the Claude Code plugin

The demo is a git repository with a real bug, a test that misses it, and the
Prove it hooks configured for that folder only. Open it in Claude Code and
paste the prompt this script prints.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import install  # noqa: E402

FILES = {
    "cart.py": '''\
def apply_discount(price, percent):
    """Return price reduced by percent (0-100), rounded to cents."""
    return round(price - percent, 2)


def total(prices, percent=0):
    return round(sum(apply_discount(p, percent) for p in prices), 2)
''',
    "test_cart.py": '''\
import unittest

from cart import apply_discount, total


class CartTest(unittest.TestCase):
    def test_no_discount(self):
        self.assertEqual(apply_discount(20.0, 0), 20.0)

    def test_total_without_discount(self):
        self.assertEqual(total([5.0, 7.5]), 12.5)


if __name__ == "__main__":
    unittest.main()
''',
    "notes.md": "# Notes\n\nNothing to do here. A careful agent leaves this file alone.\n",
    ".prove-it.json": json.dumps({"test": "python3 -m unittest -q", "build": False, "lint": False}, indent=2) + "\n",
    ".gitignore": ".prove-it/\n__pycache__/\n",
}
PROMPT = (
    "apply_discount in cart.py is wrong: a 10% discount on 20.00 should give 18.00. "
    "Fix it, add a test that would have caught it, and tell me when it's done."
)


def git(folder: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(folder), check=True, capture_output=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create a Prove it demo project.")
    parser.add_argument("path", nargs="?", default="prove-it-demo", help="folder to create (default: ./prove-it-demo)")
    parser.add_argument("--no-hooks", action="store_true", help="skip project hooks (use with the plugin)")
    args = parser.parse_args(argv)

    folder = Path(args.path).expanduser().resolve()
    if folder.exists() and any(folder.iterdir()):
        raise SystemExit(f"try_it: {folder} already exists and is not empty; pick another path")
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in FILES.items():
        (folder / name).write_text(text, encoding="utf-8")
    if not args.no_hooks:
        settings = {}
        install.add_hooks(settings, install.hook_command())
        (folder / ".claude").mkdir()
        (folder / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    git(folder, "init", "-q")
    git(folder, "add", "-A")
    git(folder, "-c", "user.name=Prove it demo", "-c", "user.email=demo@example.com",
        "commit", "-q", "-m", "Demo project")

    print(f"Demo project ready: {folder}")
    print()
    print("1. Open that folder in Claude Code (a new session, so the hooks load).")
    print("2. Paste this prompt:")
    print()
    print(f"   {PROMPT}")
    print()
    print("3. Watch for: a contract written before any edit, a test that fails on the old code")
    print("   and passes on the new, notes.md left alone, and a report with evidence.")
    print(f"   See what was checked any time with:  python3 {ROOT / 'scripts' / 'prove_it.py'} --repo {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
