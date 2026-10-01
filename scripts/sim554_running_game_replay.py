"""
scripts/sim554_running_game_replay.py — SIM-554: the running-game replay.

THE QUESTION
============
A steal in the records is one combined fact: the runner went AND the batter did
not hit the ball. The simulator can sample that fact in several ways. This
script asks which way gives real pitches the best odds.

It replays real pitches on which a steal was possible through the simulator's
own draw machinery — the same sampler, the same cell index, the same weights —
without playing games. For each real pitch it hides the pitch's own game and
every later game (the point-in-time cutoff), builds the draw exactly as the loop
does, reads the WHOLE jar of candidate rows and their weights as a probability
table over (the pitch's result) x (no steal / stolen base / caught stealing),
and scores what really happened. Eleven designs are scored on the same pitches,
in two families: seven on the two-step pitch draw production runs (the pitch,
then its result), four on the single pitch draw:

  today                   three separate draws; the steal draw reads only the
                          count (production today: a steal can land on any pitch)
  steal_reads_class       three separate draws; the steal draw reads the count
                          AND the class of the pitch that came out (plan v1)
  steal_first             the runner decides first, from the steal pool's count
                          cell; when he goes, the drawn steal row's own pitch
                          result is the result; when he stays, the batter's
                          result comes from the non-running pitch rows
  pitch_steal             the pitch step and the steal are one row; the batter's
                          result is then drawn among real running pitches (or
                          among the non-running ones when the runner stays)
  pitch_steal_no_contact  the same, but when the runner goes the batter's result
                          is only barred from contact (ball, called strike, whiff)
  result_steal            the pitch step alone; then ONE row gives the batter's
                          result and the steal, every look-alike weight on it
  result_steal_2step      the same jar, read in two steps: the runner / catcher /
                          pitcher-hold weights decide only WHETHER a steal is
                          recorded; the batter's result then comes from the rows
                          of that kind, with the ordinary weights
  single_today            the single pitch draw, the separate steal draw as today
  single_reads_class      the single pitch draw, the steal draw reading the class
  one_draw                one row gives the pitch, the batter's result and the
                          steal (the single pitch draw, no second step)
  one_draw_2step          the single draw's jar, read in the same two steps

Every design is scored at four strengths of the runner / pitcher-hold / catcher
look-alike weights (LEVELS; the first is OFF) and at the base settings in
CONFIGS. The report picks each design's strength on one season and scores it on
another.

THE SCORES. Every score is a BRIER score (the squared distance between the odds
and what happened; bounded, so a design that says "impossible" for something
that then happens pays a fixed price). The score that decides is the WHOLE
TABLE: the pitch's result and the steal together. Designs are compared inside
the family of their pitch draw, against that family's baseline, because which
pitch draw to run is another ticket's question. The report also prints the
table's parts (the result alone; how many steals; the steal given the real
result), who steals by runner, what the steal rides by batter, and the LOG
score at two floors with how often each design said "impossible" — the log
score moves with the floor, so it does not decide. The rule is printed with the
report.

FIDELITY. The script drives a ``FullPoolSampler`` built by the production
factory and reads what the sampler assembled: ``_pa_rows`` / ``_bucket_cdf`` for
the pitch step, ``result_weights`` for the result step, ``steal_weights`` for
the steal draw, ``_matrix_gather`` for every look-alike factor. It re-implements
no weight. The steal facts are joined to the pitch rows in memory by pitch id
(the bundle's own meta parquet + ``sim.steal_opportunity_pool``, read-only), so
no table is rebuilt and the app stays up.

NOT MODELLED. The batting manager's steal weight is neutral for every design.
The player profiles are the season's (not point-in-time); that leak is the same
for every design. Pickoff labels are not scored.

    # one pass per season (all three base settings), resumable:
    docker compose run --rm -T -v <worktree>/scripts:/app/scripts \\
        -v <worktree>/simulation:/app/simulation app \\
        python scripts/sim554_running_game_replay.py pass --season 2026 --games 100000 \\
        --out "/app/scripts/sim554_replay_{season}_{config}.npz"
    # the report:
    python scripts/sim554_running_game_replay.py report --tune-season 2025 --test-season 2026 \\
        --dir /app/scripts --json-out /app/scripts/sim554_replay_report.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402

CLASSES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")
CLS_IDX = {c: i for i, c in enumerate(CLASSES)}
BALL, CALLED, SWING, FOUL, IN_PLAY, HBP = range(6)
OTHER = 6
NC = 7  # six classes + "other"
NCELL = NC * 3  # x (no steal, stolen base, caught stealing)

DESIGNS = (
    "today",
    "steal_reads_class",
    "steal_first",
    "pitch_steal",
    "pitch_steal_no_contact",
    "result_steal",
    "result_steal_2step",
    "single_today",
    "single_reads_class",
    "one_draw",
    "one_draw_2step",
)
D_IDX = {d: i for i, d in enumerate(DESIGNS)}
#: The designs on the single pitch draw (one step for the pitch and its result).
#: ``single_today`` / ``single_reads_class`` are that family's own baselines: the
#: single draw for the pitch, the separate steal draw as today / reading the class.
SINGLE = ("single_today", "single_reads_class", "one_draw", "one_draw_2step")
#: The designs on the two-step pitch draw production runs (the pitch, then its result).
SPLIT = tuple(d for d in DESIGNS if d not in SINGLE)
FAMILIES: dict[str, tuple[str, ...]] = {"split": SPLIT, "single": SINGLE}
BASELINE = {"split": "today", "single": "single_today"}
#: The simpler build first (the tie-break's last word). ``today`` changes
#: nothing; ``steal_reads_class`` adds one column to the small steal pool and
#: moves the steal draw after the pitch draw; ``steal_first`` makes the pitch
#: draw obey the drawn steal row and needs the steal facts on the pitch rows;
#: the merged designs move the steal facts onto the big pitch pool and change
#: the pitch draw itself (the two-step reads less than the unprotected ones,
#: which also need the look-alike weights inside the pitch draw).
BUILD_ORDER: dict[str, tuple[str, ...]] = {
    "split": (
        "today",
        "steal_reads_class",
        "steal_first",
        "pitch_steal_no_contact",
        "pitch_steal",
        "result_steal_2step",
        "result_steal",
    ),
    "single": ("single_today", "single_reads_class", "one_draw_2step", "one_draw"),
}
FAMILY_TITLE = {
    "split": "the two-step pitch draw (what production runs)",
    "single": "the single pitch draw (its own baseline)",
}
#: (runner, pitcher hold, catcher arm) powers of the three look-alike weights.
#: The first level is OFF: no look-alike weight on the running game at all.
LEVELS: tuple[tuple[float, float, float], ...] = (
    (0, 0, 0),
    (1, 1, 1),
    (4, 4, 2),
    (12, 12, 2),
)
STEAL_FACTORS = ("runner_steal", "pitcher_steal", "catcher_throwing")
POWER_ATTRS = (
    "pitch_pitcher_power",
    "pitch_batter_power",
    "result_pitcher_power",
    "result_batter_power",
)
#: The base settings. ``neutral`` = production as the factory built it.
CONFIGS: dict[str, dict[str, float]] = {
    "neutral": {},
    "split_fit": {"pitch_pitcher_power": 16, "result_pitcher_power": 16, "result_batter_power": 8},
    # The single draw must serve the pitcher and the batter with one weight each.
    "single_fit": {"pitch_pitcher_power": 16, "pitch_batter_power": 8},
}
#: What each base setting scores (``single_fit`` exists for the single draw only).
CONFIG_DESIGNS: dict[str, tuple[str, ...]] = {
    "neutral": DESIGNS,
    "split_fit": DESIGNS,
    "single_fit": SINGLE,
}
#: The per-pitch record: the odds given to what happened, and the pieces of it.
SLOTS = (
    "p_real",  # the odds of the real (result, steal) cell
    "p_class",  # the odds of the real result, steal or not
    "p_safe",  # the odds of a stolen base
    "js_ball",  # the odds of a steal (safe or out) on each result
    "js_called",
    "js_swing",
    "js_foul",
    "js_in_play",
    "js_hbp",
    "sum_sq",  # the sum of squares of the whole table (for the Brier score)
    "sum_sq_class",  # the same over the results alone
    "p_class_safe",  # the odds of the real result WITH a stolen base
)
S_REAL, S_CLASS, S_SAFE, S_JS0, S_SQ, S_SQC, S_CSAFE = 0, 1, 2, 3, 9, 10, 11
CLASS_MIN_CELL = 20  # plan v1: a class cell under 20 rows widens to the count cell
IMPOSSIBLE = 1e-9


def slots_of(joint: np.ndarray, c_real: int, s_real: int) -> np.ndarray:
    """The per-pitch record of one probability table (``NCELL`` odds, result x
    steal) against the real result ``c_real`` and the real steal outcome
    ``s_real`` (0 none, 1 stolen base, 2 caught)."""
    j = joint.reshape(NC, 3)
    pc = j.sum(axis=1)
    o = np.empty(len(SLOTS), dtype=np.float64)
    o[S_REAL] = joint[c_real * 3 + s_real]
    o[S_CLASS] = pc[c_real]
    o[S_SAFE] = j[:, 1].sum()
    o[S_JS0 : S_JS0 + 6] = (j[:, 1] + j[:, 2])[:6]
    o[S_SQ] = float(np.dot(joint, joint))
    o[S_SQC] = float(np.dot(pc, pc))
    o[S_CSAFE] = j[c_real, 1]
    return o


def _prev_day(ymd: int) -> int:
    d = date(ymd // 10000, (ymd // 100) % 100, ymd % 100) - timedelta(days=1)
    return int(d.strftime("%Y%m%d"))


# ---------------------------------------------------------------------------
# The steal facts on the pitch rows
# ---------------------------------------------------------------------------


class RunningData:
    """The steal facts of every pitch-pool row, joined in memory by pitch id,
    and the in-memory steal pools built from the same rows."""

    def __init__(self, fp: Any, duck: Any, art_dir: str):
        import duckdb

        from pipeline.batch.engine_artifacts import StealPool

        self.fp = fp
        t0 = time.time()
        seasons = sorted({int(s) for pool in fp.a.pools.values() for s in np.unique(pool.season)})
        season_list = ", ".join(str(s) for s in seasons)
        d = duck.execute(
            "SELECT pitch_id, target_base, runner_id, attempted, success "
            f"FROM sim.steal_opportunity_pool WHERE season IN ({season_list})"
        ).fetchnumpy()
        sp_id = np.asarray(np.ma.filled(d["pitch_id"], -1), dtype=np.int64)
        order = np.argsort(sp_id, kind="stable")
        sp_id = sp_id[order]
        sp_tgt = np.asarray(np.ma.filled(d["target_base"], 0), dtype=np.int8)[order]
        sp_run = np.asarray(np.ma.filled(d["runner_id"], 0), dtype=np.int64)[order]
        sp_att = np.asarray(np.ma.filled(d["attempted"], False), dtype=np.int8)[order]
        sp_safe = np.asarray(np.ma.filled(d["success"], False), dtype=np.int8)[order]
        if sp_id.size == 0:
            raise RuntimeError("sim.steal_opportunity_pool returned no rows")

        self.cls: dict[str, np.ndarray] = {}
        self.tgt: dict[str, np.ndarray] = {}
        self.att: dict[str, np.ndarray] = {}
        self.safe: dict[str, np.ndarray] = {}
        self.runner: dict[str, np.ndarray] = {}
        self.emb: dict[str, dict[str, np.ndarray]] = {}
        mem = duckdb.connect()
        self.coverage: dict[str, Any] = {}
        for hand, pool in fp.a.pools.items():
            path = os.path.join(art_dir, "pitch_pool", f"{hand}.meta.parquet")
            m = mem.execute(f"SELECT pitch_id, pitcher_id FROM read_parquet('{path}')").fetchnumpy()
            pid = np.asarray(np.ma.filled(m["pitch_id"], -1), dtype=np.int64)
            mpit = np.asarray(np.ma.filled(m["pitcher_id"], 0), dtype=np.int64)
            if pid.size != pool.n or not np.array_equal(
                mpit, np.asarray(pool.pitcher_id, dtype=np.int64)
            ):
                raise RuntimeError(
                    f"the {hand}-hand meta parquet does not line up with the loaded pool "
                    f"({pid.size} rows against {pool.n}) — the pitch ids cannot be trusted"
                )
            pos = np.clip(np.searchsorted(sp_id, pid), 0, sp_id.size - 1)
            hit = sp_id[pos] == pid
            self.tgt[hand] = np.where(hit, sp_tgt[pos], 0).astype(np.int8)
            self.att[hand] = np.where(hit, sp_att[pos], 0).astype(np.int8)
            self.safe[hand] = np.where(hit, sp_safe[pos], 0).astype(np.int8)
            self.runner[hand] = np.where(hit, sp_run[pos], 0).astype(np.int64)
            oc = np.asarray(pool.outcome_type, dtype=object)
            self.cls[hand] = np.fromiter(
                (CLS_IDX.get(str(o), OTHER) for o in oc), dtype=np.int8, count=pool.n
            )
            season = np.asarray(pool.season, dtype=np.int64)
            cat = getattr(pool, "catcher_id", None)
            cat = (
                np.asarray(cat, dtype=np.int64)
                if cat is not None
                else np.zeros(pool.n, dtype=np.int64)
            )
            self.emb[hand] = {
                "runner_steal": self._emb_rows("baserunner", self.runner[hand], season, hit),
                "pitcher_steal": self._emb_rows(
                    "pitcher_steal", np.asarray(pool.pitcher_id, dtype=np.int64), season, hit
                ),
                "catcher_throwing": self._emb_rows("catcher", cat, season, hit),
            }
            # coverage: how many opportunity-shaped rows carry a steal row
            sit = np.asarray(pool.sit)
            rs = sit[:, 3].astype(np.int64) & 0b111
            shape = (((rs & 1) == 1) & ((rs & 2) == 0)) | (((rs & 2) == 2) & ((rs & 4) == 0))
            shape &= sit[:, 2] <= 2
            ymd = np.asarray(pool.game_ymd)
            self.coverage[hand] = {
                "rows": int(pool.n),
                "opportunity_shaped": int(shape.sum()),
                "with_a_steal_row": int((shape & hit).sum()),
                "steal_rows_outside_the_shape": int((hit & ~shape).sum()),
                "last_day_pool": int(ymd.max()),
                "last_day_with_a_steal_row": int(ymd[hit].max()) if hit.any() else 0,
                "attempts": int(self.att[hand].sum()),
            }

        # The in-memory steal pools: the same rows the pitch pools hold, so the
        # steal draw and the pitch rows describe one set of real pitches.
        self.sp_cls: dict[str, np.ndarray] = {}
        pools: dict[str, Any] = {}
        for tb in (2, 3):
            parts: dict[str, list[np.ndarray]] = defaultdict(list)
            for hand, pool in fp.a.pools.items():
                idx = np.where(self.tgt[hand] == tb)[0]
                sit = np.asarray(pool.sit)[idx]
                parts["sit"].append(
                    np.stack(
                        [sit[:, 0], sit[:, 1], sit[:, 2], np.clip(sit[:, 5], -5, 5)], axis=1
                    ).astype(np.float32)
                )
                parts["runner"].append(self.runner[hand][idx])
                parts["pitcher"].append(np.asarray(pool.pitcher_id, dtype=np.int64)[idx])
                cat = getattr(pool, "catcher_id", None)
                parts["catcher"].append(
                    np.asarray(cat, dtype=np.int64)[idx]
                    if cat is not None
                    else np.zeros(idx.size, dtype=np.int64)
                )
                parts["season"].append(np.asarray(pool.season, dtype=np.int64)[idx])
                parts["att"].append(self.att[hand][idx])
                parts["safe"].append(self.safe[hand][idx])
                parts["rec"].append(np.asarray(pool.recency, dtype=np.float32)[idx])
                parts["ymd"].append(np.asarray(pool.game_ymd, dtype=np.int32)[idx])
                parts["cls"].append(self.cls[hand][idx])
            pools[str(tb)] = StealPool(
                sit=np.concatenate(parts["sit"]),
                runner_id=np.concatenate(parts["runner"]),
                pitcher_id=np.concatenate(parts["pitcher"]),
                catcher_id=np.concatenate(parts["catcher"]),
                season=np.concatenate(parts["season"]),
                attempted=np.concatenate(parts["att"]),
                success=np.concatenate(parts["safe"]),
                recency=np.concatenate(parts["rec"]),
                game_ymd=np.concatenate(parts["ymd"]),
            )
            self.sp_cls[str(tb)] = np.concatenate(parts["cls"])
        fp.a.steal_pools = pools
        fp._steal_meta_cache.clear()
        # The new pools carry no pitch class: forget the cached answer, so the
        # sampler reads these pools and not the bundle's.
        fp._steal_classes = None
        self.seconds = time.time() - t0

    def _emb_rows(
        self, actor: str, ids: np.ndarray, season: np.ndarray, mask: np.ndarray
    ) -> np.ndarray:
        out = np.full(ids.size, -1, dtype=np.int64)
        emb = self.fp.a.actor_emb.get(actor)
        if emb is None:
            return out
        kidx = emb["key_index"]
        idx = np.where(mask)[0]
        out[idx] = np.fromiter(
            (
                kidx.get(f"{int(a)}:{int(s)}", -1)
                for a, s in zip(ids[idx], season[idx], strict=True)
            ),
            dtype=np.int64,
            count=idx.size,
        )
        return out

    # -- tiers ---------------------------------------------------------------
    def tiers(self) -> tuple[dict[tuple[int, int], int], dict[tuple[int, int], int]]:
        """(batter, season) -> 1..3 by whiffs per swing (1 = the most contact);
        (runner, season) -> 1..3 by recorded attempts per opportunity pitch."""
        sw: dict[tuple[int, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
        rn: dict[tuple[int, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
        for hand, pool in self.fp.a.pools.items():
            cls = self.cls[hand]
            season = np.asarray(pool.season, dtype=np.int64)
            key = np.asarray(pool.batter_id, dtype=np.int64) * 10000 + season
            uk, inv = np.unique(key, return_inverse=True)
            swings = np.bincount(inv, weights=np.isin(cls, (SWING, FOUL, IN_PLAY)).astype(float))
            whiffs = np.bincount(inv, weights=(cls == SWING).astype(float))
            for k, s_, w_ in zip(uk.tolist(), swings.tolist(), whiffs.tolist(), strict=True):
                e = sw[(k // 10000, k % 10000)]
                e[0] += s_
                e[1] += w_
            opp = self.tgt[hand] > 0
            key = self.runner[hand][opp] * 10000 + season[opp]
            uk, inv = np.unique(key, return_inverse=True)
            n = np.bincount(inv).astype(float)
            a = np.bincount(inv, weights=self.att[hand][opp].astype(float))
            for k, n_, a_ in zip(uk.tolist(), n.tolist(), a.tolist(), strict=True):
                e = rn[(k // 10000, k % 10000)]
                e[0] += n_
                e[1] += a_

        def terciles(d: dict[tuple[int, int], list[float]], floor: float) -> dict:
            out: dict[tuple[int, int], int] = {}
            by_season: dict[int, list[tuple[float, tuple[int, int]]]] = defaultdict(list)
            for k, (den, num) in d.items():
                if den >= floor:
                    by_season[k[1]].append((num / den, k))
            for lst in by_season.values():
                lst.sort()
                m = len(lst)
                for rank, (_v, k) in enumerate(lst):
                    out[k] = 1 + min(2, (3 * rank) // max(m, 1))
            return out

        return terciles(sw, 200.0), terciles(rn, 100.0)


# ---------------------------------------------------------------------------
# The test set: pitcher-games
# ---------------------------------------------------------------------------


def sample_pitches(
    data: RunningData,
    season: int,
    n_games: int,
    min_opp: int,
    nonatt_every: int,
    rng: np.random.Generator,
) -> list[dict[str, Any]]:
    """Every recorded steal pitch and one in ``nonatt_every`` of the other
    opportunity pitches of ``n_games`` random (pitcher, game day) pairs. A
    sampled non-steal pitch weighs ``nonatt_every``."""
    fp = data.fp
    btier, rtier = data.tiers()
    per_group: dict[tuple[int, int], list[tuple[str, int]]] = defaultdict(list)
    for hand, pool in fp.a.pools.items():
        idx = np.where((data.tgt[hand] > 0) & (np.asarray(pool.season) == season))[0]
        pid = np.asarray(pool.pitcher_id)[idx]
        ymd = np.asarray(pool.game_ymd)[idx]
        for i, p_, y_ in zip(idx.tolist(), pid.tolist(), ymd.tolist(), strict=True):
            per_group[(int(p_), int(y_))].append((hand, int(i)))
    keys = sorted(k for k, v in per_group.items() if len(v) >= min_opp)
    if not keys:
        raise RuntimeError(f"no pitcher-game of {season} has {min_opp} opportunity pitches")
    chosen = rng.choice(len(keys), size=min(n_games, len(keys)), replace=False)
    recs: list[dict[str, Any]] = []
    for gi, c in enumerate(sorted(chosen.tolist())):
        for hand, i in per_group[keys[c]]:
            att = int(data.att[hand][i])
            if not att and rng.random() >= 1.0 / nonatt_every:
                continue
            pool = fp.a.pools[hand]
            s = np.asarray(pool.sit)[i]
            bh = getattr(pool, "bat_home", None)
            pc = getattr(pool, "pitch_count", None)
            tto = getattr(pool, "tto", None)
            cat = getattr(pool, "catcher_id", None)
            side = int(bh[i]) if bh is not None else -1
            batter = int(pool.batter_id[i])
            runner = int(data.runner[hand][i])
            recs.append(
                {
                    "hand": hand,
                    "i": int(i),
                    "group": gi,
                    "pitcher": int(pool.pitcher_id[i]),
                    "batter": batter,
                    "catcher": int(cat[i]) if cat is not None else 0,
                    "runner": runner,
                    "target": int(data.tgt[hand][i]),
                    "ymd": int(pool.game_ymd[i]),
                    "season": season,
                    "balls": int(min(max(s[0], 0), 3)),
                    "strikes": int(min(max(s[1], 0), 2)),
                    "inning": int(s[4]),
                    "base_out": (int(s[2]), int(s[3]), int(s[4]), int(max(-5, min(5, s[5])))),
                    "bat_home": (bool(side) if side in (0, 1) else None),
                    "pitch_count": (int(pc[i]) if pc is not None else None),
                    "tto": (int(tto[i]) if tto is not None else None),
                    "cls": int(data.cls[hand][i]),
                    "s": (1 if data.safe[hand][i] else 2) if att else 0,
                    "weight": 1.0 if att else float(nonatt_every),
                    "btier": int(btier.get((batter, season), 0)),
                    "rtier": int(rtier.get((runner, season), 0)),
                }
            )
    recs.sort(key=lambda r: (r["hand"], r["pitcher"], r["ymd"], r["inning"], r["batter"], r["i"]))
    return recs


def fingerprint(recs: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for r in recs:
        h.update(f"{r['hand']}:{r['i']};".encode())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# The scoring pass
# ---------------------------------------------------------------------------


class Pass:
    """One base setting over the test pitches: every design at every level."""

    def __init__(self, fp: Any, data: RunningData, config: str, k: int, k_att: int):
        self.fp = fp
        self.data = data
        self.config = config
        self.designs = CONFIG_DESIGNS[config]
        self.chain = any(d not in SINGLE for d in self.designs)
        self.k = k
        self.k_att = k_att
        self._group: tuple | None = None
        self._pa: tuple | None = None

    @staticmethod
    def _cls_shares(cls: np.ndarray, w: np.ndarray, fallback_cls: int) -> np.ndarray:
        tot = float(w.sum())
        if not np.isfinite(tot) or tot <= 0.0:
            one = np.zeros(NC)
            one[fallback_cls] = 1.0
            return one
        return np.bincount(cls, weights=w, minlength=NC)[:NC] / tot

    @staticmethod
    def _joint(cell: np.ndarray, w: np.ndarray, fallback_cell: int) -> np.ndarray:
        tot = float(w.sum())
        if not np.isfinite(tot) or tot <= 0.0:
            one = np.zeros(NCELL)
            one[fallback_cell] = 1.0
            return one
        return np.bincount(cell, weights=w, minlength=NCELL)[:NCELL] / tot

    @classmethod
    def _two_step(
        cls_,
        cell: np.ndarray,
        cls: np.ndarray,
        a: np.ndarray,
        base: np.ndarray,
        f: np.ndarray,
        fallback_cell: int,
    ) -> np.ndarray:
        """One jar read in two steps. Step one: is a steal recorded? — the
        share of the running rows under ``base * f`` (the look-alike weights
        decide only this). Step two: the row, among the rows of that kind —
        a non-running row under ``base`` alone (the ordinary weights, so the
        pitch's result is not disturbed), a running row under ``base * f``."""
        wf = base * f
        tot = float(wf.sum())
        if not np.isfinite(tot) or tot <= 0.0:
            one = np.zeros(NCELL)
            one[fallback_cell] = 1.0
            return one
        t_att = float(wf[a].sum())
        p_st = t_att / tot
        t_non = float(base[~a].sum())
        if t_non <= 0.0:
            return cls_._joint(cell, wf, fallback_cell)
        j = np.zeros((NC, 3))
        j[:, 0] = (1.0 - p_st) * np.bincount(cls[~a], weights=base[~a], minlength=NC)[:NC] / t_non
        out = j.reshape(-1)
        if t_att > 0.0:
            out = out + p_st * np.bincount(cell[a], weights=wf[a], minlength=NCELL)[:NCELL] / t_att
        return out

    def _setup(self, r: dict[str, Any]) -> None:
        fp = self.fp
        season = r["season"]
        group = (r["hand"], r["pitcher"], r["ymd"])
        if group != self._group:
            fp.set_asof(_prev_day(r["ymd"]))
            fp.new_half_inning(r["hand"], f"{r['pitcher']}:{season}", None)
            self._group = group
            self._pa = None
        pa = (r["batter"], r["base_out"], r["pitch_count"], r["tto"], r["bat_home"])
        if pa != self._pa:
            extra: dict[str, Any] = {}
            if r["pitch_count"] is not None and r["tto"] is not None:
                extra["pitch_count"], extra["tto"] = r["pitch_count"], r["tto"]
            if r["bat_home"] is not None:
                extra["bat_home"] = r["bat_home"]
            fp.new_plate_appearance(
                f"{r['batter']}:{season}", np.array(r["base_out"], dtype=np.float32), **extra
            )
            self._pa = pa

    def score(self, r: dict[str, Any], rng: np.random.Generator) -> np.ndarray | None:
        """(designs, levels, slots) for one real pitch, or None when the pitch
        step has no candidate row."""
        fp, data = self.fp, self.data
        self._setup(r)
        hand, season = r["hand"], r["season"]
        b = r["balls"] * 3 + r["strikes"]
        cdf = fp._bucket_cdf[b] if fp._bucket_cdf is not None else None
        if cdf is None or cdf.size == 0 or cdf[-1] <= 0 or fp._pa_rows is None:
            return None
        rows = fp._pa_rows[b]
        w = np.diff(np.asarray(cdf, dtype=np.float64), prepend=0.0)
        tot = float(w.sum())
        if rows.size == 0 or not np.isfinite(tot) or tot <= 0.0:
            return None
        cls = data.cls[hand][rows].astype(np.int64)
        a = (data.att[hand][rows] > 0) & (data.tgt[hand][rows] == r["target"])
        z = (data.safe[hand][rows] > 0) & a
        cell = cls * 3 + np.where(a, np.where(z, 1, 2), 0)
        c_real, s_real = r["cls"], r["s"]
        real_cell = c_real * 3 + s_real
        rkey = f"{r['runner']}:{season}"
        pkey = f"{r['pitcher']}:{season}"
        ckey = f"{r['catcher']}:{season}" if r["catcher"] > 0 else None
        emb = data.emb[hand]

        out = np.full((len(DESIGNS), len(LEVELS), len(SLOTS)), np.nan, dtype=np.float64)

        def put(design: str, li: int, joint: np.ndarray) -> None:
            out[D_IDX[design], li] = slots_of(joint, c_real, s_real)

        # --- the pitch step's anchors and the result step's weights -----------
        wr_cache: dict[int, np.ndarray | None] = {}

        def wr_of(j: int) -> np.ndarray | None:
            if j not in wr_cache:
                x = fp.result_weights(b, rows, int(rows[j]))
                wr_cache[j] = None if x is None else np.asarray(x, dtype=np.float64)
            return wr_cache[j]

        picks: list[int] = []
        p_chain = np.zeros(NC)
        if self.chain:
            picks = rng.choice(rows.size, size=self.k, replace=True, p=w / tot).tolist()
            for j in picks:
                wr = wr_of(j)
                p_chain += (
                    self._cls_shares(cls, wr, int(cls[j]))
                    if wr is not None
                    else np.eye(NC)[int(cls[j])]
                )
            p_chain /= len(picks)
        no_contact = np.isin(cls, (BALL, CALLED, SWING))
        # steal_first: when the runner stays, the batter's result comes from the
        # non-running rows under the ordinary weights (no look-alike weight).
        p_stay = np.zeros(NC)
        n_stay = 0
        for j in picks:
            if a[j]:
                continue
            wr = wr_of(j)
            p_stay += (
                self._cls_shares(cls[~a], wr[~a], int(cls[j]))
                if wr is not None
                else np.eye(NC)[int(cls[j])]
            )
            n_stay += 1
        p_stay = p_stay / n_stay if n_stay else p_chain.copy()
        p_single = self._cls_shares(cls, w, c_real)  # the single draw's own result odds

        for li, level in enumerate(LEVELS):
            for name, pw in zip(STEAL_FACTORS, level, strict=True):
                fp.actor_power[name] = float(pw)
            f = np.ones(rows.size, dtype=np.float64)
            for name, key in zip(STEAL_FACTORS, (rkey, pkey, ckey), strict=True):
                if key is None:
                    continue
                g = fp._matrix_gather(name, key, emb[name][rows])
                if g is not None:
                    f *= g

            # the steal draw from its own pool (today's draw), whole and by result
            got = fp.steal_weights(
                r["target"],
                rkey,
                pkey,
                ckey,
                outs=r["base_out"][0],
                balls=r["balls"],
                strikes=r["strikes"],
                score_diff=r["base_out"][3],
                aggression=1.0,
            )
            ps = np.array([1.0, 0.0, 0.0])
            ps_c = np.tile(ps, (NC, 1))
            pool_go = np.zeros((NC, 2))  # the pool's own (result, safe / caught) odds
            if got is not None:
                spool, srows, sw = got
                sw = np.asarray(sw, dtype=np.float64)
                st_tot = float(sw.sum())
                if st_tot > 0:
                    s_att = np.asarray(spool.attempted)[srows] > 0
                    s_safe = (np.asarray(spool.success)[srows] > 0) & s_att
                    scls = data.sp_cls[str(r["target"])][srows].astype(np.int64)
                    caught = s_att & ~s_safe
                    pool_go[:, 0] = (
                        np.bincount(scls[s_safe], weights=sw[s_safe], minlength=NC)[:NC] / st_tot
                    )
                    pool_go[:, 1] = (
                        np.bincount(scls[caught], weights=sw[caught], minlength=NC)[:NC] / st_tot
                    )

                    def triple(m: np.ndarray, _sw=sw, _a=s_att, _z=s_safe) -> np.ndarray:
                        t = float(_sw[m].sum())
                        go = float(_sw[m & _a].sum()) / t
                        sf = float(_sw[m & _z].sum()) / t
                        return np.array([1.0 - go, sf, go - sf])

                    live = sw > 0
                    ps = triple(live)
                    for c in range(NC):
                        m = live & (scls == c)
                        ps_c[c] = triple(m) if int(m.sum()) >= CLASS_MIN_CELL else ps

            # the single pitch draw: its baselines, then one row for everything
            put("single_today", li, (p_single[:, None] * ps[None, :]).reshape(-1))
            put("single_reads_class", li, (p_single[:, None] * ps_c).reshape(-1))
            put("one_draw", li, self._joint(cell, w * f, real_cell))
            put("one_draw_2step", li, self._two_step(cell, cls, a, w, f, real_cell))
            if not self.chain:
                continue

            # today / steal_reads_class / steal_first on the two-step pitch draw
            first = np.zeros((NC, 3))  # steal_first: the drawn steal row's own result
            first[:, 1:] = pool_go
            first[:, 0] = (1.0 - float(pool_go.sum())) * p_stay
            put("today", li, (p_chain[:, None] * ps[None, :]).reshape(-1))
            put("steal_reads_class", li, (p_chain[:, None] * ps_c).reshape(-1))
            put("steal_first", li, first.reshape(-1))

            # result_steal (+ 2step): the result row carries the steal
            acc = np.zeros(NCELL)
            acc2 = np.zeros(NCELL)
            for j in picks:
                wr = wr_of(j)
                if wr is None:
                    acc += np.eye(NCELL)[int(cell[j])]
                    acc2 += np.eye(NCELL)[int(cell[j])]
                else:
                    acc += self._joint(cell, wr * f, int(cell[j]))
                    acc2 += self._two_step(cell, cls, a, wr, f, int(cell[j]))
            put("result_steal", li, acc / len(picks))
            put("result_steal_2step", li, acc2 / len(picks))

            # pitch_steal (+ no_contact): the pitch row carries the steal
            w_a = w * f
            tot_a = float(w_a.sum())
            p_go = float(w_a[a].sum()) / tot_a if tot_a > 0 else 0.0
            p_safe = float(w_a[z].sum()) / float(w_a[a].sum()) if p_go > 0 else 0.0
            non = [j for j in picks if not a[j]]
            p_non = np.zeros(NC)
            p_non_all = np.zeros(NC)
            om = 0.0
            for j in non:
                wr = wr_of(j)
                o = float(f[j])
                om += o
                if wr is None:
                    p_non += o * np.eye(NC)[int(cls[j])]
                    p_non_all += o * np.eye(NC)[int(cls[j])]
                else:
                    p_non += o * self._cls_shares(cls[~a], wr[~a], int(cls[j]))
                    p_non_all += o * self._cls_shares(cls, wr, int(cls[j]))
            if om > 0:
                p_non /= om
                p_non_all /= om
            else:
                p_non = p_chain.copy()
                p_non_all = p_chain.copy()
            p_att = np.zeros(NC)
            p_att_nc = np.zeros(NC)
            if p_go > 0:
                att_idx = np.where(a)[0]
                pa2 = w_a[att_idx] / float(w_a[att_idx].sum())
                picks2 = rng.choice(att_idx.size, size=self.k_att, replace=True, p=pa2).tolist()
                for q in picks2:
                    j = int(att_idx[q])
                    wr = wr_of(j)
                    if wr is None:
                        p_att += np.eye(NC)[int(cls[j])]
                        p_att_nc += np.eye(NC)[int(cls[j])]
                    else:
                        p_att += self._cls_shares(cls[a], wr[a], int(cls[j]))
                        p_att_nc += self._cls_shares(cls[no_contact], wr[no_contact], int(cls[j]))
                p_att /= len(picks2)
                p_att_nc /= len(picks2)
            for design, pn, pt in (
                ("pitch_steal", p_non, p_att),
                ("pitch_steal_no_contact", p_non_all, p_att_nc),
            ):
                j3 = np.zeros((NC, 3))
                j3[:, 0] = (1.0 - p_go) * pn
                j3[:, 1] = p_go * p_safe * pt
                j3[:, 2] = p_go * (1.0 - p_safe) * pt
                put(design, li, j3.reshape(-1))
        return out


def run_pass(
    fp: Any,
    data: RunningData,
    recs: list[dict[str, Any]],
    config: str,
    production: dict[str, float],
    args: argparse.Namespace,
    out_path: Path,
) -> None:
    for k_, v_ in production.items():
        setattr(fp, k_, float(v_))
    for k_, v_ in CONFIGS[config].items():
        setattr(fp, k_, float(v_))
    fp.pitch_result_split = True  # the result weights need the bucket weights kept
    saved_powers = {n: fp.actor_power.get(n) for n in STEAL_FACTORS}
    n = len(recs)
    fpr = fingerprint(recs)
    res = np.full((n, len(DESIGNS), len(LEVELS), len(SLOTS)), np.nan, dtype=np.float32)
    done = 0
    if out_path.exists():
        try:
            z = np.load(out_path, allow_pickle=False)
            if str(z["fingerprint"]) == fpr and z["res"].shape == res.shape:
                res = z["res"].copy()
                done = int(z["done"])
                print(f"  [{config}] checkpoint: {done} of {n} pitches already scored", flush=True)
        except Exception as exc:  # noqa: BLE001 — a bad checkpoint is a fresh start
            print(f"  [{config}] the checkpoint could not be read ({exc}); starting over")
    runner = Pass(fp, data, config, args.k, args.k_att)
    rng = np.random.default_rng(args.seed + sum(map(ord, config)) + done)
    meta = {
        "weight": np.array([r["weight"] for r in recs], dtype=np.float32),
        "cls": np.array([r["cls"] for r in recs], dtype=np.int8),
        "s": np.array([r["s"] for r in recs], dtype=np.int8),
        "btier": np.array([r["btier"] for r in recs], dtype=np.int8),
        "rtier": np.array([r["rtier"] for r in recs], dtype=np.int8),
        "runner": np.array([r["runner"] for r in recs], dtype=np.int64),
        "group": np.array([r["group"] for r in recs], dtype=np.int32),
        "target": np.array([r["target"] for r in recs], dtype=np.int8),
    }

    def save(d: int) -> None:
        tmp = out_path.with_suffix(".tmp.npz")
        np.savez(
            tmp,
            res=res,
            done=d,
            fingerprint=fpr,
            config=config,
            designs=np.array(DESIGNS),
            levels=np.array(LEVELS, dtype=np.float32),
            slots=np.array(SLOTS),
            **meta,
        )
        os.replace(tmp, out_path)

    t0 = time.time()
    try:
        for idx in range(done, n):
            got = runner.score(recs[idx], rng)
            if got is not None:
                res[idx] = got
            if (idx + 1) % args.ckpt_every == 0:
                save(idx + 1)
            if (idx + 1) % args.progress_every == 0:
                el = time.time() - t0
                rate = (idx + 1 - done) / max(el, 1e-9)
                print(
                    f"  [{config}] {idx + 1}/{n}  {el:7.0f} s  {1000.0 / rate:6.1f} ms/pitch  "
                    f"eta {(n - idx - 1) / rate / 60.0:5.1f} min",
                    flush=True,
                )
        save(n)
    finally:
        for name, pw in saved_powers.items():
            if pw is None:
                fp.actor_power.pop(name, None)
            else:
                fp.actor_power[name] = pw
        for k_, v_ in production.items():
            setattr(fp, k_, float(v_))
    print(f"  [{config}] {n - done} pitches in {time.time() - t0:.0f} s -> {out_path}", flush=True)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    z = np.load(path, allow_pickle=False)
    d = {k: z[k] for k in z.files}
    if int(d["done"]) < d["res"].shape[0]:
        raise RuntimeError(f"{path} is not finished ({int(d['done'])} of {d['res'].shape[0]})")
    return d


def _per_pitch(res: np.ndarray, m: dict[str, np.ndarray], keep: np.ndarray) -> dict[str, Any]:
    """The per-pitch losses of one (design, level) over the pitches ``keep``."""
    p = res[keep].astype(np.float64)
    s = m["s"][keep]
    p_st = p[:, S_JS0 : S_JS0 + 6].sum(axis=1)
    p_safe = p[:, S_SAFE]
    p3 = np.stack([1.0 - p_st, p_safe, p_st - p_safe], axis=1)
    y3 = np.eye(3)[s]
    # The running game GIVEN the pitch's real result: the odds of no steal /
    # stolen base / caught stealing among the table's cells of that result.
    cls = np.clip(m["cls"][keep].astype(np.int64), 0, 5)
    pc = p[:, S_CLASS]
    js_real = p[np.arange(p.shape[0]), S_JS0 + cls]
    csafe = p[:, S_CSAFE]
    ok = pc > IMPOSSIBLE
    den = np.where(ok, pc, 1.0)
    q3 = np.stack(
        [
            np.where(ok, (pc - js_real) / den, 1.0),
            np.where(ok, csafe / den, 0.0),
            np.where(ok, (js_real - csafe) / den, 0.0),
        ],
        axis=1,
    )
    return {
        "brier_running": ((q3 - y3) ** 2).sum(axis=1),
        "brier_joint": p[:, S_SQ] - 2.0 * p[:, S_REAL] + 1.0,
        "brier_result": p[:, S_SQC] - 2.0 * p[:, S_CLASS] + 1.0,
        "brier_steal": ((p3 - y3) ** 2).sum(axis=1),
        "log_1e6": -np.log(np.maximum(p[:, S_REAL], 1e-6)),
        "log_1e3": -np.log(np.maximum(p[:, S_REAL], 1e-3)),
        "p_steal": p_st,
        "p": p,
    }


def _metrics(res: np.ndarray, m: dict[str, np.ndarray], keep: np.ndarray) -> dict[str, Any]:
    pp = _per_pitch(res, m, keep)
    p = pp["p"]
    wt = m["weight"][keep].astype(np.float64)
    cls = m["cls"][keep]
    s = m["s"][keep]
    st = s > 0
    tw = float(wt.sum())
    p_st = pp["p_steal"]
    js = p[:, S_JS0 : S_JS0 + 6]
    pred_mix = (wt[:, None] * js).sum(axis=0)
    pred_mix = pred_mix / max(pred_mix.sum(), 1e-12)
    real_mix = np.bincount(cls[st], minlength=6)[:6].astype(float)
    real_mix = real_mix / max(real_mix.sum(), 1.0)
    out: dict[str, Any] = {
        "pitches": int(keep.sum()),
        "steals": int(st.sum()),
        "brier_running": float((wt * pp["brier_running"]).sum() / tw),
        "brier_joint": float((wt * pp["brier_joint"]).sum() / tw),
        "brier_result": float((wt * pp["brier_result"]).sum() / tw),
        "brier_steal": float((wt * pp["brier_steal"]).sum() / tw),
        "log_floor_1e6": float((wt * pp["log_1e6"]).sum() / tw),
        "log_floor_1e3": float((wt * pp["log_1e3"]).sum() / tw),
        "said_impossible_share_of_steals": (
            float((p_st[st] < IMPOSSIBLE).mean()) if st.any() else None
        ),
        "said_impossible_share_of_pitches": float((wt * (p[:, S_REAL] < IMPOSSIBLE)).sum() / tw),
        "steals_per_100_real": float(100.0 * (wt * st).sum() / tw),
        "steals_per_100_predicted": float(100.0 * (wt * p_st).sum() / tw),
        "steal_mix_real": [float(x) for x in real_mix],
        "steal_mix_predicted": [float(x) for x in pred_mix],
        "steal_mix_distance": float(0.5 * np.abs(real_mix - pred_mix).sum()),
        "safe_share_real": float((s[st] == 1).mean()) if st.any() else None,
        "safe_share_predicted": float((wt * p[:, S_SAFE]).sum() / max((wt * p_st).sum(), 1e-12)),
    }
    return out


def _tier_table(
    res: np.ndarray, m: dict[str, np.ndarray], keep: np.ndarray, tier: str
) -> list[dict[str, Any]]:
    rows = []
    for t in (1, 2, 3):
        k = keep & (m[tier] == t)
        if not k.any():
            continue
        p = res[k].astype(np.float64)
        wt = m["weight"][k].astype(np.float64)
        st = m["s"][k] > 0
        p_st = p[:, S_JS0 : S_JS0 + 6].sum(axis=1)
        rows.append(
            {
                "tier": t,
                "pitches": int(k.sum()),
                "steals": int(st.sum()),
                "steals_per_100_real": float(100.0 * (wt * st).sum() / wt.sum()),
                "steals_per_100_predicted": float(100.0 * (wt * p_st).sum() / wt.sum()),
                "whiff_share_real": float((m["cls"][k][st] == SWING).mean()) if st.any() else None,
                "whiff_share_predicted": float(
                    (wt * p[:, S_JS0 + SWING]).sum() / max((wt * p_st).sum(), 1e-12)
                ),
            }
        )
    return rows


def _runner_rates(
    res: np.ndarray, m: dict[str, np.ndarray], keep: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """(real, predicted) steals per opportunity pitch of every runner with at
    least 150 weighted pitches — the same runners for every design."""
    wt = m["weight"][keep].astype(np.float64)
    y = (m["s"][keep] > 0).astype(np.float64)
    ps = res[keep][:, S_JS0 : S_JS0 + 6].astype(np.float64).sum(axis=1)
    _uk, inv = np.unique(m["runner"][keep], return_inverse=True)
    n = np.bincount(inv, weights=wt)
    real = np.bincount(inv, weights=wt * y)
    pred = np.bincount(inv, weights=wt * ps)
    big = n >= 150
    return real[big] / n[big], pred[big] / n[big]


def _corr_gap(rr: np.ndarray, pa: np.ndarray, pb: np.ndarray, rng: Any) -> dict[str, float] | None:
    """corr(real, a) minus corr(real, b), with a standard error from
    resampling runners. None when a correlation is undefined."""
    if rr.size < 10 or rr.std() == 0 or pa.std() == 0 or pb.std() == 0:
        return None
    point = float(np.corrcoef(rr, pa)[0, 1] - np.corrcoef(rr, pb)[0, 1])
    boots = []
    for _ in range(400):
        k = rng.integers(0, rr.size, size=rr.size)
        if rr[k].std() == 0 or pa[k].std() == 0 or pb[k].std() == 0:
            continue
        boots.append(np.corrcoef(rr[k], pa[k])[0, 1] - np.corrcoef(rr[k], pb[k])[0, 1])
    se = float(np.std(boots, ddof=1)) if len(boots) > 10 else float("inf")
    return {"difference": point, "standard_error": se}


def _runner_read(res: np.ndarray, m: dict[str, np.ndarray], keep: np.ndarray) -> dict[str, Any]:
    """Each runner's expected steals against his real ones (the prop's quantity)."""
    rr, pr = _runner_rates(res, m, keep)
    if rr.size < 10:
        return {"runners": int(rr.size)}
    big = np.ones(rr.size, dtype=bool)
    ok = pr.std() > 0 and rr.std() > 0
    return {
        "runners": int(big.sum()),
        "correlation": float(np.corrcoef(rr, pr)[0, 1]) if ok else None,
        "slope_real_on_predicted": float(np.polyfit(pr, rr, 1)[0]) if pr.std() > 0 else None,
        "mean_abs_error_per_100": float(100.0 * np.abs(rr - pr).mean()),
    }


def _paired(
    la: np.ndarray, lb: np.ndarray, m: dict[str, np.ndarray], keep: np.ndarray, rng: Any
) -> dict[str, float]:
    """The difference of two per-pitch losses, a minus b, with a standard error
    from resampling whole pitcher-games."""
    wt = m["weight"][keep].astype(np.float64)
    g = m["group"][keep]
    ug, inv = np.unique(g, return_inverse=True)
    num = np.bincount(inv, weights=wt * (la - lb))
    den = np.bincount(inv, weights=wt)
    point = float(num.sum() / den.sum())
    boots = np.empty(400)
    for i in range(400):
        pick = rng.integers(0, ug.size, size=ug.size)
        boots[i] = num[pick].sum() / den[pick].sum()
    return {"difference": point, "standard_error": float(boots.std(ddof=1))}


def _lv(li: int) -> str:
    return "off" if li == 0 else "/".join(f"{x:g}" for x in LEVELS[li])


def report(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    rng = np.random.default_rng(args.seed)
    sets: dict[str, dict[str, dict[str, Any]]] = {}
    for label, season in (("tune", args.tune_season), ("test", args.test_season)):
        sets[label] = {}
        for config in CONFIGS:
            path = d / f"sim554_replay_{season}_{config}.npz"
            if path.exists():
                sets[label][config] = _load(path)
        if "neutral" not in sets[label]:
            raise RuntimeError(f"no finished neutral pass for {season} in {d}")
        fps = {str(v["fingerprint"]) for v in sets[label].values()}
        if len(fps) != 1:
            raise RuntimeError(f"the {season} passes scored different pitches: {fps}")

    def keep_mask(label: str) -> np.ndarray:
        """Pitches every base setting scored for every design it scores."""
        k = None
        for config, z in sets[label].items():
            for dn in CONFIG_DESIGNS[config]:
                ok = np.isfinite(z["res"][:, D_IDX[dn], :, S_REAL]).all(axis=1)
                k = ok if k is None else (k & ok)
        assert k is not None
        return k & (sets[label]["neutral"]["cls"] < OTHER)

    keep = {label: keep_mask(label) for label in sets}
    out: dict[str, Any] = {
        "tune_season": args.tune_season,
        "test_season": args.test_season,
        "levels": [list(map(float, lv)) for lv in LEVELS],
        "configs": CONFIGS,
        "pitches_tune": int(keep["tune"].sum()),
        "pitches_test": int(keep["test"].sum()),
        "rule": [
            "The score that decides is the Brier score of the whole table: the odds each "
            "design gave to (the pitch's result) x (no steal / stolen base / caught stealing), "
            "against what happened. Lower is better. It is bounded, so a design that says "
            "'impossible' for something that then happens pays a fixed price.",
            "1. Designs are compared inside the family of their pitch draw, against that "
            "family's own baseline (today's separate steal draw). The question here is the "
            "running game; which pitch draw to use is the weight-fitting ticket's question.",
            "2. Production runs the two-step pitch draw, so that family's winner is the pick. "
            "A difference of two standard errors or less is a tie.",
            "3. A tie goes to the design whose per-runner expected steals track the real "
            "ones best (a gap in that correlation of two standard errors or less is again "
            "a tie), then to the simpler build, in this order: "
            + " < ".join(BUILD_ORDER["split"])
            + ".",
            "Each design's strength is the one with the lowest whole-table score on the "
            "tuning season.",
        ],
        "sections": {},
    }

    def wmean(x: np.ndarray, m: dict[str, np.ndarray], k: np.ndarray) -> float:
        wt = m["weight"][k].astype(np.float64)
        return float((wt * x).sum() / wt.sum())

    def family(configs: tuple[str, ...], fam: str) -> dict[str, Any]:
        """One family at one group of base settings: each design's strength
        picked on the tune season, then scored on the test season."""
        sec: dict[str, Any] = {"family": fam, "title": FAMILY_TITLE[fam], "designs": {}}
        avail = [c for c in configs if c in sets["tune"] and c in sets["test"]]
        if not avail:
            return sec
        zm = sets["test"][avail[0]]
        ztm = sets["tune"][avail[0]]
        chosen: dict[str, tuple[str, int]] = {}
        for dn in FAMILIES[fam]:
            grid = []
            for config in avail:
                if dn not in CONFIG_DESIGNS[config]:
                    continue
                zt = sets["tune"][config]
                for li in range(len(LEVELS)):
                    pt = _per_pitch(zt["res"][:, D_IDX[dn], li], ztm, keep["tune"])
                    grid.append((wmean(pt["brier_joint"], ztm, keep["tune"]), config, li))
            if not grid:
                continue
            grid.sort()
            _b, cfg, li = grid[0]
            chosen[dn] = (cfg, li)
            r = sets["test"][cfg]["res"][:, D_IDX[dn], li]
            sec["designs"][dn] = {
                "config": cfg,
                "level": _lv(li),
                "tune_grid": [
                    {"config": c_, "level": _lv(l_), "whole_table": b_} for b_, c_, l_ in grid
                ],
                "test": _metrics(r, zm, keep["test"]),
                "test_by_batter": _tier_table(r, zm, keep["test"], "btier"),
                "test_by_runner": _tier_table(r, zm, keep["test"], "rtier"),
                "test_runners": _runner_read(r, zm, keep["test"]),
                "test_all_levels": {},
            }
            for c_ in avail:
                if dn not in CONFIG_DESIGNS[c_]:
                    continue
                for l_ in range(len(LEVELS)):
                    rl = sets["test"][c_]["res"][:, D_IDX[dn], l_]
                    ml = _metrics(rl, zm, keep["test"])
                    sec["designs"][dn]["test_all_levels"][f"{c_}:{_lv(l_)}"] = {
                        "whole_table": ml["brier_joint"],
                        "how_many_steals": ml["brier_steal"],
                        "steals_per_100_predicted": ml["steals_per_100_predicted"],
                        "runner_correlation": _runner_read(rl, zm, keep["test"]).get("correlation"),
                    }
        base_name = BASELINE[fam]
        if base_name not in chosen:
            return sec
        per: dict[str, dict[str, Any]] = {}
        for dn, (cfg, li) in chosen.items():
            per[dn] = _per_pitch(sets["test"][cfg]["res"][:, D_IDX[dn], li], zm, keep["test"])
        base = per[base_name]
        for dn in chosen:
            e = sec["designs"][dn]
            for name in ("joint", "result", "steal", "running"):
                e[f"vs_baseline_{name}"] = _paired(
                    per[dn][f"brier_{name}"], base[f"brier_{name}"], zm, keep["test"], rng
                )
        ranked = sorted(chosen, key=lambda dn: sec["designs"][dn]["test"]["brier_joint"])
        sec["ranking"] = ranked
        best = ranked[0]
        tied = [best]
        for dn in ranked[1:]:
            pd = _paired(per[dn]["brier_joint"], per[best]["brier_joint"], zm, keep["test"], rng)
            sec["designs"][dn]["vs_best"] = pd
            if pd["difference"] <= 2.0 * pd["standard_error"]:
                tied.append(dn)
        sec["tied_with_the_best"] = tied

        # the tie-break: who steals, then the simpler build
        rates = {
            dn: _runner_rates(
                sets["test"][chosen[dn][0]]["res"][:, D_IDX[dn], chosen[dn][1]], zm, keep["test"]
            )
            for dn in tied
        }

        def corr_of(dn: str) -> float:
            c = sec["designs"][dn]["test_runners"].get("correlation")
            return -1.0 if c is None else float(c)

        top = max(tied, key=corr_of)
        level = [top]
        for dn in tied:
            if dn == top:
                continue
            gap = _corr_gap(rates[top][0], rates[top][1], rates[dn][1], rng)
            sec["designs"][dn]["runner_correlation_gap_to_the_best"] = gap
            if gap is None or gap["difference"] <= 2.0 * gap["standard_error"]:
                level.append(dn)
        sec["tied_on_who_steals"] = level
        sec["pick"] = min(level, key=BUILD_ORDER[fam].index)
        return sec

    groups = [("neutral", ("neutral",), "AT TODAY'S NEUTRAL WEIGHTS (what production runs)")]
    fit = tuple(c for c in ("split_fit", "single_fit") if c in sets["test"] and c in sets["tune"])
    if fit:
        groups.append(("fitted", fit, "AT THE EARLIER FITTED PITCHER AND BATTER WEIGHTS"))
    for key, configs, title in groups:
        out["sections"][key] = {
            "title": title,
            "families": {fam: family(configs, fam) for fam in FAMILIES},
        }
    if out["sections"]["neutral"]["families"]["split"].get("pick"):
        out["pick"] = out["sections"]["neutral"]["families"]["split"]["pick"]

    # ---- the text report
    print(
        f"THE RUNNING-GAME REPLAY — each design's strength picked on {args.tune_season} "
        f"({out['pitches_tune']} pitches), scored on {args.test_season} ({out['pitches_test']} pitches)"
    )
    print()
    print("THE RULE")
    for line in out["rule"]:
        print("  " + line)
    for group in out["sections"].values():
        print()
        print(f"=================== {group['title']} ===================")
        for sec in group["families"].values():
            if not sec.get("ranking"):
                continue
            order = sec["ranking"]
            bname = BASELINE[sec["family"]]
            print()
            print(f"--- {sec['title']}; baseline = {bname} ---")
            any_t = sec["designs"][order[0]]["test"]
            print(
                f"    {any_t['steals']} recorded steals, "
                f"{any_t['steals_per_100_real']:.3f} per 100 pitches"
            )
            print(
                f"{'design':24s} {'strength':18s} {'whole table':>12s} {'vs baseline x1e4':>18s} "
                f"{'of which result':>17s} {'how many steals':>17s} {'steal | result':>17s} "
                f"{'log 1e-3':>9s} {'log 1e-6':>9s} {'said impossible':>16s}"
            )
            for dn in order:
                e = sec["designs"][dn]
                t = e["test"]
                vj, vr = e["vs_baseline_joint"], e["vs_baseline_result"]
                vs, vg = e["vs_baseline_steal"], e["vs_baseline_running"]
                imp = t["said_impossible_share_of_steals"]
                print(
                    f"{dn:24s} {e['config'] + ' ' + e['level']:18s} {t['brier_joint']:12.6f} "
                    f"{1e4 * vj['difference']:+9.2f}±{1e4 * vj['standard_error']:<7.2f} "
                    f"{1e4 * vr['difference']:+9.2f}±{1e4 * vr['standard_error']:<6.2f} "
                    f"{1e4 * vs['difference']:+9.2f}±{1e4 * vs['standard_error']:<6.2f} "
                    f"{1e4 * vg['difference']:+9.2f}±{1e4 * vg['standard_error']:<6.2f} "
                    f"{t['log_floor_1e3']:9.5f} {t['log_floor_1e6']:9.5f} "
                    f"{100.0 * (imp or 0.0):14.1f}%"
                )
            print("    (Brier scores, lower is better; differences against the baseline x 10,000.")
            print(
                "     'of which result' = the pitch's result alone; 'how many steals' = no steal /"
            )
            print(
                "     stolen base / caught before the result is known; 'steal | result' = the same"
            )
            print("     given the real result; 'said impossible' = real steals given no chance)")
            print()
            print(
                "  WHO STEALS — steals per 100 pitches, real / predicted, by runner "
                "(1 seldom, 3 most)"
            )
            for dn in order:
                e = sec["designs"][dn]
                line = "  ".join(
                    f"t{r['tier']} {r['steals_per_100_real']:.2f}/{r['steals_per_100_predicted']:.2f}"
                    for r in e["test_by_runner"]
                )
                rr = e["test_runners"]
                c = rr.get("correlation")
                sl = rr.get("slope_real_on_predicted")
                print(
                    f"    {dn:24s} {line}   {rr.get('runners')} runners: corr "
                    f"{'n/a' if c is None else f'{c:.3f}'}, "
                    f"slope {'n/a' if sl is None else f'{sl:.2f}'}, "
                    f"error {rr.get('mean_abs_error_per_100', float('nan')):.2f} per 100"
                )
            print()
            print("  WHAT THE STEAL RIDES — by batter (1 contact, 3 swing-and-miss):")
            print(
                "  steals per 100 real/predicted; share of steals on a swing and miss real/predicted"
            )
            for dn in order:
                e = sec["designs"][dn]
                line = "  ".join(
                    f"t{r['tier']} {r['steals_per_100_real']:.2f}/{r['steals_per_100_predicted']:.2f} "
                    f"whiff {100 * (r['whiff_share_real'] or 0):.1f}/"
                    f"{100 * r['whiff_share_predicted']:.1f}%"
                    for r in e["test_by_batter"]
                )
                t = e["test"]
                print(
                    f"    {dn:24s} {line}   mix error {100 * t['steal_mix_distance']:.1f} pts; "
                    f"safe {100 * (t['safe_share_real'] or 0):.1f}/"
                    f"{100 * t['safe_share_predicted']:.1f}%"
                )
            mix = any_t["steal_mix_real"]
            print(
                "    real steals ride: "
                + ", ".join(f"{CLASSES[i]} {100 * mix[i]:.1f}%" for i in range(6))
            )
            print()
            print("  EVERY STRENGTH on the test season — the whole-table score; steals per 100")
            print("  predicted; the per-runner correlation; the 'how many steals' score x 10,000:")
            for dn in order:
                e = sec["designs"][dn]
                for k, v in e["test_all_levels"].items():
                    c = v["runner_correlation"]
                    print(
                        f"    {dn:24s} {k:18s} {v['whole_table']:.6f}  "
                        f"{v['steals_per_100_predicted']:6.3f}  "
                        f"{'   n/a' if c is None else f'{c:+.3f}'}  "
                        f"{1e4 * v['how_many_steals']:8.2f}"
                    )
            print()
            print(
                f"  TIED WITH THE BEST ON THE WHOLE TABLE: {', '.join(sec['tied_with_the_best'])}\n"
                f"  OF THOSE, TIED ON WHO STEALS: {', '.join(sec['tied_on_who_steals'])}\n"
                f"  THIS FAMILY'S PICK (the simpler build of those): {sec['pick']}"
            )
    if out.get("pick"):
        print()
        print(f"THE PICK (the two-step pitch draw, today's weights): {out['pick']}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, default=float)
        print(f"\nwrote {args.json_out}")
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def _build(args: argparse.Namespace) -> tuple[Any, RunningData, dict[str, float]]:
    from sim_stats import _FACTORY, _resolve, open_sim_duckdb, sim_kwargs_from_state

    from simulation.batch_runner import GameSpec
    from simulation.production_factory import production_machine_factory

    duck = open_sim_duckdb()
    if duck is None:
        raise RuntimeError("the sim DuckDB could not be opened read-only (a writer holds it?)")
    try:
        state = asyncio.run(_resolve(args.game_pk, duck))
        kw = sim_kwargs_from_state(state)
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        fp = machine.full_pool_sampler
        if not fp.pitch_cell_index:
            raise RuntimeError("the pitch cell index is off; the replay needs the production draw")
        production = {k: float(getattr(fp, k)) for k in POWER_ATTRS}
        art_dir = os.path.join(
            os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool"), "engine_artifacts"
        )
        data = RunningData(fp, duck, art_dir)
    finally:
        duck.close()
    print(f"production powers: {production}; steal facts joined in {data.seconds:.1f} s")
    for hand, c in data.coverage.items():
        print(f"  pitch pool [{hand}]: {c}")
    for tb, sp in fp.a.steal_pools.items():
        print(
            f"  steal pool [{tb}] in memory: {sp.n} rows, {int(np.asarray(sp.attempted).sum())} attempts"
        )
    return fp, data, production


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="mode", required=True)
    pp = sub.add_parser("pass", help="score one season's pitches (resumable)")
    pp.add_argument("--season", type=int, required=True)
    pp.add_argument("--games", type=int, default=100000, help="pitcher-games sampled")
    pp.add_argument("--min-opp", type=int, default=1, help="opportunity pitches a game needs")
    pp.add_argument("--nonatt-every", type=int, default=3, help="1 in N non-steal pitches scored")
    pp.add_argument("--configs", default="neutral,split_fit,single_fit")
    pp.add_argument("--k", type=int, default=8, help="anchors of the pitch step per pitch")
    pp.add_argument("--k-att", type=int, default=4, help="running-pitch anchors per pitch")
    pp.add_argument("--seed", type=int, default=554)
    pp.add_argument("--limit", type=int, default=0, help="score only the first N pitches (smoke)")
    pp.add_argument("--ckpt-every", type=int, default=5000)
    pp.add_argument("--progress-every", type=int, default=5000)
    pp.add_argument("--game-pk", type=int, default=744795, help="any resolvable game")
    pp.add_argument("--out", default="/app/scripts/sim554_replay_{season}_{config}.npz")
    pr = sub.add_parser(
        "report", help="pick each design's strength on one season, score on another"
    )
    pr.add_argument("--tune-season", type=int, required=True)
    pr.add_argument("--test-season", type=int, required=True)
    pr.add_argument("--dir", default="/app/scripts")
    pr.add_argument("--seed", type=int, default=554)
    pr.add_argument("--json-out", default=None)
    pr.add_argument("--text-out", default=None, help="also write the printed report here (UTF-8)")
    args = ap.parse_args(argv)
    if args.mode == "report":
        if not args.text_out:
            return report(args)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = report(args)
        text = buf.getvalue()
        print(text, end="")
        Path(args.text_out).write_text(text, encoding="utf-8")
        return rc

    fp, data, production = _build(args)
    rng = np.random.default_rng(args.seed + args.season)
    t0 = time.time()
    recs = sample_pitches(data, args.season, args.games, args.min_opp, args.nonatt_every, rng)
    if args.limit:
        recs = recs[: args.limit]
    n_st = sum(1 for r in recs if r["s"] > 0)
    print(
        f"{len(recs)} pitches of {args.season} ({n_st} recorded steals) in "
        f"{len({r['group'] for r in recs})} pitcher-games; sampled in {time.time() - t0:.1f} s",
        flush=True,
    )
    for config in [c.strip() for c in args.configs.split(",") if c.strip()]:
        if config not in CONFIGS:
            raise SystemExit(f"unknown config {config!r}")
        out_path = Path(args.out.format(season=args.season, config=config))
        run_pass(fp, data, recs, config, production, args, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
