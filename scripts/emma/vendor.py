#!/usr/bin/env python3
"""Vendor the EMMA runtime subset out of the upstream package tree.

This is the reproducible half of the migration: it walks the transitive import
closure of the modules EMMA actually runs, copies those modules into the
first-party ``emma`` package with rewritten imports, scrubbed branding and
provenance headers, and ports the upstream tests that cover them.

Provenance is preserved mechanically, not by hand: every copied file keeps its
original copyright and SPDX header and gains a QuantumLoopAI modification notice.
``--check`` reports what would be produced without writing anything.

The upstream package is discovered rather than named: it is the one package
directory under ``src/`` that is not ``emma``. Module mapping is therefore
expressed in *relative* paths, which keeps this tool free of upstream branding
and makes re-pointing it at a future upstream a one-flag change.

Usage::

    python scripts/emma/vendor.py --check
    python scripts/emma/vendor.py
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_graph import build_graph, closure, discover_modules, upstream_root  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
TARGET_DIR = SRC / "emma"
TARGET_ROOT = "emma"
PROJECT_URL = "https://github.com/QuantumLoopAI/emma-receptionist"

# Modules that make up a working EMMA voice session. Everything else is dropped.
SEEDS_REL: tuple[str, ...] = (
    # Runtime engine and lifecycle
    "pipeline.worker",
    "workers.runner",
    "tests.utils",
    # Transports
    "transports.livekit.transport",
    "transports.websocket.fastapi",
    # Speech in / speech out. The Piper integration is deliberately absent: the
    # piper-tts distribution it needs (OHF-Voice/piper1-gpl, >=1.3.0, which is
    # what the upstream extra pins) is GPL-3.0-or-later, which cannot ship inside
    # a proprietary product. Kokoro covers local synthesis under Apache-2.0
    # model weights and an MIT runtime.
    "services.google.stt",
    "services.google.tts",
    "services.kokoro.tts",
    "services.whisper.stt",
    "audio.vad.silero",
    # Intelligence
    "services.google.llm",
    "services.ollama.llm",
    "processors.aggregators.llm_context",
    "processors.aggregators.llm_response_universal",
)

# Modules kept even though no seed imports them: the observers behind the latency
# metrics EMMA reports on.
EXTRA_REL: tuple[str, ...] = (
    "observers.loggers.metrics_log_observer",
    "observers.user_bot_latency_observer",
    "observers.turn_tracking_observer",
)

# Exact relative module renames. Applied before the prefix rules.
EXACT_REL: dict[str, str] = {
    "": "",  # package root -> emma/__init__.py (hand-written, preserved)
    "frames.frames": "runtime.frames",
    "processors.frame_processor": "runtime.processor",
    "serializers.base_serializer": "transports.serializer",
    "transcriptions.language": "speech.language",
    "services.stt_service": "speech.stt.base",
    "services.stt_latency": "speech.stt.latency",
    "services.tts_service": "speech.tts.base",
    "services.llm_service": "intelligence.llm.base",
    "services.ai_service": "speech.ai_service",
    "services.settings": "speech.settings",
    "services.websocket_service": "speech.websocket_service",
    "services.google.stt": "speech.stt.gemini",
    "services.google.tts": "speech.tts.gemini",
    "services.google.llm": "intelligence.llm.gemini",
    "services.google.frames": "intelligence.llm.gemini_frames",
    "services.google.utils": "intelligence.llm.gemini_utils",
    "services.kokoro.tts": "speech.tts.kokoro",
    "services.whisper.stt": "speech.stt.whisper",
    "services.ollama.llm": "intelligence.llm.ollama",
    "services.openai.llm": "intelligence.llm.openai_compat",
    "services.openai.base_llm": "intelligence.llm.openai_base",
    "services.openai._errors": "intelligence.llm.openai_errors",
    "services.openai._constants": "intelligence.llm.openai_constants",
    "tests.utils": "testing.utils",
}

# Prefix rules; the longest matching prefix wins.
PREFIX_REL: dict[str, str] = {
    "processors.metrics": "observability.frame_metrics",
    "processors.aggregators": "intelligence.aggregators",
    "processors.frameworks.rtvi": "transports.rtvi",
    "processors": "processors",
    "pipeline": "runtime.pipeline",
    "workers": "runtime.workers",
    "clocks": "runtime.clocks",
    "bus": "runtime.bus",
    "registry": "runtime.registry",
    "metrics": "observability.metrics",
    "observers": "observability.observers",
    "audio.vad": "speech.vad",
    "audio.turn": "speech.turn_detection",
    "audio": "audio",
    "turns": "turns",
    "classifiers": "turns.classifiers",
    "adapters.llm": "intelligence.adapters",
    "adapters.tts": "speech.adapters",
    "adapters": "adapters",
    "transports": "transports",
    "serializers": "transports.serializers",
    "transcriptions": "speech",
    "expressive": "speech.expression",
    "services": "providers",
    "utils": "support",
    "tests": "testing",
}

HEADER_FALLBACK = (
    "#",
    "# Copyright (c) 2024-2026, Daily",
    "# Copyright (c) 2026, QuantumLoopAI",
    "#",
    "# SPDX-License-Identifier: BSD 2-Clause License",
    "# Inherited from an upstream BSD 2-Clause licensed voice framework and",
    "# modified for EMMA Receptionist by QuantumLoopAI.",
    "#",
)

MODIFICATION_NOTICE = (
    "# Copyright (c) 2026, QuantumLoopAI",
    "# Modified for EMMA Receptionist by QuantumLoopAI.",
)

# Branding scrub. Applied to vendored text after import rewriting, longest match
# first, so no upstream product name survives into the generated package.
# The upstream product name appears exactly once in this repository, here, and
# only so that the extractor can strip it from generated code. It is not part of
# the runtime, the packaging or the documentation; `nameaudit.py` enforces that.
UPSTREAM_BRAND = "pipecat"

BRAND_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (f"https://github.com/{UPSTREAM_BRAND}-ai/{UPSTREAM_BRAND}", PROJECT_URL),
    (f"http://github.com/{UPSTREAM_BRAND}-ai/{UPSTREAM_BRAND}", PROJECT_URL),
    (f"github.com/{UPSTREAM_BRAND}-ai/{UPSTREAM_BRAND}", PROJECT_URL),
    (f"https://docs.{UPSTREAM_BRAND}.ai", f"{PROJECT_URL}/docs"),
    (f"https://{UPSTREAM_BRAND}.ai", PROJECT_URL),
    (f"{UPSTREAM_BRAND}-ai-prebuilt", TARGET_ROOT),
    (f"{UPSTREAM_BRAND}-ai", TARGET_ROOT),
    (f"{UPSTREAM_BRAND}_ai", TARGET_ROOT),
    ((UPSTREAM_BRAND[0].upper() + UPSTREAM_BRAND[1:]), "EMMA"),
    (UPSTREAM_BRAND.upper(), "EMMA"),
    (UPSTREAM_BRAND, TARGET_ROOT),
)

DATA_SUFFIXES = (".onnx", ".proto", ".wav", ".json", ".txt")
#: Data files that are plain text, so the branding scrub applies to them too.
TEXT_DATA_SUFFIXES = (".proto", ".txt", ".json")
MODULE_STRING = re.compile(r"^([\w]+)((?:\.[\w]+)*)$")

# Hand-written first-party modules. The generator must never overwrite these, so a
# regeneration after an upstream rebase cannot delete QuantumLoopAI's own work.
FIRST_PARTY_PATHS: frozenset[str] = frozenset({"__init__.py", "config.py"})
FIRST_PARTY_DIRS: tuple[str, ...] = ("cost", "security")

PROVENANCE_MARKER = "Modified for EMMA Receptionist by QuantumLoopAI"

MODULES: dict[str, Path] = {}
UPSTREAM: str = ""


def target_module(module: str) -> str | None:
    """Map an upstream absolute module name to its first-party name."""
    if not module or module.split(".")[0] != UPSTREAM:
        return None
    relative = module[len(UPSTREAM) + 1 :]
    if relative in EXACT_REL:
        mapped = EXACT_REL[relative]
        return TARGET_ROOT if not mapped else f"{TARGET_ROOT}.{mapped}"
    best = ""
    for prefix in PREFIX_REL:
        if (relative == prefix or relative.startswith(prefix + ".")) and len(prefix) > len(best):
            best = prefix
    if not best:
        return None
    remainder = relative[len(best) :]
    return f"{TARGET_ROOT}.{PREFIX_REL[best]}{remainder}"


def scrub_branding(text: str) -> str:
    """Remove upstream product naming from vendored text."""
    for old, new in BRAND_REPLACEMENTS:
        text = text.replace(old, new)
    return text


def add_provenance(text: str) -> str:
    """Keep the upstream notice and add the QuantumLoopAI modification notice."""
    lines = text.splitlines()
    if any(PROVENANCE_MARKER in line for line in lines[:20]):
        return text
    insert_at = 0
    while insert_at < len(lines) and lines[insert_at].startswith("#"):
        insert_at += 1
    if insert_at == 0:
        return "\n".join(HEADER_FALLBACK) + "\n" + text
    header = lines[:insert_at]
    if not any("SPDX-License-Identifier" in line for line in header):
        header.append("# SPDX-License-Identifier: BSD 2-Clause License")
    header.extend(MODIFICATION_NOTICE)
    return "\n".join(header + lines[insert_at:])


def _import_rewrites(tree: ast.AST, module: str) -> dict[int, str]:
    """Map 1-indexed line numbers of import statements to their new text."""
    rewrites: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                parts = module.split(".")
                # A top-level test module has no package; relative imports there
                # are already absolute in this repository's test suite.
                if len(parts) < node.level:
                    continue
                base = parts[: len(parts) - node.level + 1]
                absolute = ".".join(base + ([node.module] if node.module else []))
            elif node.module and node.module.split(".")[0] == UPSTREAM:
                absolute = node.module
            else:
                continue
            mapped = target_module(absolute)
            if mapped is None:
                continue
            names = ", ".join(
                alias.name + (f" as {alias.asname}" if alias.asname else "") for alias in node.names
            )
            rewrites[node.lineno] = f"from {mapped} import {names}"
        elif isinstance(node, ast.Import):
            aliases = [a for a in node.names if a.name.split(".")[0] == UPSTREAM]
            if not aliases:
                continue
            parts = []
            for alias in aliases:
                mapped = target_module(alias.name)
                if mapped is None:
                    break
                parts.append(f"import {mapped}" + (f" as {alias.asname}" if alias.asname else ""))
            else:
                rewrites[node.lineno] = "; ".join(parts)
    return rewrites


def _string_rewrites(tree: ast.AST) -> dict[int, list[tuple[str, str]]]:
    """Rewrite string literals that name a whole upstream module."""
    rewrites: dict[int, list[tuple[str, str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if not MODULE_STRING.match(node.value):
            continue
        mapped = target_module(node.value)
        if mapped is None or mapped == node.value:
            continue
        rewrites.setdefault(node.lineno, []).append((node.value, mapped))
    return rewrites


def rewrite_source(text: str, module: str) -> str:
    """Rewrite upstream module references and branding in one source file."""
    tree = ast.parse(text)
    lines = text.splitlines()
    for lineno, replacement in _import_rewrites(tree, module).items():
        lines[lineno - 1] = replacement
    for lineno, pairs in _string_rewrites(tree).items():
        for old, new in pairs:
            lines[lineno - 1] = lines[lineno - 1].replace(f'"{old}"', f'"{new}"').replace(
                f"'{old}'", f"'{new}'"
            )
    text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return scrub_branding(text)


def module_path(mapped: str) -> Path:
    """Destination file for a mapped module name.

    ``mapped`` is an absolute first-party module path, e.g. ``emma.runtime.frames``,
    so the package root is stripped before joining it under ``src/emma``.
    """
    parts = mapped.split(".")
    if parts[0] == TARGET_ROOT:
        parts = parts[1:]
    if not parts:
        return TARGET_DIR / "__init__.py"
    return TARGET_DIR.joinpath(*parts).with_suffix(".py")


def is_first_party(dest: Path) -> bool:
    """Whether ``dest`` is hand-written QuantumLoopAI code the generator must not touch."""
    relative = dest.relative_to(TARGET_DIR)
    if len(relative.parts) == 1 and relative.name in FIRST_PARTY_PATHS:
        return True
    return bool(relative.parts) and relative.parts[0] in FIRST_PARTY_DIRS


def ensure_packages(dest: Path) -> None:
    """Create ``__init__.py`` for every package directory above ``dest``."""
    package_parts = dest.relative_to(TARGET_DIR).parts[:-1]
    for depth in range(1, len(package_parts) + 1):
        init = TARGET_DIR.joinpath(*package_parts[:depth], "__init__.py")
        if not init.exists():
            init.parent.mkdir(parents=True, exist_ok=True)
            init.write_text("\n".join(HEADER_FALLBACK) + "\n", encoding="utf-8")


def vendor_modules(kept: set[str], write: bool) -> tuple[int, int, list[Path]]:
    total_modules = total_lines = 0
    written: set[Path] = set()
    skipped: list[Path] = []
    for module in sorted(kept):
        path = MODULES.get(module)
        if path is None:
            continue
        mapped = target_module(module)
        if mapped is None:
            raise SystemExit(f"no mapping rule for {module}")
        dest = module_path(mapped)
        if is_first_party(dest):
            skipped.append(dest)
            written.add(dest)
            continue
        original = path.read_text(encoding="utf-8")
        text = add_provenance(rewrite_source(original, module))
        written.add(dest)
        if write:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
            ensure_packages(dest)
            written |= set(TARGET_DIR.rglob("__init__.py"))
        total_modules += 1
        total_lines += len(text.splitlines())
    if skipped:
        print(
            "preserved first-party: "
            + ", ".join(str(p.relative_to(TARGET_DIR)) for p in skipped)
        )
    return total_modules, total_lines, sorted(written)


def vendor_data(kept: set[str], write: bool) -> list[str]:
    copied: list[str] = []
    for module in sorted(kept):
        path = MODULES.get(module)
        mapped = target_module(module)
        if path is None or mapped is None:
            continue
        for asset in path.parent.iterdir():
            if not asset.is_file() or asset.suffix not in DATA_SUFFIXES:
                continue
            dest = module_path(mapped).parent / asset.name
            if write:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if asset.suffix in TEXT_DATA_SUFFIXES:
                    # Schemas and notes are readable text and carry branding too.
                    dest.write_text(
                        scrub_branding(asset.read_text(encoding="utf-8")), encoding="utf-8"
                    )
                else:
                    dest.write_bytes(asset.read_bytes())
            copied.append(str(dest.relative_to(REPO_ROOT)))
    return copied


def referenced_modules(tree: ast.AST) -> set[str]:
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module.split(".")[0] == UPSTREAM:
                referenced.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == UPSTREAM:
                    referenced.add(alias.name)
    return referenced


def vendor_tests(kept: set[str], write: bool) -> list[str]:
    """Port upstream tests whose every imported module survived the extraction.

    Ported tests land in ``tests/emma`` as new files; the upstream suite is left
    untouched so the migration stays reviewable and reversible.
    """
    tests_dir = REPO_ROOT / "tests"
    out_dir = tests_dir / "emma"
    ported: list[str] = []
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(tests_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if UPSTREAM not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        referenced = referenced_modules(tree)
        if not referenced:
            continue
        if not all(any(name == m or m.startswith(name + ".") for m in kept) for name in referenced):
            continue
        if write:
            (out_dir / path.name).write_text(
                add_provenance(rewrite_source(text, path.stem)), encoding="utf-8"
            )
        ported.append(path.name)
    return ported


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report without writing")
    parser.add_argument("--tests-only", action="store_true", help="only port tests")
    parser.add_argument("--upstream", help="override the upstream package directory name")
    args = parser.parse_args()
    write = not args.check

    global MODULES, UPSTREAM
    UPSTREAM = upstream_root(args.upstream)
    MODULES = discover_modules(UPSTREAM)
    graph = build_graph(MODULES, UPSTREAM)

    seeds = [f"{UPSTREAM}.{rel}" if rel else UPSTREAM for rel in SEEDS_REL]
    extras = [f"{UPSTREAM}.{rel}" for rel in EXTRA_REL]
    unknown = [s for s in seeds + extras if s not in MODULES]
    if unknown:
        raise SystemExit(f"unknown seed modules: {unknown}")

    kept = set(closure(graph, seeds + extras))
    kept.add(UPSTREAM)

    if write and not args.tests_only:
        TARGET_DIR.mkdir(parents=True, exist_ok=True)

    if not args.tests_only:
        modules, lines, _written = vendor_modules(kept, write)
        data = vendor_data(kept, write)
        if write:
            (TARGET_DIR / "py.typed").write_text("", encoding="utf-8")
        print(f"vendored modules : {modules}")
        print(f"vendored lines   : {lines}")
        print(f"data files       : {len(data)}")

    tests = vendor_tests(kept, write)
    print(f"ported tests     : {len(tests)}")
    if args.check:
        print("(check mode: nothing written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
