#!/usr/bin/env python3
"""Static module-graph analysis for the upstream package tree.

Used to compute the transitive import closure of the modules EMMA keeps, so that
vendoring is decided by evidence (the import graph) rather than by guesswork.

The upstream package is *discovered*, not named: it is the one package directory
under ``src/`` that is not ``emma``. That keeps this tool free of upstream
branding and lets it be re-pointed at a different upstream with a single flag.

Usage::

    python scripts/emma/module_graph.py --closure pipeline.worker
    python scripts/emma/module_graph.py --reverse services.google.llm
    python scripts/emma/module_graph.py --report
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict, deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
TARGET_PACKAGE = "emma"


def upstream_root(explicit: str | None = None) -> str:
    """Return the name of the upstream package directory under ``src/``.

    Args:
        explicit: Override; use when ``src/`` holds more than one candidate.

    Returns:
        The upstream import root name.

    Raises:
        SystemExit: If the candidate is ambiguous or missing.
    """
    if explicit:
        if not (SRC / explicit / "__init__.py").is_file():
            raise SystemExit(f"src/{explicit}/ is not a package")
        return explicit
    candidates = sorted(
        path.name
        for path in SRC.iterdir()
        if path.is_dir()
        and (path / "__init__.py").is_file()
        and path.name != TARGET_PACKAGE
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SystemExit(f"no upstream package found under {SRC}")
    raise SystemExit(f"ambiguous upstream package under {SRC}: {candidates}; pass --upstream")


def discover_modules(package: str) -> dict[str, Path]:
    """Map every importable module name in ``package`` to its file."""
    modules: dict[str, Path] = {}
    for path in (SRC / package).rglob("*.py"):
        rel = path.relative_to(SRC).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            modules[".".join(parts)] = path
    return modules


def _classify(node: ast.AST, module: str, modules: dict[str, Path], package: str) -> list[str]:
    """Return the in-package modules imported by a single AST node."""
    found: list[str] = []
    if isinstance(node, ast.ImportFrom):
        if node.level:
            parts = module.split(".")
            base = parts[: len(parts) - node.level + 1]
            target = ".".join(base + ([node.module] if node.module else []))
        elif node.module and node.module.split(".")[0] == package:
            target = node.module
        else:
            return found
        for cand in (target, target.rsplit(".", 1)[0] if "." in target else ""):
            if cand in modules:
                found.append(cand)
                break
    elif isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.split(".")[0] != package:
                continue
            for cand in (alias.name, alias.name.rsplit(".", 1)[0]):
                if cand in modules:
                    found.append(cand)
                    break
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        # Dynamic imports: importlib.import_module("<package>.x") and friends.
        if node.value.startswith(package + ".") and node.value in modules:
            found.append(node.value)
    return found


def build_graph(modules: dict[str, Path], package: str) -> dict[str, set[str]]:
    """Build the intra-package import graph."""
    graph: dict[str, set[str]] = defaultdict(set)
    for module, path in modules.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - defensive
            print(f"warning: cannot parse {path}: {exc}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            graph[module].update(_classify(node, module, modules, package))
    return graph


def closure(graph: dict[str, set[str]], seeds: list[str]) -> dict[str, int]:
    """Transitive import closure of ``seeds``; values are graph depths."""
    depth: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque((seed, 0) for seed in seeds)
    while queue:
        module, level = queue.popleft()
        if module in depth:
            continue
        depth[module] = level
        for dep in graph.get(module, ()):
            if dep not in depth:
                queue.append((dep, level + 1))
    return depth


def dependents(graph: dict[str, set[str]], module: str) -> list[str]:
    """Modules that import ``module``."""
    return sorted(m for m, deps in graph.items() if module in deps)


def loc(path: Path) -> int:
    """Line count of one file."""
    return len(path.read_text(encoding="utf-8").splitlines())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--closure", nargs="+", metavar="MODULE", help="relative to upstream root")
    parser.add_argument("--reverse", metavar="MODULE", help="relative to upstream root")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument("--upstream", help="override the upstream package directory name")
    args = parser.parse_args()

    package = upstream_root(args.upstream)
    modules = discover_modules(package)
    graph = build_graph(modules, package)

    if args.reverse:
        target = args.reverse if args.reverse.startswith(package) else f"{package}.{args.reverse}"
        print(json.dumps(dependents(graph, target), indent=2))
        return 0

    if args.closure:
        seeds = [s if s.startswith(package) else f"{package}.{s}" for s in args.closure]
        kept = closure(graph, seeds)
        missing = [s for s in seeds if s not in modules]
        total_loc = sum(loc(p) for p in modules.values())
        kept_loc = sum(loc(modules[m]) for m in kept if m in modules)
        groups: dict[str, int] = defaultdict(int)
        for module in kept:
            parts = module.split(".")
            groups[parts[1] if len(parts) > 1 else module] += 1
        if args.json:
            print(
                json.dumps(
                    {
                        "modules_total": len(modules),
                        "modules_kept": len(kept),
                        "loc_total": total_loc,
                        "loc_kept": kept_loc,
                        "missing_seeds": missing,
                        "groups": dict(groups),
                        "modules": sorted(kept),
                    },
                    indent=2,
                )
            )
        else:
            print(f"in-package modules : {len(modules)}  ({total_loc} LOC)")
            print(f"closure            : {len(kept)}  ({kept_loc} LOC)")
            if missing:
                print(f"unknown seeds      : {missing}")
            print("by subpackage      :")
            for name, count in sorted(groups.items(), key=lambda kv: -kv[1]):
                print(f"  {name:<16} {count}")
        return 0

    if args.report:
        rows = []
        for module, path in sorted(modules.items()):
            rows.append(
                {
                    "module": module,
                    "loc": loc(path),
                    "imports": sorted(graph.get(module, ())),
                    "imported_by": len(dependents(graph, module)),
                }
            )
        print(json.dumps(rows, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
