# SIM-314 — Resolution of the SIM-200 / SIM-201 ID collision

*Author: Product Manager (Agent 1) · Sprint 2026-06-17 (Phase 4 P0 gates) · executed 2026-05-22*

## The problem

`SIM-201` was overloaded onto two unrelated pieces of work, which corrupts the
dependency graph (a ticket cannot be both a P0 critical-path spec and a Held
Phase-4 placeholder):

1. **"Manager decision logic spec"** — a P0/critical-path design+module ticket
   (pull/pinch-hit/bullpen-by-leverage/bunt from manager-similarity tendencies),
   referenced as `SIM-201` throughout `BACKLOG.md` and the Phase 2/3 sprint
   banners (e.g. "Next: SIM-220 (backtesting), SIM-201 (manager logic)").
2. **"Step 3b — Catcher Blocking Coefficient on Dirt-Pitch Outcomes"** — a Small,
   `Held`, forward-looking Phase-4 *placeholder* tracked on the **Phase 4 Gate**
   sheet of `backlog.xlsx` (paired with `SIM-200`, the catcher *framing*
   placeholder).

Two tickets, one ID.

## The decision

The Phase-3-close program audit already re-homed the manager-logic scope to a new
ID, **`SIM-323`** ("Manager decision-logic spec + module … the real SIM-201
manager scope", Tier P1, depends on SIM-312). We formalize that here:

| ID | Canonical meaning (after this resolution) | Status |
|----|--------------------------------------------|--------|
| **SIM-200** | Step 3b — *catcher framing* bias on shadow-zone takes (Phase 4 placeholder) | Held |
| **SIM-201** | Step 3b — *catcher blocking* coefficient on dirt-pitch outcomes (Phase 4 placeholder) | Held |
| **SIM-323** | Manager decision-logic spec + module (pull / pinch-hit / bullpen-by-leverage / bunt) | Open (P1) |

Rationale: `SIM-200`/`SIM-201` were authored as a *paired* catcher framing +
blocking placeholder set on the Phase 4 Gate sheet and are referenced as a pair
("SIM-200, SIM-201") in the README Step-3b notes. Renumbering *those* would ripple
into the Phase 4 Gate sheet and the README. The manager-logic usage, by contrast,
only appears in prose "Next:" banners and one Phase-4 Gate-adjacent row, and the
audit already minted `SIM-323` for it. So the lower-churn, audit-consistent
resolution is: **keep SIM-200/201 as the catcher placeholders; manager logic is
SIM-323.**

## Application (where references change)

The following `BACKLOG.md` references to "SIM-201 (manager logic / manager
decision logic spec)" are repointed to **SIM-323** as part of this sprint's
`BACKLOG.md` rewrite (see `docs/SPRINT_2026-06-17_phase4_p0_gates.md`):

- The Phase-2/Phase-3 sprint "Next:" banners (historical prose — repointed for
  forward correctness; the historical record of what was said at the time is
  preserved in `CHANGES.md`).
- The standalone manager-logic backlog row (was labeled `SIM-201`) → `SIM-323`.

The catcher-placeholder references ("SIM-200, SIM-201 … Step 3b", README Step-3b
TODOs) are left unchanged — they are correct.

## Downstream

- `SIM-349` (situational-decision module: IBB / sac bunt / sac fly / hit-and-run)
  depends on `SIM-323`, not on the catcher placeholders — dependency graph now
  unambiguous.
- README §"Step 3b" still needs framing/blocking wording (tracked under SIM-341
  README reconciliation); not in scope here.

*No code change. `backlog.xlsx` is regenerated from `BACKLOG.md` (see sprint log).*
