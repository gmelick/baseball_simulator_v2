# SIM-315 — OneDrive truncation remediation plan (DEFERRED)

*Author: QA / DevOps (Agent 9) · Sprint 2026-06-17 (Phase 4 P0 gates) · executed 2026-05-22*
*Status: **RESOLVED** — Option A (repo moved off OneDrive, now at `C:\Users\grego\Documents`) + Option B (`scripts/check_file_integrity.py` + `.pre-commit-config.yaml`) both landed.*

## Why this is a P0 gate

The repository lives under OneDrive
(`C:\Users\grego\OneDrive\Documents\PycharmProjects\baseball_simulator_v2`). Two
failure modes have hit **every recent sprint** and will get worse in Phase 4,
which adds several large new modules:

1. **Large-file truncation on sync.** Editing files larger than ~1,500 lines via
   the file tools delivers a *truncated* copy to the sandbox mount that the test
   runner reads (a clean prefix, the tail cut mid-statement), and sometimes
   injects **null bytes** — while the authoritative Windows file is intact. The
   two stores (file tools ↔ bash mount) behave as separate stores that do not
   reliably propagate large writes. This sprint alone, `pitcher_similarity.py`
   (~1,900 lines), `play_pool_sampler.py`, and several test files truncated mid-
   write and had to be hand-repaired before the suite could run.
2. **`git` is unusable from the working tree.** `.git/config` returns
   "Invalid argument", so version control, `git blame`, and any pre-commit hook
   cannot run from the OneDrive-backed checkout.

The net risk: a truncated module can silently ship (the authoritative file is
fine, but CI/tests running against a synced copy may pass or fail on the wrong
bytes), and there is no VCS safety net.

## Options considered

### Option A — Move the repo off OneDrive (recommended, but manual)
Relocate the working tree to a non-synced local path (e.g.
`C:\dev\baseball_simulator_v2`) and, if cloud backup is still wanted, exclude it
from OneDrive or use a `.git`-aware backup. This eliminates **both** failure
modes at the source: no sync layer to truncate large writes, and `git` works
again.

- **Pros:** root-cause fix; restores git; removes the per-large-file repair tax
  that is costing real time every sprint.
- **Cons:** must be done by Greg on the Windows host (this agent environment
  cannot relocate the user's OneDrive folder or run `git`); requires re-pointing
  the IDE/run configs and re-validating the toolchain paths.
- **Effort:** ~1–2 hours, manual, one-time.

### Option B — In-repo file-integrity guard (programmatic, partial)
Add a guard that catches truncation/corruption before it propagates:

- A check script `scripts/check_file_integrity.py` that, for every tracked
  `*.py`, verifies (a) `ast.parse()` succeeds (catches truncation mid-statement)
  and (b) no `\x00` bytes are present (catches null-byte injection); optionally a
  byte-count / line-count manifest diff for the known large files.
- Wire it as a **pre-commit hook** *and* a CI job (the CI job is what actually
  runs, since pre-commit needs working git locally).
- A `make verify-integrity` target for manual runs in the sandbox.

- **Pros:** implementable in-repo; gives an automated tripwire even before the
  repo moves; the `ast.parse` + null-byte scan is exactly the manual check the
  team already does by hand.
- **Cons:** a *detector*, not a *fix* — it tells you a file truncated, it doesn't
  stop OneDrive from truncating it; pre-commit half is inert until git works
  (i.e. until Option A or a git relocation lands). Does not restore git on its
  own.

## Recommendation

Do **both, in order**: ship **Option B** (the integrity guard + CI job) first as
the automated tripwire, then have Greg perform **Option A** (the physical move)
as the root-cause fix, after which the pre-commit half of B becomes live.

## Why deferred this sprint

Option A is a manual host action only Greg can take, and Option B is code that
should land with its own tests and CI wiring — neither fits the "P0 gates,
document-only" decision for this sprint. SIM-315 therefore stays **Open** with
this plan attached. In the meantime, the documented mitigations remain in force:
prefer surgical `Edit`s over rewrites on large files; after editing a large file,
`py_compile` the mount copy and repair it (`head -n <last-good>` + append the
authoritative tail; `tr -d '\000'` for null bytes); serialize tickets that touch
the same large file.

## Suggested acceptance criteria when SIM-315 is actioned

1. `scripts/check_file_integrity.py` exists: `ast.parse` + null-byte scan over all
   `*.py`, non-zero exit on any failure, with a large-file line-count manifest.
2. CI job runs it on every push/PR (unified Python 3.11; ties into SIM-343).
3. Pre-commit hook config added (`.pre-commit-config.yaml`).
4. Decision recorded on whether the working tree was relocated off OneDrive
   (Option A) — and if so, run configs / docs updated to the new path.
