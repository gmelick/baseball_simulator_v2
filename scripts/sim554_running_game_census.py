"""
scripts/sim554_running_game_census.py — SIM-554: where the simulator's steals and pickoffs land.

WHAT IT DOES
============
The running game on the pitch (SIM-554) changes the ORDER of the running-game
draws, not their weights. Today (arm 0, ``SIM_STEAL_PITCH_CLASS=0``) one draw
before the pitch reads the count and answers both "picked off?" and "does he
go?"; the attempt then lands on whatever pitch comes out, a ball in play
included. The new order (arm 1, ``=1``): the pickoff draw before the pitch,
then the pitch, then the steal draw among real pitches of the same count and
the same class. The design is
``docs/audit/2026-09-29-sim554-running-game-on-the-pitch-plan.md`` §5.9 and
§8 step 5.

One process runs ONE arm on the balanced 45 games
(``scripts/sim523_game_set.json``), seeds 0..iters-1 for every game. It wraps
each machine's ``step_pitch`` (the ``scripts/trace_game.py`` pattern: the
state before the step, the result, the state after) and reads, per arm:

  * the steal opportunities and attempts by pitch class and target base (an
    opportunity is a thrown pitch with a stealable lead runner at the start
    of the step), the attempts per opportunity, the safe share, and the
    stolen bases and caught stealings per team-game from the box;
  * the pickoff outcomes per game and by the count they were drawn at, and
    their kind (a plain out, an out while advancing, an errant throw) read at
    the sampler seam: ``pickoff_draw``'s tuple on arm 1, ``steal_draw``'s
    five-tuple on arm 0;
  * the no-pitch third outs (a pickoff before the pitch for the third out):
    the half-inning rolled, and the same batter leads off his side's next
    half at 0-0. On arm 0, the pickoff third outs credited as a plate
    appearance (the old defect);
  * the steals voided by reason, the dropped-third-strike reaches and the
    runners they moved, the loop's own ``RunningGameTally`` summed over the
    machines, and the seconds per game-sim (the census's own reads add a
    little to them).

THE EXPECTED-RATE READ (the noise fix). At 45 x 20 the pickoff count is too
small to read a 28% rise (about +-0.15 on the ratio). So at every pickoff draw
(arm 1: ``pickoff_draw``; arm 0: the pre-pitch ``steal_draw``) the census
asks the sampler's rng-free ``steal_weights`` for the same group and adds the
weight share of the rows with a pickoff outcome to an expected total. The
ratio of the two arms' expected pickoffs per draw is then near noiseless. The
same read gives the expected attempt share per class at every steal draw. The
wrappers on the cached sampler carry a wrap-once mark (the SIM-514 stacking
trap); they consume no random number, so the arm's games are the games it
plays unwrapped.

``report OFF.json ON.json`` prints the reads side by side, the pool's own
class shares (from the artifact pool, pitch rows only) and the run book's
stop rules:

  * a misplaced share (foul + ball in play + hit by pitch) of the new arm's
    attempts above 5%;
  * the expected pickoffs per draw, new arm over old, outside 1.15-1.40 (the
    pool's own ratio is 1,788 / 1,394 = 1.28);
  * the attempts per opportunity moved by more than 2 standard errors, per
    target (UNPAIRED errors: the two arms consume random numbers in another
    order, so the same seed is not the same game);
  * a steal voided as "third_out_first" on the new arm (the class group holds
    no attempt there).

RUN
===
Run docker compose from the MAIN checkout (a compose run from the worktree is
a separate project with empty volumes), with the worktree's code mounted over
the image's. One arm per process; the two arms may run side by side. About 15
to 17 minutes per arm at 45 x 20, plus the bundle load and the game states.
In Git Bash:

    W=/c/Users/grego/Documents/baseball_simulator_v2/.claude/worktrees/sim554
    P="MSYS_NO_PATHCONV=1 docker compose run --rm -T -v $W/scripts:/app/scripts \\
       -v $W/simulation:/app/simulation -v $W/pipeline:/app/pipeline -v $W/api:/app/api \\
       -v $W/similarity:/app/similarity -v $W/db:/app/db app"
    $P python scripts/sim554_running_game_census.py run --arm 0 --iters 20 \\
        --json-out /app/scripts/sim554_census_off.json &
    $P python scripts/sim554_running_game_census.py run --arm 1 --iters 20 \\
        --json-out /app/scripts/sim554_census_on.json &
    wait
    $P python scripts/sim554_running_game_census.py report scripts/sim554_census_off.json \\
        scripts/sim554_census_on.json --out /app/scripts/sim554_census_report.txt

The census reads DuckDB read-only. Do not run it while the steal-pool rebuild
holds the writer lock.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

from simulation.game_state import NO_PITCH, PITCH_OUTCOMES  # noqa: E402

#: The six pitch classes, in the order of the steal pool's class codes (1-6).
CLASSES: tuple[str, ...] = tuple(PITCH_OUTCOMES)
#: The classes on which real runners (almost) never ride an attempt: 1% of
#: real attempts ride a foul, none a ball in play or a hit by pitch (§2.1).
MISPLACED: tuple[str, ...] = ("foul", "in_play", "hit_by_pitch")
TARGETS: tuple[str, ...] = ("2", "3")
#: The run book's stop rules (design §8 step 5).
MISPLACED_STOP = 0.05
PICKOFF_RATIO_WINDOW: tuple[float, float] = (1.15, 1.40)
POOL_PICKOFF_RATIO = 1788 / 1394
VOLUME_SE_STOP = 2.0
#: The real shares of attempts by class, 2023-2026 (design §2.1), for the report.
REAL_ATTEMPT_SHARE: dict[str, float] = {
    "ball": 0.644,
    "called_strike": 0.246,
    "swinging_strike": 0.100,
    "foul": 0.0105,
    "in_play": 0.0,
    "hit_by_pitch": 0.0,
}

_FACTORY = "simulation.production_factory:production_machine_factory"
_GAME_SET = _ROOT / "scripts" / "sim523_game_set.json"
_MARK = "_sim554_census"


# ---------------------------------------------------------------------------
# One step
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Snap:
    """The state the census reads before and after one step."""

    first: int | None
    second: int | None
    third: int | None
    outs: int
    balls: int
    strikes: int
    inning: int
    half: str
    batter: int | None


def snap_of(state: Any, batter: int | None) -> Snap:
    """Read a game state at once: ``step_pitch`` mutates it in place."""
    b = state.bases
    half = getattr(state, "half", None)
    return Snap(
        first=b.first,
        second=b.second,
        third=b.third,
        outs=int(state.outs),
        balls=int(state.balls),
        strikes=int(state.strikes),
        inning=int(state.inning),
        half=str(getattr(half, "name", half)),
        batter=batter,
    )


def lead_stealable(s: Snap) -> tuple[int, int, int] | None:
    """The lead stealable runner -> (runner id, his base, the target base):
    on first with second open (target 2), or on second with third open
    (target 3, first occupied or not). The loop's own rule."""
    if s.first is not None and s.second is None:
        return s.first, 1, 2
    if s.second is not None and s.third is None:
        return s.second, 2, 3
    return None


def _zero_by_class() -> dict[str, int]:
    return dict.fromkeys(CLASSES, 0)


class Recorder:
    """Everything one arm counts; serialized to JSON at the end."""

    def __init__(self) -> None:
        self.opp: dict[str, dict[str, int]] = {t: _zero_by_class() for t in TARGETS}
        self.att: dict[str, dict[str, int]] = {t: _zero_by_class() for t in TARGETS}
        self.safe: dict[str, int] = dict.fromkeys(TARGETS, 0)
        self.ibb = 0
        self.pickoffs_by_count: dict[str, int] = {}
        self.pickoff_steps: dict[str, int] = {"out": 0, "error": 0}
        self.seam_kind: dict[str, int] = {"out": 0, "out_advancing": 0, "error": 0}
        self.pickoff_third_outs = 0
        self.pickoff_third_outs_pa_credited = 0
        self.no_pitch: dict[str, int] = {"n": 0, "half_rolled": 0, "labelled": 0}
        self.leadoff: dict[str, int] = {"same": 0, "other": 0, "unchecked": 0}
        self.voided: dict[str, int] = {}
        self.d3k: dict[str, int] = {
            "reaches": 0,
            "runners_on": 0,
            "runners_moved": 0,
            "unforced_chances": 0,
            "unforced_moved": 0,
            "with_running_play": 0,
        }
        #: Per target: [the sum of the expected pickoff shares, the draws read].
        self.exp_pickoff: dict[str, list[float]] = {t: [0.0, 0] for t in TARGETS}
        #: Per target and class of the pitch: the sum of the expected attempt shares.
        self.exp_attempt: dict[str, dict[str, float]] = {
            t: dict.fromkeys(CLASSES, 0.0) for t in TARGETS
        }
        self.tally: dict[str, Any] = {}
        self.per_sim: list[dict[str, Any]] = []
        self._staged_attempt: tuple[int, float] | None = None
        #: The side whose half a no-pitch third out ended -> the batter due.
        self._expect: dict[str, int | None] = {}
        self._sim = {"pickoffs": 0, "opp2": 0, "att2": 0, "opp3": 0, "att3": 0}

    # ---- the game-sim frame ------------------------------------------------
    def start_game_sim(self) -> None:
        self._sim = {"pickoffs": 0, "opp2": 0, "att2": 0, "opp3": 0, "att3": 0}
        self._expect.clear()
        self._staged_attempt = None

    def end_game_sim(self, box_lines: Iterable[Any], seconds: float) -> None:
        lines = list(box_lines)
        self.per_sim.append(
            {
                "sb": int(sum(int(getattr(x, "sb", 0) or 0) for x in lines)),
                "cs": int(sum(int(getattr(x, "cs", 0) or 0) for x in lines)),
                "pickoffs": self._sim["pickoffs"],
                "opp2": self._sim["opp2"],
                "att2": self._sim["att2"],
                "opp3": self._sim["opp3"],
                "att3": self._sim["att3"],
                "seconds": float(seconds),
            }
        )
        # A side whose half ended on a no-pitch third out and never batted again.
        self.leadoff["unchecked"] += len(self._expect)
        self._expect.clear()

    # ---- the sampler seam --------------------------------------------------
    def stage_expected_attempt(self, target: int, p: float) -> None:
        """A steal draw's expected attempt share; the step's pitch class takes it."""
        self._staged_attempt = (int(target), float(p))

    def add_expected_pickoff(self, target: int, p: float) -> None:
        cell = self.exp_pickoff[str(int(target))]
        cell[0] += float(p)
        cell[1] += 1

    def add_seam_kind(self, out: bool, advancing: bool, error: bool) -> None:
        if out:
            self.seam_kind["out_advancing" if advancing else "out"] += 1
        elif error:
            self.seam_kind["error"] += 1

    def add_tally(self, tally: Any) -> None:
        """Sum one machine's ``RunningGameTally`` (it accumulates over its games)."""
        t = self.tally
        for name in (
            "pickoff_draws",
            "pickoff_outs",
            "pickoff_errors",
            "no_pitch_third_outs",
            "d3k_reaches",
            "d3k_runners_moved",
        ):
            t[name] = int(t.get(name, 0)) + int(getattr(tally, name, 0) or 0)
        by_count = t.setdefault("pickoffs_by_count", {})
        for (b, s), n in dict(getattr(tally, "pickoffs_by_count", {}) or {}).items():
            key = f"{int(b)}-{int(s)}"
            by_count[key] = int(by_count.get(key, 0)) + int(n)
        voided = t.setdefault("voided", {})
        for reason, n in dict(getattr(tally, "voided", {}) or {}).items():
            voided[str(reason)] = int(voided.get(str(reason), 0)) + int(n)

    # ---- the step ----------------------------------------------------------
    def begin_step(self) -> None:
        self._staged_attempt = None

    def observe(self, pre: Snap, res: Any, post: Snap) -> None:
        """Count one step: ``pre`` and ``post`` are the states around it."""
        staged, self._staged_attempt = self._staged_attempt, None
        # The side due to lead off after a no-pitch third out: its first step.
        if pre.half in self._expect:
            due = self._expect.pop(pre.half)
            same = pre.batter == due and pre.balls == 0 and pre.strikes == 0
            self.leadoff["same" if same else "other"] += 1
        event = getattr(res, "event", None)
        if event == "intentional_walk":
            self.ibb += 1
            return
        po_out = bool(getattr(res, "pickoff_out", False))
        po_err = bool(getattr(res, "pickoff_error", False))
        if po_out or po_err:
            key = f"{pre.balls}-{pre.strikes}"
            self.pickoffs_by_count[key] = self.pickoffs_by_count.get(key, 0) + 1
            self.pickoff_steps["out" if po_out else "error"] += 1
            self._sim["pickoffs"] += 1
            if po_out and pre.outs == 2:
                self.pickoff_third_outs += 1
                if getattr(res, "pa_terminal", False) and event is not None:
                    self.pickoff_third_outs_pa_credited += 1
        if res.pitch_outcome == NO_PITCH:
            self.no_pitch["n"] += 1
            if (post.inning, post.half) != (pre.inning, pre.half):
                self.no_pitch["half_rolled"] += 1
            if getattr(res, "pa_voided", None) == "pickoff_third_out":
                self.no_pitch["labelled"] += 1
            self._expect[pre.half] = pre.batter
            return
        voided = getattr(res, "steal_voided", None)
        if voided:
            self.voided[str(voided)] = self.voided.get(str(voided), 0) + 1
        cls = str(res.pitch_outcome)
        lead = lead_stealable(pre)
        if lead is not None and cls in self.opp["2"]:
            t = str(lead[2])
            self.opp[t][cls] += 1
            self._sim[f"opp{t}"] += 1
            if getattr(res, "steal_attempted", False):
                self.att[t][cls] += 1
                self._sim[f"att{t}"] += 1
                if getattr(res, "steal_outcome", None) == "safe":
                    self.safe[t] += 1
        if staged is not None and cls in self.opp["2"]:
            target, p = staged
            self.exp_attempt[str(target)][cls] += p
        if event == "strikeout":
            self._dropped_third_strike(pre, res)

    def _dropped_third_strike(self, pre: Snap, res: Any) -> None:
        """A strikeout that put a NEW runner on first is a dropped-third-strike
        reach. The batter is told apart from the runners already on base (the
        loop's own batter id can be stale on a half's first plate appearance)."""
        adv = dict(getattr(res, "baserunner_advances", {}) or {})
        on_base = {1: pre.first, 2: pre.second, 3: pre.third}
        runners = {rid for rid in on_base.values() if rid is not None}
        if not any(end == 1 and rid not in runners for rid, end in adv.items()):
            return
        self.d3k["reaches"] += 1
        if (
            getattr(res, "steal_attempted", False)
            or getattr(res, "pickoff_out", False)
            or getattr(res, "pickoff_error", False)
        ):
            # A steal or a pickoff moved the runners on this pitch: one mover
            # per pitch, so the reach's own advance did not run.
            self.d3k["with_running_play"] += 1
            return
        loaded_behind = pre.first is not None and pre.second is not None
        for base, rid in on_base.items():
            if rid is None:
                continue
            end = adv.get(rid)
            moved = end is not None and (end == 0 or end > base)
            self.d3k["runners_on"] += 1
            self.d3k["runners_moved"] += int(moved)
            unforced = (base == 2 and pre.first is None) or (base == 3 and not loaded_behind)
            if unforced:
                self.d3k["unforced_chances"] += 1
                self.d3k["unforced_moved"] += int(moved)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "opp": self.opp,
            "att": self.att,
            "safe": self.safe,
            "ibb": self.ibb,
            "pickoffs_by_count": self.pickoffs_by_count,
            "pickoff_steps": self.pickoff_steps,
            "seam_kind": self.seam_kind,
            "pickoff_third_outs": self.pickoff_third_outs,
            "pickoff_third_outs_pa_credited": self.pickoff_third_outs_pa_credited,
            "no_pitch": self.no_pitch,
            "leadoff": self.leadoff,
            "voided": self.voided,
            "d3k": self.d3k,
            "exp_pickoff": self.exp_pickoff,
            "exp_attempt": self.exp_attempt,
            "tally": self.tally,
            "per_sim": self.per_sim,
        }


# ---------------------------------------------------------------------------
# The sampler seams (wrapped once per process) and the step wrap (per machine)
# ---------------------------------------------------------------------------


def expected_shares(
    fp: Any, args: tuple[Any, ...], kw: dict[str, Any], *, rows: Any, aggression: float
) -> tuple[float, float] | None:
    """The weight shares of the group's rows with a pickoff outcome and with an
    attempt -> (pickoff share, attempt share), or None when the group draws
    nothing. ``steal_weights`` uses no random number."""
    got = fp.steal_weights(
        *args[:4],
        outs=kw["outs"],
        balls=kw["balls"],
        strikes=kw["strikes"],
        score_diff=kw["score_diff"],
        aggression=aggression,
        rows=rows,
    )
    if got is None:
        return None
    pool, idx, w = got
    w = np.asarray(w, dtype=np.float64)
    total = float(w.sum())
    if not math.isfinite(total) or total <= 0.0:
        return None
    idx = np.asarray(idx)
    po = np.asarray(pool.pickoff_out)[idx].astype(bool) | np.asarray(pool.pickoff_error)[
        idx
    ].astype(bool)
    att = np.asarray(pool.attempted)[idx].astype(bool)
    return float(w[po].sum() / total), float(w[att].sum() / total)


def install_seams(fp: Any, holder: dict[str, Any]) -> None:
    """Wrap the cached sampler's two running-game draws ONCE. The wrappers
    feed ``holder["rec"]`` and change no draw."""
    if getattr(fp.steal_draw, _MARK, False):
        return
    orig_steal = fp.steal_draw
    orig_pickoff = getattr(fp, "pickoff_draw", None)

    def steal_draw(*a: Any, _o: Any = orig_steal, **kw: Any) -> Any:
        rec = holder.get("rec")
        pitch_class = kw.get("pitch_class")
        aggression = float(kw.get("aggression", 1.0))
        if rec is not None:
            if pitch_class is None:
                # The single pre-pitch draw (arm 0): the count group answers
                # the pickoff and the steal at once.
                got = expected_shares(fp, a, kw, rows=None, aggression=aggression)
                rec.add_expected_pickoff(a[0], got[0] if got else 0.0)
                rec.stage_expected_attempt(a[0], got[1] if got else 0.0)
            else:
                meta = fp._steal_meta(str(int(a[0])))
                code = CLASSES.index(pitch_class) + 1 if pitch_class in CLASSES else 0
                rows = (
                    meta["class_cells"].get((kw["outs"], kw["balls"], kw["strikes"], code))
                    if meta is not None and "class_cells" in meta
                    else None
                )
                got = (
                    expected_shares(fp, a, kw, rows=rows, aggression=aggression)
                    if rows is not None and len(rows)
                    else None
                )
                rec.stage_expected_attempt(a[0], got[1] if got else 0.0)
        res = _o(*a, **kw)
        if rec is not None and pitch_class is None and res is not None and len(res) >= 5:
            if res[2] or res[4]:
                rec.add_seam_kind(bool(res[2]), bool(res[3]), bool(res[4]))
        return res

    setattr(steal_draw, _MARK, True)
    fp.steal_draw = steal_draw
    if orig_pickoff is None:
        return

    def pickoff_draw(*a: Any, _o: Any = orig_pickoff, **kw: Any) -> Any:
        rec = holder.get("rec")
        if rec is not None:
            meta = fp._steal_meta(str(int(a[0])))
            cell = (int(kw["outs"]), int(kw["balls"]), int(kw["strikes"]))
            p = 0.0
            if meta is not None and cell in meta.get("pickoff_live", ()):
                got = expected_shares(fp, a, kw, rows=meta["pickoff_cells"][cell], aggression=1.0)
                p = got[0] if got else 0.0
            rec.add_expected_pickoff(a[0], p)
        res = _o(*a, **kw)
        if rec is not None and res is not None and (res[0] or res[2]):
            rec.add_seam_kind(bool(res[0]), bool(res[1]), bool(res[2]))
        return res

    setattr(pickoff_draw, _MARK, True)
    fp.pickoff_draw = pickoff_draw


def install_step(machine: Any, rec: Recorder) -> None:
    """Wrap one machine's ``step_pitch``; read the result at once (its
    ``next_state`` is the live, mutating state)."""
    if getattr(machine.step_pitch, _MARK, False):
        return
    orig = machine.step_pitch
    batter_of = machine._current_batter_id

    def step(state: Any, *a: Any, _o: Any = orig, **k: Any) -> Any:
        pre = snap_of(state, batter_of(state))
        rec.begin_step()
        res = _o(state, *a, **k)
        rec.observe(pre, res, snap_of(state, batter_of(state)))
        return res

    setattr(step, _MARK, True)
    machine.step_pitch = step


def pool_reference(steal_pools: dict[str, Any]) -> dict[str, Any]:
    """The pool's own answer, from the loaded artifact pools, over PITCH rows:
    the attempts by class, the attempt rate and the safe share; and the
    pickoff outcomes on pitch rows (tags) and on pickoff rows.

    The shares and rates weight each row by its recency, the draw's base
    weight (a synthetic pool carries its rates as row weights); the raw row
    counts by class sit beside them (the design's §2.1 table counts rows).
    """
    out: dict[str, Any] = {"targets": {}}
    att_w = dict.fromkeys(CLASSES, 0.0)
    att_n = dict.fromkeys(CLASSES, 0)
    tags = rows_po = 0
    for t in TARGETS:
        p = steal_pools.get(t)
        if p is None:
            continue
        att = np.asarray(p.attempted).astype(bool)
        suc = np.asarray(p.success).astype(bool)
        rcy = np.asarray(p.recency, dtype=np.float64)
        po = np.asarray(p.pickoff_out).astype(bool) | np.asarray(p.pickoff_error).astype(bool)
        code = None if p.pitch_class is None else np.asarray(p.pitch_class).astype(np.int64)
        pitch = np.ones(att.size, dtype=bool) if code is None else code > 0
        by_cls: dict[str, int] = {}
        if code is not None:
            for i, c in enumerate(CLASSES):
                m = att & (code == i + 1)
                by_cls[c] = int(m.sum())
                att_n[c] += int(m.sum())
                att_w[c] += float(rcy[m].sum())
        t_tags = int((po & pitch).sum())
        t_rows = int((po & ~pitch).sum())
        tags += t_tags
        rows_po += t_rows
        a, r, s = att[pitch], rcy[pitch], suc[pitch]
        out["targets"][t] = {
            "rows": int(att.size),
            "pitch_rows": int(pitch.sum()),
            "pickoff_rows": int((~pitch).sum()),
            "att_rate": float(a.mean()) if a.size else 0.0,
            "att_rate_rcy": float((a * r).sum() / r.sum()) if r.sum() > 0 else 0.0,
            "safe_share": float((r * (a & s)).sum() / (r * a).sum()) if (r * a).sum() > 0 else 0.0,
            "att_by_class": by_cls,
            "tags": t_tags,
            "pickoff_row_outcomes": t_rows,
        }
    w_total = sum(att_w.values())
    n_total = sum(att_n.values())
    out["has_classes"] = n_total > 0
    out["att_share"] = {c: (v / w_total if w_total else 0.0) for c, v in att_w.items()}
    out["att_share_raw"] = {c: (v / n_total if n_total else 0.0) for c, v in att_n.items()}
    out["pickoff_ratio"] = (tags + rows_po) / tags if tags else float("nan")
    return out


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _game_pks(args: argparse.Namespace) -> list[int]:
    with open(_GAME_SET, encoding="utf-8") as fh:
        pks = [int(g["game_pk"]) for g in json.load(fh)["order"]]
    return pks[: args.games] if args.games else pks


def run(args: argparse.Namespace) -> int:
    arm = int(args.arm)
    # Before the first factory build: the factory reads SIM_* once, when it
    # builds the process's sampler. The sampler attribute is set after it too.
    os.environ["SIM_STEAL_PITCH_CLASS"] = "1" if arm else "0"
    from sim_stats import _resolve, open_sim_duckdb, sim_kwargs_from_state

    from simulation.batch_runner import GameSpec
    from simulation.production_factory import production_machine_factory
    from simulation.sim_loop import BoxScore, simulate_game

    game_pks = _game_pks(args)
    print(
        f"sim554_running_game_census: arm {arm} "
        f"({'the new order' if arm else 'the single pre-pitch draw'}), "
        f"{len(game_pks)} games x {args.iters} iterations",
        flush=True,
    )
    duck = open_sim_duckdb()
    try:
        states = [asyncio.run(_resolve(gp, duck)) for gp in game_pks]
    finally:
        if duck is not None:
            duck.close()

    rec = Recorder()
    holder: dict[str, Any] = {"rec": rec}
    pool_ref: dict[str, Any] | None = None
    t0 = time.perf_counter()
    for gp, state in zip(game_pks, states, strict=True):
        kw = sim_kwargs_from_state(state)
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        fp = machine.full_pool_sampler
        fp.steal_pitch_class = bool(arm)
        if pool_ref is None:
            _guard(machine, fp, arm)
            pool_ref = pool_reference(fp.a.steal_pools)
        install_seams(fp, holder)
        install_step(machine, rec)
        for seed in range(args.iters):
            rec.start_game_sim()
            machine.boxscore = BoxScore()
            t1 = time.perf_counter()
            res = simulate_game(state_machine=machine, seed=seed, **kw)
            rec.end_game_sim(res.boxscore.lines.values(), time.perf_counter() - t1)
        rec.add_tally(machine.running_game_tally)
        print(f"  game {gp} done ({time.perf_counter() - t0:.0f}s)", flush=True)

    out = {
        "arm": arm,
        "game_pks": game_pks,
        "iters": args.iters,
        "elapsed_s": time.perf_counter() - t0,
        "env": {k: v for k, v in sorted(os.environ.items()) if k.startswith("SIM_")},
        "pool": pool_ref or {},
        **rec.to_jsonable(),
    }
    Path(args.json_out).write_text(json.dumps(out), encoding="utf-8")
    print(f"wrote {args.json_out}  ({out['elapsed_s']:.0f}s)")
    return 0


def _guard(machine: Any, fp: Any, arm: int) -> None:
    """Refuse an arm that would read as "no effect" because it ran the other
    order or a flag the reads need is off (the SIM-449 rule)."""
    flags = {
        "SIM_STEAL_PITCH_CLASS": os.environ.get("SIM_STEAL_PITCH_CLASS"),
        "fp.steal_pitch_class": fp.steal_pitch_class,
        "fp.has_steal_classes()": fp.has_steal_classes(),
        "machine._steal_order_active()": machine._steal_order_active(),
        "machine._got_away (SIM_GOT_AWAY)": getattr(machine, "_got_away", None),
        "machine.manager (SIM_MANAGER)": machine.manager is not None,
        "SIM_MANAGER_DRAW": os.environ.get("SIM_MANAGER_DRAW"),
    }
    print("  the effective flags:", flush=True)
    for k, v in flags.items():
        print(f"    {k} = {v}", flush=True)
    if machine.manager is None:
        raise SystemExit("SIM_MANAGER is off in this process; the census needs the production loop")
    if not getattr(machine, "_got_away", False):
        raise SystemExit(
            "SIM_GOT_AWAY is off in this process; the dropped-third-strike read needs it"
        )
    pools = fp.a.steal_pools
    if arm == 1:
        bare = [t for t in TARGETS if pools.get(t) is None or pools[t].pitch_class is None]
        if bare:
            raise SystemExit(
                f"the bundle's steal pool carries no pitch class for target(s) {bare}: arm 1 "
                "would silently run the old order. Rebuild and export the steal pool first "
                "(scripts/sim554_rebuild_steal_pool.py)"
            )
        if not machine._steal_order_active():
            raise SystemExit("arm 1 does not run the new order; read the flags above")
    elif machine._steal_order_active():
        raise SystemExit("arm 0 runs the new order; read the flags above")


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def ratio_se(pairs: Iterable[tuple[float, float]]) -> tuple[float, float]:
    """The ratio sum(a) / sum(o) over game-sims and its standard error (the
    delta method: the game-sim is the unit)."""
    arr = np.asarray(list(pairs), dtype=np.float64).reshape(-1, 2)
    a, o = arr[:, 0], arr[:, 1]
    total = float(o.sum())
    if total <= 0:
        return 0.0, float("nan")
    rate = float(a.sum()) / total
    n = len(a)
    if n < 2:
        return rate, float("nan")
    resid = a - rate * o
    return rate, float(math.sqrt(float((resid**2).sum()) * n / (n - 1)) / total)


def _mean_se(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return float("nan"), float("nan")
    arr = np.asarray(xs, dtype=np.float64)
    se = float(arr.std(ddof=1) / math.sqrt(arr.size)) if arr.size > 1 else float("nan")
    return float(arr.mean()), se


def summarize(a: dict[str, Any]) -> dict[str, Any]:
    """The derived reads of one arm's JSON."""
    per_sim = a.get("per_sim", [])
    g = len(per_sim)
    att_per_opp = {t: ratio_se((p[f"att{t}"], p[f"opp{t}"]) for p in per_sim) for t in TARGETS}
    att_cls = {c: sum(int(a["att"][t][c]) for t in TARGETS) for c in CLASSES}
    opp_cls = {c: sum(int(a["opp"][t][c]) for t in TARGETS) for c in CLASSES}
    n_att = sum(att_cls.values())
    n_opp = sum(opp_cls.values())
    att_share = {c: (v / n_att if n_att else 0.0) for c, v in att_cls.items()}
    exp_po_sum = sum(float(a["exp_pickoff"][t][0]) for t in TARGETS)
    exp_po_n = sum(int(a["exp_pickoff"][t][1]) for t in TARGETS)
    exp_att_cls = {c: sum(float(a["exp_attempt"][t][c]) for t in TARGETS) for c in CLASSES}
    exp_att_total = sum(exp_att_cls.values())
    sb = _mean_se([p["sb"] / 2.0 for p in per_sim])
    cs = _mean_se([p["cs"] / 2.0 for p in per_sim])
    po_steps = a.get("pickoff_steps", {})
    return {
        "arm": int(a.get("arm", -1)),
        "game_sims": g,
        "seconds": _mean_se([p["seconds"] for p in per_sim])[0],
        "opp": {t: sum(a["opp"][t].values()) for t in TARGETS},
        "att": {t: sum(a["att"][t].values()) for t in TARGETS},
        "att_per_opp": att_per_opp,
        "safe_share": {
            t: (a["safe"][t] / sum(a["att"][t].values()) if sum(a["att"][t].values()) else 0.0)
            for t in TARGETS
        },
        "n_att": n_att,
        "att_share": att_share,
        "opp_share": {c: (v / n_opp if n_opp else 0.0) for c, v in opp_cls.items()},
        "misplaced_share": sum(att_share[c] for c in MISPLACED),
        "sb_per_team_game": sb,
        "cs_per_team_game": cs,
        "pickoffs_per_game": (sum(p["pickoffs"] for p in per_sim) / g) if g else 0.0,
        "pickoff_steps": po_steps,
        "pickoffs_by_count": a.get("pickoffs_by_count", {}),
        "seam_kind": a.get("seam_kind", {}),
        "exp_pickoff_per_draw": exp_po_sum / exp_po_n if exp_po_n else 0.0,
        "exp_pickoff_per_game": exp_po_sum / g if g else 0.0,
        "exp_pickoff_draws": exp_po_n,
        "exp_att_share": {
            c: (v / exp_att_total if exp_att_total else 0.0) for c, v in exp_att_cls.items()
        },
        "exp_att_per_opp": {
            t: (
                sum(float(v) for v in a["exp_attempt"][t].values()) / sum(a["opp"][t].values())
                if sum(a["opp"][t].values())
                else 0.0
            )
            for t in TARGETS
        },
        "no_pitch": a.get("no_pitch", {}),
        "leadoff": a.get("leadoff", {}),
        "pickoff_third_outs": int(a.get("pickoff_third_outs", 0)),
        "pickoff_third_outs_pa_credited": int(a.get("pickoff_third_outs_pa_credited", 0)),
        "voided": a.get("voided", {}),
        "third_out_first": int(a.get("voided", {}).get("third_out_first", 0)),
        "d3k": a.get("d3k", {}),
        "tally": a.get("tally", {}),
        "ibb": int(a.get("ibb", 0)),
    }


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    ok: bool
    text: str


def stop_rules(off: dict[str, Any], on: dict[str, Any]) -> list[Rule]:
    """The run book's stop rules (design §8 step 5) on two arm summaries."""
    rules: list[Rule] = []
    share = float(on["misplaced_share"])
    ok = int(on["n_att"]) > 0 and share <= MISPLACED_STOP
    rules.append(
        Rule(
            "misplaced",
            ok,
            f"the new arm's attempts on a foul, a ball in play or a hit by pitch: {share:.2%} "
            f"of {int(on['n_att']):,} (stop above {MISPLACED_STOP:.0%}; no attempt at all stops)",
        )
    )
    off_po, on_po = float(off["exp_pickoff_per_draw"]), float(on["exp_pickoff_per_draw"])
    ratio = on_po / off_po if off_po > 0 else float("nan")
    lo, hi = PICKOFF_RATIO_WINDOW
    rules.append(
        Rule(
            "pickoff_ratio",
            bool(lo <= ratio <= hi),
            f"the expected pickoffs per draw, new over old: {ratio:.3f} ({on_po:.6f} / "
            f"{off_po:.6f}); window {lo:.2f}-{hi:.2f}, the pool's own {POOL_PICKOFF_RATIO:.2f}",
        )
    )
    for t in TARGETS:
        r0, se0 = off["att_per_opp"][t]
        r1, se1 = on["att_per_opp"][t]
        se = math.sqrt(float(se0) ** 2 + float(se1) ** 2)
        moved = abs(float(r1) - float(r0))
        ok = bool(math.isfinite(se) and moved <= VOLUME_SE_STOP * se)
        rules.append(
            Rule(
                f"volume_{t}",
                ok,
                f"target {t}: attempts per opportunity {float(r0):.4f} -> {float(r1):.4f}, moved "
                f"{moved:.4f} against {VOLUME_SE_STOP:g} unpaired standard errors "
                f"{VOLUME_SE_STOP * se:.4f}",
            )
        )
    n = int(on["third_out_first"])
    rules.append(
        Rule(
            "third_out_first",
            n == 0,
            f"steals voided as the third out first on the new arm: {n} (the old arm: "
            f"{int(off['third_out_first'])}); the class group holds no attempt there",
        )
    )
    return rules


def _row(label: str, off: str, on: str, pool: str = "") -> str:
    return f"  {label:<46}{off:>18}{on:>18}{pool:>14}"


def _pm(v: tuple[float, float], fmt: str = ".4f") -> str:
    return f"{v[0]:{fmt}}+-{v[1]:{fmt}}"


def report_lines(off_json: dict[str, Any], on_json: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The side-by-side reads and the stop rules -> (lines, the rules that stop)."""
    off, on = summarize(off_json), summarize(on_json)
    pool = on_json.get("pool") or off_json.get("pool") or {}
    out = [
        "SIM-554 running-game census: OFF = the single pre-pitch draw, ON = the new order",
        f"  games {len(on_json.get('game_pks', []))} x {on_json.get('iters')} iterations; "
        f"game-sims OFF {off['game_sims']:,}, ON {on['game_sims']:,}",
        _row("read", "OFF", "ON", "pool"),
        _row(
            "seconds per game-sim",
            f"{off['seconds']:.2f}",
            f"{on['seconds']:.2f}",
        ),
    ]
    targets = pool.get("targets", {})
    for t in TARGETS:
        pt = targets.get(t, {})
        out.append(_row(f"target {t}: opportunities", f"{off['opp'][t]:,}", f"{on['opp'][t]:,}"))
        out.append(_row(f"target {t}: attempts", f"{off['att'][t]:,}", f"{on['att'][t]:,}"))
        out.append(
            _row(
                f"target {t}: attempts per opportunity",
                _pm(off["att_per_opp"][t]),
                _pm(on["att_per_opp"][t]),
                f"{pt.get('att_rate_rcy', float('nan')):.4f}",
            )
        )
        out.append(
            _row(
                f"target {t}: expected attempts per opportunity",
                f"{off['exp_att_per_opp'][t]:.4f}",
                f"{on['exp_att_per_opp'][t]:.4f}",
            )
        )
        out.append(
            _row(
                f"target {t}: safe share",
                f"{off['safe_share'][t]:.3f}",
                f"{on['safe_share'][t]:.3f}",
                f"{pt.get('safe_share', float('nan')):.3f}",
            )
        )
    out.append(
        "  attempts by the class of the pitch they rode (share; the pool's own, recency-weighted, "
        "pitch rows; real 2023-2026):"
    )
    for c in CLASSES:
        out.append(
            _row(
                f"  {c}",
                f"{off['att_share'][c]:.3f}",
                f"{on['att_share'][c]:.3f}",
                f"{pool.get('att_share', {}).get(c, float('nan')):.3f}"
                f" / {REAL_ATTEMPT_SHARE[c]:.3f}",
            )
        )
    out.append(
        _row(
            "misplaced share (foul + in play + HBP)",
            f"{off['misplaced_share']:.3f}",
            f"{on['misplaced_share']:.3f}",
        )
    )
    out.append("  expected attempt share by class (the weights at each steal draw):")
    for c in CLASSES:
        out.append(
            _row(f"  {c}", f"{off['exp_att_share'][c]:.3f}", f"{on['exp_att_share'][c]:.3f}")
        )
    out.append(
        _row(
            "stolen bases per team-game",
            _pm(off["sb_per_team_game"], ".3f"),
            _pm(on["sb_per_team_game"], ".3f"),
        )
    )
    out.append(
        _row(
            "caught stealings per team-game",
            _pm(off["cs_per_team_game"], ".3f"),
            _pm(on["cs_per_team_game"], ".3f"),
        )
    )
    out.append(
        _row(
            "pickoff outcomes per game",
            f"{off['pickoffs_per_game']:.4f}",
            f"{on['pickoffs_per_game']:.4f}",
        )
    )
    out.append(
        _row(
            "expected pickoffs per draw",
            f"{off['exp_pickoff_per_draw']:.6f}",
            f"{on['exp_pickoff_per_draw']:.6f}",
            f"x{pool.get('pickoff_ratio', float('nan')):.3f}",
        )
    )
    out.append(
        _row(
            "expected pickoffs per game",
            f"{off['exp_pickoff_per_game']:.4f}",
            f"{on['exp_pickoff_per_game']:.4f}",
        )
    )
    for kind in ("out", "out_advancing", "error"):
        out.append(
            _row(
                f"pickoff kind at the seam: {kind}",
                f"{off['seam_kind'].get(kind, 0):,}",
                f"{on['seam_kind'].get(kind, 0):,}",
            )
        )
    counts = sorted(set(off["pickoffs_by_count"]) | set(on["pickoffs_by_count"]))
    for key in counts:
        out.append(
            _row(
                f"pickoffs at {key}",
                f"{off['pickoffs_by_count'].get(key, 0):,}",
                f"{on['pickoffs_by_count'].get(key, 0):,}",
            )
        )
    out.append(
        _row(
            "pickoff third outs", f"{off['pickoff_third_outs']:,}", f"{on['pickoff_third_outs']:,}"
        )
    )
    out.append(
        _row(
            "  of which credited a plate appearance",
            f"{off['pickoff_third_outs_pa_credited']:,}",
            f"{on['pickoff_third_outs_pa_credited']:,}",
        )
    )
    for key in ("n", "half_rolled", "labelled"):
        out.append(
            _row(
                f"no-pitch steps: {key}",
                f"{off['no_pitch'].get(key, 0):,}",
                f"{on['no_pitch'].get(key, 0):,}",
            )
        )
    for key in ("same", "other", "unchecked"):
        out.append(
            _row(
                f"leadoff after a no-pitch third out: {key}",
                f"{off['leadoff'].get(key, 0):,}",
                f"{on['leadoff'].get(key, 0):,}",
            )
        )
    reasons = sorted(set(off["voided"]) | set(on["voided"])) or ["third_out_first"]
    for reason in reasons:
        out.append(
            _row(
                f"steals voided: {reason}",
                f"{off['voided'].get(reason, 0):,}",
                f"{on['voided'].get(reason, 0):,}",
            )
        )
    for key in (
        "reaches",
        "runners_on",
        "runners_moved",
        "unforced_chances",
        "unforced_moved",
        "with_running_play",
    ):
        out.append(
            _row(
                f"dropped third strike: {key}",
                f"{off['d3k'].get(key, 0):,}",
                f"{on['d3k'].get(key, 0):,}",
            )
        )
    out.append("  the loop's RunningGameTally, summed over the machines:")
    for key in sorted(set(off["tally"]) | set(on["tally"])):
        out.append(_row(f"  {key}", str(off["tally"].get(key, "")), str(on["tally"].get(key, ""))))
    out.append("STOP RULES (the run book, design §8 step 5):")
    rules = stop_rules(off, on)
    for r in rules:
        out.append(f"  {'PASS' if r.ok else 'STOP'}  {r.name}: {r.text}")
    stops = [r.name for r in rules if not r.ok]
    out.append(
        "VERDICT: "
        + (
            "every stop rule passes; go to the ten-game smoke"
            if not stops
            else f"STOP for a diagnosis: {', '.join(stops)}"
        )
    )
    return out, stops


def report(args: argparse.Namespace) -> int:
    off_json = json.loads(Path(args.off).read_text(encoding="utf-8"))
    on_json = json.loads(Path(args.on).read_text(encoding="utf-8"))
    if int(off_json.get("arm", 0)) == 1 and int(on_json.get("arm", 1)) == 0:
        off_json, on_json = on_json, off_json
        print("(the two files were given ON first; read as OFF, ON)")
    lines, stops = report_lines(off_json, on_json)
    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 1 if stops else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one arm")
    r.add_argument(
        "--arm",
        type=int,
        choices=(0, 1),
        required=True,
        help="0 = the single pre-pitch draw (today), 1 = the new order",
    )
    r.add_argument("--iters", type=int, default=20, help="iterations per game (seeds 0..N-1)")
    r.add_argument("--games", type=int, default=None, help="the first K games of the set only")
    r.add_argument("--json-out", required=True)
    p = sub.add_parser("report", help="pair the two arms: OFF first, then ON")
    p.add_argument("off")
    p.add_argument("on")
    p.add_argument("--out", default=None, help="also write the report here")
    args = ap.parse_args(argv)
    return run(args) if args.cmd == "run" else report(args)


if __name__ == "__main__":
    raise SystemExit(main())
