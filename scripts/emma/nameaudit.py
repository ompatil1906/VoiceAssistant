#!/usr/bin/env python3
"""Enforce the "no upstream product identity" rule.

EMMA Receptionist ships under its own identity. The upstream framework's name must
not appear in the product, the packaging, the documentation or the tests — and,
once the migration is finished, not anywhere else either.

This script scans the working tree and classifies every hit, so the rule is
enforced by a command instead of remembered:

    python3 scripts/emma/nameaudit.py
    python3 scripts/emma/nameaudit.py --verbose

Exit status is 0 when every remaining hit is in an allowed category, 1 otherwise.
Git history is never scanned and never rewritten; it is upstream's record.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The name being removed. Assembled so the audit's own source does not have to be
# allowlisted for the string it searches for.
NEEDLE = "pipec" + "at"

# Paths whose contents may still carry the name while the migration is in flight.
# Every entry states why, and every entry is temporary.
ALLOWED: dict[str, str] = {
    "scripts/emma/vendor.py": "the single constant that drives the branding scrub",
    "scripts/emma/nameaudit.py": "this audit's own needle",
    "LICENSE": "upstream BSD 2-Clause notice retained verbatim (licence requirement)",
    "NOTICE": "upstream attribution record (licence requirement)",
    "THIRD_PARTY_NOTICES/BSD-2-Clause-Daily.txt": "upstream licence text, verbatim",
}

# The upstream package tree is the extractor's input and is deleted at the end of
# the migration (MIGRATION_PLAN.md step 7).
UPSTREAM_TREE_PREFIX = "src/" + NEEDLE

# Upstream-only files scheduled for deletion; they are not part of the product.
PENDING_DELETION_PREFIXES: tuple[str, ...] = (
    ".claude",
    ".claude-plugin",
    ".codex",
    "docs/api/",
    "scripts/cli/",
    "scripts/flows/",
    "scripts/krisp/",
    "scripts/llm-with-backend/",
    "scripts/provider-watch/",
    "scripts/deprecations/",
    "scripts/check_licenses.py",
    "examples/",
    "evals/",
    "changelog/",
    "CHANGELOG.md",
    "COMMUNITY_INTEGRATIONS.md",
    "CONTRIBUTING.md",
    "MANIFEST.in",
    "pyrightconfig.json",
    "env.example",
    "uv.lock",
    ".pre-commit-config.yaml",
    ".gitignore",
    "tests/",
)

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".onnx", ".wav", ".png", ".jpg", ".lock"}


def tracked_files() -> list[Path]:
    """Every tracked or untracked-but-present file, relative to the repo root."""
    try:
        output = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return [
            path
            for path in REPO_ROOT.rglob("*")
            if path.is_file() and not (set(path.parts) & SKIP_DIRS)
        ]
    return [REPO_ROOT / name for name in output.decode().split("\0") if name]


def is_pending_deletion(relative: str) -> bool:
    return any(
        relative == prefix or relative.startswith(prefix.rstrip("/") + "/")
        for prefix in PENDING_DELETION_PREFIXES
    )


def scan(verbose: bool) -> int:
    categories: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path in tracked_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if any(part in SKIP_DIRS for part in Path(relative).parts):
            continue
        if path.suffix in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lowered = text.lower()
        if NEEDLE not in lowered:
            continue
        count = lowered.count(NEEDLE)
        if relative in ALLOWED:
            categories["allowed"].append((relative, count))
        elif relative.startswith(UPSTREAM_TREE_PREFIX + "/"):
            categories["upstream_tree"].append((relative, count))
        elif is_pending_deletion(relative):
            categories["pending_deletion"].append((relative, count))
        else:
            categories["VIOLATION"].append((relative, count))

    labels = {
        "VIOLATION": "PRODUCT SURFACE — must be fixed",
        "allowed": "allowed (licence record / scrub tool)",
        "upstream_tree": "upstream tree, deleted after extraction",
        "pending_deletion": "upstream-only file, pending deletion",
    }
    for key in ("VIOLATION", "allowed", "upstream_tree", "pending_deletion"):
        entries = sorted(categories[key])
        if not entries:
            continue
        print(f"\n{labels[key]}  ({len(entries)} files)")
        for relative, count in entries:
            print(f"  {count:>4}  {relative}")

    violations = categories["VIOLATION"]
    total = sum(len(v) for v in categories.values())
    print(
        f"\nfiles scanned with hits: {total}  |  violations: {len(violations)}"
    )
    return 1 if violations else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="list every file")
    args = parser.parse_args()
    return scan(args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
