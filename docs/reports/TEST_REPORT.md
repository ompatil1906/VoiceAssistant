# Test Execution Report

Every entry states what was actually run and what the result was. A capability
that was not exercised is marked **NOT TESTED** or **BLOCKED** — never assumed.

Environment: macOS, Python 3.13.5, no virtualenv, no third-party packages
installed (`pydantic`, `loguru`, `numpy`, `pytest` and the upstream runtime are all absent
from the interpreter used for these runs).

---

## Summary

| Area | Status |
| --- | --- |
| First-party unit tests (cost, budget, data policy, redaction, config) | **VERIFIED** — 39 passed, 0 failed |
| Name audit (`nameaudit.py`) | **VERIFIED** — 0 product-surface hits |
| Extraction analysis (`module_graph.py`) | **VERIFIED** |
| Extraction generator (`vendor.py --check`) | **VERIFIED** (plan only; see BLOCKED below) |
| Generated package import | **BLOCKED** — generator execution refused by the environment's permission reviewer |
| Ported framework tests | **BLOCKED** — depend on the generated package |
| Upstream `pytest` suite baseline | **BLOCKED** — no virtualenv, dependencies not installed |
| Browser / LiveKit / microphone session | **NOT TESTED** — requires a running server and a browser |
| Latency benchmarks | **NOT TESTED** — harness not yet written; nothing to measure without the package |
| EMMA application regression suite | **BLOCKED** — the application is not in this repository |

---

## 1. First-party unit tests — VERIFIED

39 tests across five modules, executed with
`PYTHONPATH=src python3 scripts/emma/selfcheck.py` (a dependency-free runner, used
because `pytest` is not installed here; the same test functions run under
`pytest` once a virtualenv exists).

| Test module | Tests | Covers |
| --- | --- | --- |
| `tests/emma/test_cost_policy.py` | 6 | unknown provider ⇒ billable; paid denied by default; trial needs opt-in; local-only policy denies every remote provider |
| `tests/emma/test_budget.py` | 7 | request/token ceilings raise; zero ceiling denies the first call; $0 spend default; spend requires a known unit price; unlimited limits never raise |
| `tests/emma/test_data_policy.py` | 5 | synthetic mode refuses providers that train on inputs; local providers allowed; approved mode allows a signed-off provider; unknown provider treated as permissive |
| `tests/emma/test_redaction.py` | 8 | NHS numbers, UK phones (mobile, landline, +44), dates of birth (both formats), postcodes, emails; ordinary clinical text untouched; recursive mapping redaction; bounded summaries |
| `tests/emma/test_config.py` | 13 | defaults are local + synthetic; `$0` ceiling; free profile refused in synthetic mode; missing Gemini key is a config error; provider outside profile refused; paid provider refused even when selected; `describe()` never contains secret values; transport auto-selection |

Notable behaviour confirmed by these tests, because it is the core of the
zero-cost guarantee:

- A provider that is not in the catalogue is classified `PAID`, so a typo or a
  new vendor cannot silently become free.
- `EMMA_ALLOW_PAID=false` rejects a billable provider even when the deployment
  names it explicitly.
- `describe()` reports which credentials exist without rendering their values.

## 2. Extraction analysis — VERIFIED

`scripts/emma/module_graph.py` was run against the upstream tree:

```
in-package modules : 758  (224,628 LOC)
closure            : 180  (66,598 LOC)   ← 29.6 % of upstream
```

The closure is computed from an AST walk that also follows module-path string
literals, so dynamic imports are not missed. Seeds and mapping tables are in
`scripts/emma/vendor.py`.

## 3. Extraction generator — VERIFIED (plan), BLOCKED (execution)

`python3 scripts/emma/vendor.py --check` reports:

```
vendored modules : 181
vendored lines   : 67232
data files       : 39
ported tests     : 142
```

A dry inspection of the target paths confirms 181 destination files, **0 of which
already exist** (nothing would be overwritten), all writes confined to
`src/emma/` and `tests/emma/`.

Executing the generator (without `--check`) was **refused by this environment's
permission reviewer**, which treats a script that writes many files as an
unassessable effect. The migration therefore cannot proceed past this point
without an explicit approval. See "Blocker" below.

## 4. Name audit — VERIFIED

`python3 scripts/emma/nameaudit.py` scans every tracked and untracked file and
classifies each hit of the upstream product name:

```
PRODUCT SURFACE — must be fixed              0 files
allowed (licence record / scrub tool)        3 files   LICENSE, NOTICE, vendor.py
upstream tree, deleted after extraction      src/  (pending)
upstream-only file, pending deletion         the rest
files scanned with hits: 1656  |  violations: 0
```

Exit status is non-zero on any product-surface hit, so the rule is enforced rather
than remembered. The only permitted occurrences are the upstream BSD notice in
`LICENSE`/`NOTICE` (a licence requirement) and the single constant that drives the
branding scrub in the extraction tool.

## 5. Blocked and untested, stated plainly

**Blocker — generator execution.** The 181-module `emma` package does not exist
yet. Consequently:

- `import emma` works only for the hand-written `emma/__init__.py`, `emma/config.py`,
  `emma/cost/` and `emma/security/`; the engine (`emma.runtime`), speech,
  intelligence and transport modules are not generated.
- The 142 ported framework tests cannot be collected.
- No pipeline can be constructed, so no latency, barge-in or recovery test can
  run.

**Blocked — upstream pytest baseline.** Establishing the pre-existing pass/fail
counts requires `uv sync`, which needs network access to 98 extras. This
environment has no virtualenv and no installed dependencies; `uv sync` was not
attempted.

**Not tested — anything acoustic.** Browser microphone capture, echo cancellation,
real LiveKit media flow, and interruption timing under real audio all require a
running LiveKit server and a browser client. Nothing here claims they work.

**Not tested — performance.** No benchmark has been executed, so no number is
reported in `docs/performance/`. Structural observations in
`docs/architecture/INITIAL_AUDIT.md` §8 are explicitly labelled as such.

## 6. What would move each item forward

| Item | Action |
| --- | --- |
| Generated package + ported tests | Approve running `python3 scripts/emma/vendor.py` |
| Upstream baseline | `uv sync --group dev --all-extras --no-extra gstreamer --no-extra local && uv run pytest` |
| Browser and LiveKit validation | start a LiveKit server, run the EMMA backend, join from a browser |
| Latency numbers | write and run `scripts/bench/bench_latency.py` with mocked providers |
| EMMA regression suite | make the EMMA application code available to this workspace |
