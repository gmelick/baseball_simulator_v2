#!/usr/bin/env python
"""
scripts/clv_backtest.py
=======================
SIM-538 — the **sim-vs-closing-line accuracy comparison**: for every completed
game with a closing betting price, compare the simulator's own probability and
the market's (de-vigged) closing probability against what actually happened,
scored with a PROPER SCORING RULE (Brier score, log loss). This is the
platform's gold-standard validation now — see the reasoning below.

Also still produced, as a SECONDARY, clearly-labeled diagnostic: the SIM-429
Closing Line Value (CLV) scoreboard this script used to lead with (entry price
vs. closing price, no real outcome involved at all).

WHY THE HEADLINE METRIC CHANGED (owner decision, 2026-09-10)
--------------------------------------------------------------
CLV compares the price you would have ENTERED at to the price at CLOSE — never
touching the real outcome or the simulator's own probability. Two problems: the
simulator cannot produce a probability before both starting lineups are
announced, which is AFTER betting lines first open, so "entry vs. close" was
never comparing the model against a moment it could have acted at; and the odds
data collected here has a closing line for most games but not always a matched
opening line, so a method needing only the closing line uses far more of what
is already collected. The replacement, precisely:

  1. take the simulator's own probability for an outcome (already produced; no
     new math);
  2. take the closing line's probability for the SAME outcome, with the
     bookmaker's margin removed (de-vig — the math already exists, see below);
  3. score BOTH against what actually happened, with a PROPER SCORING RULE — one
     where a forecaster cannot improve their score by hedging toward a "safe"
     prediction, only by being more accurate (Brier score, log loss);
  4. because both scores come from the SAME game and the SAME real outcome,
     compare them PAIRED (sim's score minus the market's score, per game/bet),
     which needs a smaller sample to detect a real difference than comparing two
     unrelated averages would.

A model that beats the market under this test cannot get there by being bland —
unlike a "does the sim's overall rate look realistic" band, mirroring the pool
average scores no better than the market's own price under a proper scoring
rule. See docs/audit/2026-09-10-sim536-541-market-accuracy-plan.md for the full
plan (SIM-536 through SIM-541).

WHAT THIS DELIBERATELY DOES NOT CLAIM
--------------------------------------
That an edge was CAPTURABLE — that there was a real moment to transact at a
worse price than the model implied. Beating the closing line's accuracy is
NECESSARY for a real edge to exist, but proving it was bankable needs a live
price snapshot at the moment the model can actually act (right after lineups
post), which this platform does not collect today. SIM-540 (a companion
hypothetical-return report) and SIM-539 (real statistical confidence) build on
this without needing that snapshot.

That the point-in-time cutoff (see below) makes a backtest game fully
untouched by later data. The cutoff bounds which PLAY-POOL ROWS the sampler
can draw — it does not rebuild the pitcher/batter/fielder/catcher/manager
skill profiles that WEIGHT those draws as of the game's own date. Those
profiles come from one engine-artifact bundle built as of a single date for
the whole backtest run, so a January game can still be weighted by a
player's full-season (or later) form even though the literal plays it can
draw are correctly date-bounded. Building a fresh profile bundle per game
date is not feasible at backtest scale (SIM-537 shows what ONE such rebuild
costs); closing this residual needs either date-bucketed profile snapshots
or accepting it as a documented, softer form of look-ahead. An adversarial
review of SIM-538 confirmed this gap; it is not closed by this ticket.

That the confidence interval on a bucket mixing several markets or players
from the SAME game (the "overall" row; a prop bucket like K or H, which can
carry several players from one start) describes as many independent trials
as it has rows. Those rows share one game's park, bullpen, and script, so
:func:`_bootstrap_paired_diff_ci` resamples GAMES, not individual rows (see
its docstring) — the interval is honest about the real number of
independent games behind it, but a bucket built mostly from a few busy
games still carries less information than the same row count spread across
that many separate games would.

MINIMUM SAMPLE SIZE (SIM-539)
------------------------------
A small sample can look like real skill by pure chance. This section adds a
STATED floor: the smallest number of observations a market needs before its
result should be trusted at all — for BOTH reports in this file, the new
accuracy comparison above and the legacy CLV scoreboard below.

THE METHOD, IN PLAIN WORDS
  1. Pick the smallest edge worth caring about. This platform already has
     one: ``betting.bet_signal.DEFAULT_MIN_EDGE`` (0.02) — the existing
     floor the platform already uses to decide "is this edge big enough to
     act on." The legacy scoreboard's beat-close rate is a probability, the
     SAME scale ``DEFAULT_MIN_EDGE`` is on, so no conversion is needed
     there. The accuracy comparison's Brier/log-loss paired difference is
     NOT on a probability scale, so this file uses a first-order
     approximation instead: near a coin-flip market (the common case for a
     moneyline), a probability shift of size ``e`` changes one
     observation's Brier score by ABOUT ``e`` — so the SAME 0.02 floor is
     reused there too, clearly flagged as approximate (see "WHAT THIS DOES
     NOT CLAIM" below).
  2. Shrink that floor by ``tests/acceptance/bands.py``'s own
     ``DETECTION_MARGIN`` (1.6) — the SAME constant that file already uses.
     Reusing the CONSTANT is not the same as reusing its power guarantee:
     bands.py's own "99.2% power" figure for this margin holds only at that
     file's fixed Z = 4.0 (chosen for a 13-channel NIGHTLY gate's own
     false-alarm budget), and this file's z varies per row instead (step 3
     below) — see :data:`DETECTION_MARGIN`'s own comment for the corrected
     power figures. What is shared is the CONSTANT and the reasoning
     ("shrink the floor so a real edge clears the minimum with room to
     spare"), not a borrowed number.
  3. Pick a significance level: 0.05 two-sided, Bonferroni-corrected by
     however many market rows appear in THIS report — the standard fix for
     scanning many rows and mistaking one lucky one for a real signal.
     ``tests/acceptance/bands.py`` corrects the same way for its own 13
     channels; this file corrects for however many rows a given run
     actually produces, since ``--markets`` can narrow that count. The
     "overall" row uses the UNCORRECTED level instead — it is one top-line
     figure, not one of several rows a reader scans for "which one looks
     good" (see :func:`aggregate_scoreboard` / :func:`aggregate_accuracy_comparison`).
  4. Solve for the observation count that makes THIS market's OWN measured
     spread small enough to clear the shrunk floor at that confidence
     level — :func:`_minimum_paired_observations` is the plain formula;
     :func:`_minimum_observations_clustered` is the version BOTH reports
     actually call, which first groups observations by ``game_pk`` and
     backs out a GAME-CLUSTERED spread (:func:`_clustered_se`) instead of a
     naive per-row one. A noisier market needs more games. A market whose
     observations cluster inside a few games (several props from one
     start; three game markets off one score) needs MORE observations than
     the same count spread across separate games, and a market backed by
     FEWER THAN :data:`MIN_CLUSTERS_FOR_INFERENCE` distinct games is
     treated as undetermined, not resolved — see that constant's own
     comment for why 2 clusters (the bare minimum the formula can compute
     a nonzero spread from at all) is not the same as enough clusters to
     TRUST it.

WHAT THIS DOES NOT CLAIM
  That 0.02 is the right edge to care about for Brier score. It is a
  provisional, honestly-approximate stand-in, chosen because no measured
  real Brier-difference magnitude exists yet for this platform to anchor
  on — unlike ``tests/acceptance/bands.py``'s box-score floors, which ARE
  anchored in real, measured MLB defect magnitudes (SIM-494/495/496).
  Once enough real backtests accumulate, replace this floor with one
  measured the same way — the maturing path every other floor on this
  platform has already taken.

  That the "near a coin-flip market" approximation holds equally well for
  every market. A probability shift of size ``e`` changes one
  observation's Brier score by close to ``e`` only near p = 0.5; at a
  probability far from a coin flip (a home-run prop, typically well under
  0.5) the true change is smaller, by a factor of roughly ``4 * p * (1-p)``
  — at ``p`` = 0.15 that is about half of ``e``, not all of it. This file
  applies the SAME 0.02 floor to every market rather than a per-market
  correction, so the power this floor buys is weaker than stated for a
  market whose typical probability sits far from 0.5. An adversarial
  review of SIM-539 confirmed this; it is not fixed here.

  That :data:`MIN_CLUSTERS_FOR_INFERENCE` clusters certifies a row as
  reliable. It is the floor below which a read is refused outright, not a
  claim that clearing it makes the cluster-robust estimate fully trustworthy
  — see that constant's own comment. A row can also read as fully resolved
  (zero remaining uncertainty) from a SMALL sample whose observed spread
  happens to be exactly zero by chance, since the formula trusts the
  observed spread at face value rather than an uncertainty-adjusted upper
  bound on it; this is a known, unfixed limitation an adversarial review of
  SIM-539 surfaced, most likely at low observation counts just above the
  cluster floor.

  That the Bonferroni correction (step 3) protects a reader who reruns this
  backtest across many seasons or slates and checks the "overall" row each
  time for significance. It corrects only for the rows scanned WITHIN one
  run; repeated runs are a form of repeated testing this correction does
  not address.

  That the reported minimum is in GAMES. The accuracy comparison's
  ``brier_min_n`` / ``log_loss_min_n`` / ``min_n_recommended`` count paired
  OBSERVATIONS (a prop bucket can carry several players' records from one
  game), not distinct games — see :class:`AccuracyComparisonRow`'s own
  ``n_games`` field for the actual game count backing a row, and read the
  two together rather than assuming the minimum is a game count.

HOW IT REUSES THE EXISTING SEAMS (no re-invention)
--------------------------------------------------
  * **Sim run + game summary + win prob** — the SAME machinery the API / batch
    runner use: ``_resolve_state_or_error`` resolves a game's lineup into a
    GameState; ``record_game_plays`` (the ``api/routes/betting.py`` /
    ``scripts/validate_props.py`` path) replays N iterations under the production
    factory ref, collecting one ``GameSimResult`` per iteration; those build a
    ``GameSimSummary`` (carrying the per-iteration ``home_scores`` /
    ``away_scores`` / ``total_scores`` arrays) and, via
    ``simulation.win_probability.win_probability``, a ``WinProbability``.
  * **Per-player prop PMFs** — ``PropDistributionSet.from_results`` over the
    per-iteration boxscores (exactly the ``validate_props`` path); each
    ``PropDistribution`` answers ``p_over(line)`` / ``p_under(line)``.
  * **De-vig + edge math** — ``betting.clv_engine``: ``moneyline_edge_report`` /
    ``total_over_under_edge_report`` / ``run_line_edge_report`` / ``prop_edge_report``
    already de-vig a two-way market and expose the resulting fair probability
    (``EdgeReport.market_fair_prob``) alongside the sim's own
    (``EdgeReport.sim_prob``). SIM-538 calls these with the CLOSING quote fed in
    as the market (see :func:`score_game_accuracy` / :func:`score_prop_accuracy`)
    and a FIXED reference side per market (home / over), so the comparison never
    depends on which side the model would have bet — that selection is what the
    legacy CLV step still does, and mixing it into the accuracy read would bias
    it toward games the model disagreed with the market on.
  * **Proper scoring rules** — ``simulation.prop_validation``: ``binary_brier`` /
    ``binary_log_loss`` take a probability array and a 0/1 outcome array; SIM-538
    calls them twice per market — once with the sim's probabilities, once with
    the market's — over the SAME paired outcomes.
  * **Real outcomes** — game markets read ``raw.games.home_score_final`` /
    ``away_score_final`` (already read elsewhere in this file). Props reuse
    ``simulation.prop_validation.real_props_from_pa_events`` — the SAME
    ``raw.pitches.events``-derived ground truth ``validate_props.py`` uses,
    covering H/HR/TB (batter) and K/BB (pitcher) only; RBI/ER have no reliable
    per-PA-event ground truth and are not scored here for the same reason
    ``validate_props.py`` excludes them.
  * **Odds I/O** — read directly from Postgres (``raw.game_odds`` /
    ``raw.prop_odds``). The accuracy comparison reads ONLY the closing row
    (:func:`_closing_prices`) — a game needs no matched opening line to be
    scored, unlike the legacy CLV step.
  * **Park factor (SIM-452)** — ``simulation.sim_kwargs.resolve_park_factor_onto_state``
    resolves the venue run park factor onto the GameState before the replay. The
    run REFUSES to start when the sim DuckDB will not open (exit code 2), because
    a neutral 1.0 is indistinguishable from a real neutral park.
  * **Point-in-time cutoff (SIM-538 fix)** — every replay here now also resolves
    ``simulation.sim_kwargs.resolve_asof_ymd`` onto the GameState
    (``state.asof_ymd``), which ``sim_kwargs_from_state`` carries into the
    simulator as ``_asof_ymd``. Before this fix, this script built sim kwargs
    from the state directly and never resolved a cutoff at all — despite an
    OLDER version of this docstring claiming it used
    ``simulation.sim_kwargs.build_sim_kwargs`` (which does resolve one). A
    backtest of a past game was free to draw on pool rows and player profiles
    dated AFTER that game, the most direct leak available and one that makes a
    backtest look better than the simulator anyone will ever bet on. SIM-534/535/537
    (landed 2026-09-10/11) built the cutoff mechanism end to end; this was the
    one caller that never asked for it.
  * **Calibration (SIM-538 fix)** — win probability is now scored through the
    SAME fitted reliability curve the live API applies
    (``api.state.load_calibration_report`` + ``CalibrationMap.from_report``,
    falling back to the identity map when no report is configured — the exact
    pattern ``api/main.py``'s boot lifespan uses). The moneyline accuracy
    comparison would otherwise score a probability production never actually
    serves.

THE MARKETS
-----------
  * Game: moneyline (home/away ML), total (over/under at ``total_line``), run-line
    (home/away at ``home_spread``).
  * Props (the SIM-134 7-market vocab; odds ``prop_stat`` → model
    ``PropDistribution`` stat): strikeouts→K, walks→BB, earned_runs→ER, hits→H,
    home_runs→HR, total_bases→TB, rbis→RBI. The accuracy comparison scores only
    the five with a real-outcome source (K, BB, H, HR, TB); the legacy CLV step
    still scores all seven, since it never needed a real outcome.

PURE vs. IMPACTFUL
------------------
The per-bet CLV decision (:func:`evaluate_two_way_market`), the accuracy-record
builders (:func:`score_game_accuracy` / :func:`score_prop_accuracy`), and both
aggregations (:func:`aggregate_scoreboard` / :func:`aggregate_accuracy_comparison`)
are PURE — no DB, no sim; the accuracy aggregation's confidence interval uses a
SEEDED bootstrap (deterministic given the seed, still unit-testable on synthetic
inputs). Everything DB/sim-touching lives in the ``_fetch_*`` readers and
:func:`run`. The trust labels (:data:`MARKET_TRUST`) and the prop vocab
(:data:`PROP_VOCAB_MAP`) are plain data.

ACROSS-GAMES PARALLELISM
------------------------
A full-season backtest is feasible because the slate is fanned out OVER GAMES, not
over a single game's iterations: per-game cost is the irreducible per-PA full-pool
scoring (~1.5 s/iter) and the host is core-bound (~6 cores), so one game can't go
below ~30 s — but ~6 GAMES AT ONCE gives ~6× throughput. ``--workers N`` (default 6)
maps each WHOLE game onto a ``forkserver`` ``ProcessPoolExecutor`` worker that runs
it serially (resolve → N sims → prop dists → read odds → bet + accuracy records);
each worker holds only its own ~373 MB full-pool sampler cache (SIM-430), so 6
workers fit in ~2.2 GB and the parent stays lean (loads NO engine artifacts).
``--workers 1`` is the SERIAL in-process fallback (the no-pool debug mode + the
byte-identical-verify reference). The run is byte-identical to serial for the same
(game set, base_seed, iterations): each game is independent + deterministic from
its per-iteration seed, so the only thing parallelism changes — completion order —
never affects any record.

USAGE
-----
    # In the app container (Postgres at db:5432, DuckDB at /data/...):
    python scripts/clv_backtest.py --seasons 2024 --max-games 50 --iterations 100
    python scripts/clv_backtest.py --seasons 2023 2024 --markets game
    python scripts/clv_backtest.py --seasons 2024 --markets props --min-edge 0.02
    # full season, 6 games at once (the across-games parallel mode):
    python scripts/clv_backtest.py --seasons 2024 --workers 6 --iterations 100
    # serial fallback (no pool — debug / byte-identical verify reference):
    python scripts/clv_backtest.py --seasons 2024 --max-games 2 --workers 1
    # legacy CLV report only, skip the new accuracy comparison (faster smoke run):
    python scripts/clv_backtest.py --seasons 2024 --max-games 2 --no-accuracy-comparison
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
from scipy.stats import norm

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from betting.bet_signal import DEFAULT_MIN_EDGE  # noqa: E402
from betting.clv_engine import (  # noqa: E402
    EdgeReport,
    MarketSide,
    OddsQuote,
    TwoWayMarket,
    clv_from_odds,
    moneyline_edge_report,
    prop_edge_report,
    run_line_edge_report,
    total_over_under_edge_report,
)
from simulation.prop_validation import (  # noqa: E402
    OUTCOME_PROB_EPS,
    binary_brier,
    binary_log_loss,
    real_props_from_pa_events,
)
from simulation.win_probability import IDENTITY_CALIBRATION, CalibrationMap  # noqa: E402

log = logging.getLogger("clv_backtest")

DEFAULT_DSN = os.environ.get(
    "BASEBALL_DB_DSN",
    "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim",
)
DEFAULT_DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
DEFAULT_OUTPUT = os.environ.get("CLV_BACKTEST_PATH", "/data/clv_backtest.json")
#: SIM-538: mirrors validate_props.py's DEFAULT_CALIBRATION_PATH — the SAME
#: fitted reliability curve the live API applies at boot.
DEFAULT_CALIBRATION_PATH = os.environ.get("CALIBRATION_REPORT_PATH", "/data/calibration.json")
#: SIM-538: bootstrap resamples for the paired-difference confidence interval
#: (see aggregate_accuracy_comparison). 2000 is the conventional floor for a
#: percentile bootstrap; the CLI can raise it for a slower, tighter interval.
DEFAULT_BOOTSTRAP_SAMPLES = 2000

#: SIM-539: the smallest edge worth caring about, for BOTH the beat-close-rate
#: test (an exact match — both live on a probability scale) and the Brier/
#: log-loss paired-difference test (an honest approximation — see the module
#: docstring's "MINIMUM SAMPLE SIZE" section). Reuses the platform's OWN
#: existing "is this edge big enough to act on" floor rather than inventing a
#: second one.
MIN_DETECTABLE_EDGE: float = DEFAULT_MIN_EDGE

#: SIM-539: the SAME detection-margin CONSTANT tests/acceptance/bands.py
#: already uses (its own "HOW EACH FLOOR IS CHOSEN" section) — shrinking the
#: floor by this factor gives a real edge of exactly MIN_DETECTABLE_EDGE
#: better odds of clearing the minimum than an un-shrunk floor would.
#:
#: An adversarial review of SIM-539 confirmed a claim once made here was
#: WRONG: bands.py's own "99.2% power" figure for this exact margin holds
#: ONLY at bands.py's own fixed Z = 4.0 (chosen there for a 13-channel
#: NIGHTLY gate's false-alarm budget — see that file's "WHY Z = 4.0"). This
#: file's z varies per row (_z_two_sided(alpha), alpha either the
#: uncorrected DEFAULT_ALPHA or a Bonferroni correction — see
#: _bonferroni_alpha), so the power this margin actually buys HERE is
#: smaller and ROW-DEPENDENT: about 88% for an uncorrected row (alpha=0.05)
#: rising toward (but not reaching) bands.py's 99.2% as more markets are
#: Bonferroni-corrected. Only the CONSTANT is shared, not the power
#: guarantee — do not requote "99.2%" for this file's own procedure.
DETECTION_MARGIN: float = 1.6

#: SIM-539: the two-sided significance level BEFORE the Bonferroni correction
#: (see _bonferroni_alpha) — the conventional default.
DEFAULT_ALPHA: float = 0.05

#: SIM-539: the fewest DISTINCT GAMES a bucket must have before its
#: game-clustered standard error / confidence interval / minimum sample
#: size is trusted at all. Below this, treat the read as undetermined —
#: see _clustered_se and _minimum_observations_clustered.
#:
#: Two clusters is the bare minimum the CR0 sandwich formula can even
#: compute a nonzero spread from (one cluster is mathematically degenerate
#: — always exactly 0.0). It is NOT, by itself, enough to TRUST that
#: formula: the cluster-robust-inference literature (e.g. Cameron & Miller's
#: practitioner's guide) documents that CR0 understates true uncertainty
#: with few clusters, and commonly wants dozens before treating it as
#: reliable. An adversarial review of SIM-539 confirmed this: with only 2-3
#: distinct games, an unlucky (or simply small) sample can read as a
#: perfectly resolved, zero-spread rate — the observed spread really is
#: zero, but a zero ESTIMATED from 2-3 points is weak evidence that the
#: TRUE spread is zero. 10 is a documented, round floor on the low end of
#: common guidance — chosen so the read stays USABLE at the slate sizes
#: this platform actually runs (a single day is a few dozen games), not a
#: claim that 10 clusters makes the sandwich estimator fully reliable. More
#: games is always better; a reader who wants that guarantee should look
#: for well into the dozens before treating a row as certified, not just
#: "not flagged."
MIN_CLUSTERS_FOR_INFERENCE: int = 10

#: The production machine factory (dotted ref) — the SAME one the API serves with.
_FACTORY_REF = "simulation.production_factory:production_machine_factory"

#: The two line_types every CLV computation needs (entry + close).
LINE_TYPES: tuple[str, ...] = ("opening", "closing")

# ---------------------------------------------------------------------------
# Exit codes (SIM-454). Every ABANDONED run leaves a distinct, documented code.
# A backtest that measured nothing must never exit 0: the whole point of this
# programme is that a silent no-op reads exactly like a healthy run.
# ---------------------------------------------------------------------------
#: Everything ran and at least one game was priced.
EXIT_OK = 0
#: The sim DuckDB would not open and the operator did not pass
#: ``--allow-neutral-parks`` (SIM-452 precondition).
EXIT_NO_PARK_SOURCE = 2
#: A worker could not build its event loop / Postgres pool / park-factor source.
EXIT_WORKER_INIT = 3
#: A game replayed park-blind — a defect in this script (SIM-452).
EXIT_UNRESOLVED_PARK = 4
#: The run finished but priced NOTHING: no games found, or every game landed in
#: ``no_odds`` / ``unresolved`` / ``empty``. The scoreboard is empty, so the run
#: measured nothing and must not look successful.
EXIT_NOTHING_SCORED = 5
#: The parent process could not reach Postgres at all.
EXIT_INFRASTRUCTURE = 6

#: ACROSS-GAMES default worker count. The host is ~6-core (SIM-430), and the
#: per-game cost is the irreducible per-PA full-pool scoring, so ~6 GAMES AT ONCE
#: is the throughput lever; each forkserver worker holds ~373 MB → ~2.2 GB total.
DEFAULT_WORKERS = 6

#: SIM-430: the pool's multiprocessing start method (mirrors
#: ``simulation.batch_runner``). ``forkserver`` forks workers from a lean ~30 MB
#: server so they do NOT COW-inherit any engine artifacts the parent might hold —
#: each worker loads its own ~373 MB bundle. Override via ``SIM_MP_START_METHOD``.
_MP_START_METHOD = os.environ.get("SIM_MP_START_METHOD", "forkserver").strip().lower()


def _pool_mp_context() -> Any:
    """Return the multiprocessing context for the across-games pool (SIM-430).

    Defaults to ``forkserver`` (workers fork from a lean server, never COW-inherit a
    big parent); falls back to the platform-default context when the requested
    method is unavailable (e.g. a host without forkserver). Mirrors
    :func:`simulation.batch_runner._pool_mp_context`.
    """
    import multiprocessing

    try:
        if _MP_START_METHOD in multiprocessing.get_all_start_methods():
            return multiprocessing.get_context(_MP_START_METHOD)
    except Exception:  # noqa: BLE001 — any oddity → platform default
        pass
    return multiprocessing.get_context()


# ===========================================================================
# Vocab: odds prop_stat -> model PropDistribution stat (the SIM-134 7 markets)
# ===========================================================================

#: Maps the ``raw.prop_odds.prop_stat`` vocabulary to the
#: ``simulation.prop_distributions`` model-prop names. Covers exactly the 7
#: markets the SIM-134 CHECK constraint enforces.
PROP_VOCAB_MAP: dict[str, str] = {
    "strikeouts": "K",
    "walks": "BB",
    "earned_runs": "ER",
    "hits": "H",
    "home_runs": "HR",
    "total_bases": "TB",
    "rbis": "RBI",
}

# ===========================================================================
# Trust labels: how much to trust each market's CLV (Betting-Analyst tiers)
# ===========================================================================

#: market key -> trust tier. The market key is the game market_type
#: ('moneyline'/'total'/'runline') OR the MODEL prop stat (K/BB/ER/H/HR/TB/RBI).
#: Tiers (per the §11 realism residual + SIM-429 over-prediction notes):
#:   trustworthy  — box rate stats within ~4% of MLB (H/HR/TB).
#:   loose        — moneyline (win-prob fit over a bounded sample).
#:   caution      — total/runline (the hits→runs conversion gap lives here). The
#:                  AUTHORITATIVE size of that gap is **runs ~7-8% low**
#:                  (CLAUDE.md:85). CLAUDE.md:465 still says "~10-12% low"; that
#:                  line predates the 2026-05-28 DP-rate fix and is STALE. Read
#:                  7-8% when you size a total/runline bias.
#:   untrustworthy— K/BB/ER/RBI (over-predicted props / not validated — SIM-429).
MARKET_TRUST: dict[str, str] = {
    # trustworthy
    "H": "trustworthy",
    "HR": "trustworthy",
    "TB": "trustworthy",
    # loose
    "moneyline": "loose",
    # caution
    "total": "caution",
    "runline": "caution",
    # untrustworthy
    "K": "untrustworthy",
    "BB": "untrustworthy",
    "ER": "untrustworthy",
    "RBI": "untrustworthy",
}


def trust_label(market: str) -> str:
    """The trust tier for a market key (the game market_type or a model prop stat).

    Unknown markets fall back to ``"unknown"`` rather than raising, so a new
    market never crashes the scoreboard.
    """
    return MARKET_TRUST.get(str(market), "unknown")


# ===========================================================================
# Per-bet record + the pure per-market CLV evaluation
# ===========================================================================


@dataclass(frozen=True, slots=True)
class BetRecord:
    """One scored row of the backtest: the bet the model PLACED on one market.

    ``placed`` is False for a "no-bet" row (no side had positive model edge >=
    the floor) — such a row counts toward ``n_markets_priced`` but not toward
    ``n_bets_placed`` / the beat-close rate. When ``placed`` is True, ``side`` /
    ``model_edge`` / ``model_ev`` describe the chosen side and ``clv_prob`` /
    ``beat_close`` carry its entry→close CLV.
    """

    game_pk: int
    #: Market key: 'moneyline'/'total'/'runline' OR a model prop stat (K/H/...).
    market: str
    #: Coarse group for aggregation: 'moneyline'/'total'/'runline'/'prop'.
    market_type: str
    placed: bool
    side: str | None = None
    line: float | None = None
    model_edge: float | None = None
    model_ev: float | None = None
    clv_prob: float | None = None
    beat_close: bool | None = None
    #: Optional player id (props only) for provenance.
    player_id: int | None = None

    def to_jsonable(self) -> dict[str, Any]:
        """A JSON-safe dict of this record (all fields are already plain types)."""
        return asdict(self)

    @classmethod
    def from_jsonable(cls, d: dict[str, Any]) -> BetRecord:
        """Rebuild a :class:`BetRecord` from its :meth:`to_jsonable` dict.

        The parent uses this to reconstruct the records a parallel worker returns as
        plain dicts (the picklable across-process payload), so the aggregation runs
        on the SAME :class:`BetRecord`s either execution mode produces.
        """
        return cls(**d)


@dataclass(frozen=True, slots=True)
class AccuracyRecord:
    """SIM-538: one paired (sim probability, market probability, real outcome)
    observation for one market/prop of one game.

    Unlike :class:`BetRecord`, there is no "placed" concept here — every market
    with a usable closing price and a real outcome contributes exactly one
    record, scored on a FIXED reference side (home / over), never the side the
    model would have bet. That fixed side is what keeps the comparison honest:
    picking whichever side the model liked best would only ever measure
    accuracy on the games the model disagreed with the market on, which is a
    biased sample.
    """

    game_pk: int
    #: Market key: 'moneyline'/'total'/'runline' OR a model prop stat (K/H/...).
    market: str
    #: Coarse group for aggregation: 'moneyline'/'total'/'runline'/'prop'.
    market_type: str
    #: The simulator's own probability of the reference event.
    sim_prob: float
    #: The closing line's probability of the SAME event, margin removed.
    market_prob: float
    #: 1 if the reference event happened, else 0. A push (the actual value
    #: landed exactly on the line) never becomes a record — see
    #: :func:`score_game_accuracy` / :func:`score_prop_accuracy`.
    outcome: int
    #: Optional player id (props only) for provenance.
    player_id: int | None = None

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_jsonable(cls, d: dict[str, Any]) -> AccuracyRecord:
        """Mirrors :meth:`BetRecord.from_jsonable` — rebuilds one record from
        the picklable dict a parallel worker returns."""
        return cls(**d)


@dataclass(frozen=True, slots=True)
class TwoWayPrices:
    """An opening + closing two-way American-odds quote for one market.

    ``side`` / ``other`` name the two legs the same way for opening and closing
    (e.g. (home_ml, away_ml) or (over_ml, under_ml)). All four prices must be
    present (non-None) for the market to be scoreable; ``line`` is the market line
    (None for a moneyline).
    """

    open_side: float
    open_other: float
    close_side: float
    close_other: float
    line: float | None = None


def _pick_side(
    report_side: EdgeReport,
    report_other: EdgeReport,
    *,
    min_edge: float,
) -> EdgeReport | None:
    """Pick the side the model would BET (the larger positive model edge >= floor).

    Both sides' edge reports are built on the OPENING quote. The model places a
    bet on whichever side has the greater model edge, provided that edge clears
    ``min_edge`` and is strictly positive; otherwise no bet (None) — a market the
    model passes on.
    """
    best = max((report_side, report_other), key=lambda r: r.edge)
    if best.edge > 0.0 and best.edge >= float(min_edge):
        return best
    return None


def evaluate_two_way_market(
    game_pk: int,
    market: str,
    market_type: str,
    side_a: MarketSide,
    side_b: MarketSide,
    report_for_side: Any,
    prices: TwoWayPrices,
    *,
    min_edge: float = 0.0,
    player_id: int | None = None,
) -> BetRecord:
    """PURE: pick the model's +EV side on the OPENING line and score its CLV.

    ``report_for_side(side, entry_market)`` is a callable that returns the
    :class:`EdgeReport` for ``side`` given a :class:`TwoWayMarket` built on the
    OPENING quote (the caller closes over the sim output — a ``WinProbability`` /
    ``GameSimSummary`` / ``PropDistribution`` — so this function stays free of any
    sim/DB dependency). The steps:

      1. build both sides' edge reports on the OPENING two-way quote;
      2. PICK the side with the larger positive model edge >= ``min_edge`` (else a
         'no-bet' record with ``placed=False``);
      3. for the placed side, compute ``clv = clv_from_odds(entry=opening price on
         that side, close=closing price on that side)`` and record
         ``clv_prob`` / ``beat_close = clv.clv_prob > 0``.

    Deterministic given the reports + prices. ``side_a`` is the leg whose OPENING
    price is ``prices.open_side``; ``side_b`` the other leg.
    """
    # The OPENING two-way market for each side (side price first, other second).
    entry_a = TwoWayMarket(
        side=side_a,
        entry=OddsQuote(side=prices.open_side, other=prices.open_other, line=prices.line),
    )
    entry_b = TwoWayMarket(
        side=side_b,
        entry=OddsQuote(side=prices.open_other, other=prices.open_side, line=prices.line),
    )
    report_a = report_for_side(side_a, entry_a)
    report_b = report_for_side(side_b, entry_b)

    chosen = _pick_side(report_a, report_b, min_edge=min_edge)
    if chosen is None:
        return BetRecord(
            game_pk=int(game_pk),
            market=market,
            market_type=market_type,
            placed=False,
            line=prices.line,
            player_id=player_id,
        )

    # Map the chosen side back to its opening/closing entry+other prices.
    if chosen.side is side_a:
        entry_side, entry_other = prices.open_side, prices.open_other
        close_side, close_other = prices.close_side, prices.close_other
    else:
        entry_side, entry_other = prices.open_other, prices.open_side
        close_side, close_other = prices.close_other, prices.close_side

    clv = clv_from_odds(
        entry_side_american=entry_side,
        entry_other_american=entry_other,
        close_side_american=close_side,
        close_other_american=close_other,
    )
    return BetRecord(
        game_pk=int(game_pk),
        market=market,
        market_type=market_type,
        placed=True,
        side=chosen.side.value,
        line=prices.line,
        model_edge=float(chosen.edge),
        model_ev=float(chosen.ev),
        clv_prob=float(clv.clv_prob),
        beat_close=bool(clv.clv_prob > 0.0),
        player_id=player_id,
    )


# ===========================================================================
# SIM-539: minimum sample size (PURE — shared by both reports below)
# ===========================================================================


def _json_safe(d: dict[str, Any]) -> dict[str, Any]:
    """Replace every non-finite float (``nan``, ``inf``, ``-inf``) in a
    flat dict with ``None`` (valid JSON ``null``) (SIM-539).

    ``nan`` / ``inf`` are legal Python floats but NOT legal JSON (RFC 8259);
    ``json.dumps`` still emits the bare tokens ``NaN`` / ``Infinity`` for
    them rather than raising, so a report built from an un-sanitized dict
    LOOKS fine until a strict downstream parser (a browser's
    ``JSON.parse``, most non-Python JSON libraries) rejects it. An
    adversarial review of SIM-539 confirmed this would break the JSON
    report on exactly the low-sample rows this ticket exists to flag
    (a degenerate ``clustered_se`` / an unresolved ``min_n``). Used by
    :meth:`ScoreboardRow.to_jsonable` and
    :meth:`AccuracyComparisonRow.to_jsonable`.
    """
    return {
        k: (None if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))) else v)
        for k, v in d.items()
    }


def _bonferroni_alpha(base_alpha: float, n_hypotheses: int) -> float:
    """Bonferroni-corrected two-sided significance level (SIM-539).

    Scanning ``n_hypotheses`` market rows for "which one looks good" inflates
    the chance ANY one of them reads significant by pure luck. Dividing the
    per-row significance level by the row count is the standard, conservative
    fix — the SAME correction tests/acceptance/bands.py applies for its own
    13 channels ("WHY Z = 4.0"), sized here to however many rows a given run
    actually produces. ``n_hypotheses < 1`` is treated as 1 (no correction).
    """
    n = max(1, int(n_hypotheses))
    return float(base_alpha) / n


def _z_two_sided(alpha: float) -> float:
    """The two-sided critical z-value for significance level ``alpha`` (SIM-539)."""
    return float(norm.ppf(1.0 - float(alpha) / 2.0))


def _minimum_paired_observations(sd: float, *, floor: float, alpha: float) -> float:
    """SIM-539: minimum paired observations to detect a mean difference of at
    least ``floor`` at two-sided significance ``alpha``, given this sample's
    OWN observed per-observation standard deviation ``sd``.

    The standard one-sample paired-difference sample-size formula —
    ``n = (z_alpha/2 * sd / floor) ** 2`` — the SAME shape
    ``tests/acceptance/bands.py``'s own "required observations = (Z * sd_ref
    / floor) ** 2" already uses for its box-score channels. This function is
    the plain formula, unaware of any margin; a caller that wants the SAME
    99.2%-power margin that file's Rule A applies passes an ALREADY-shrunk
    ``floor`` (``target_edge / DETECTION_MARGIN``).

    Returns ``math.inf`` when ``floor`` is non-positive (nothing could ever
    resolve an edge of zero or less); ``0.0`` when ``sd`` is exactly zero (a
    perfectly resolved population needs no more observations).
    """
    if float(floor) <= 0.0:
        return float("inf")
    if sd <= 0.0:
        return 0.0
    z = _z_two_sided(alpha)
    return float((z * float(sd) / float(floor)) ** 2)


def _clustered_se(values_by_game: dict[int, list[float]]) -> float:
    """Cluster-robust (by game_pk) standard error of the MEAN of arbitrary
    real-valued per-observation values (SIM-539) — 0/1 beat indicators for
    the legacy scoreboard, or Brier/log-loss paired differences for the
    accuracy comparison.

    Observations within one game share the same simulated score arrays /
    player PMFs, so they are CORRELATED — the naive i.i.d. standard error
    understates the true uncertainty of a pooled mean. This is the standard
    CR0 cluster-robust sandwich SE::

        SE = sqrt( sum_g ( sum_{i in g} (x_i - xbar) )^2 )  /  N

    For singleton clusters (one observation per game) it reduces to the
    ordinary i.i.d. SE; when many observations fall in the same game it
    widens, reflecting the real design effect. Returns 0.0 for an empty
    input.

    Mathematically DEGENERATE (always exactly 0.0) with FEWER THAN 2
    distinct clusters — a single game's own deviations from the overall
    mean sum to zero BY CONSTRUCTION, which would misreport "no
    uncertainty" for the LEAST resolved case there is (one independent
    trial backing the whole rate). This function does not guard that case
    itself (it is a plain, reusable formula); :func:`_minimum_observations_clustered`
    is the guarded caller-facing version, and :func:`_row_for` /
    :func:`_accuracy_row_for` apply the SAME guard before trusting this
    value for a confidence interval.
    """
    all_x = [float(x) for xs in values_by_game.values() for x in xs]
    n = len(all_x)
    if n == 0:
        return 0.0
    xbar = sum(all_x) / n
    total = 0.0
    for xs in values_by_game.values():
        g = sum((float(x) - xbar) for x in xs)
        total += g * g
    return float(total**0.5 / n)


def _minimum_observations_clustered(
    values_by_game: dict[int, list[float]], *, floor: float, alpha: float
) -> float | None:
    """SIM-539: minimum total observations to trust a mean that differs from
    its reference by ``floor``, at significance ``alpha`` — accounting for
    THIS market's own observed game-level clustering.

    Backs out the implied per-observation standard deviation
    (``_clustered_se(values_by_game) * sqrt(n)``) and feeds it through
    :func:`_minimum_paired_observations` — the SAME shared formula both
    reports use. A market whose observations cluster inside a few games
    (several props from one start, several game markets off one score) has
    a LARGER implied per-observation spread than one observation per game
    would, so it correctly needs MORE observations — not the same count a
    naive (uncorrelated) formula would ask for.

    Returns ``None`` when there are fewer than 2 total observations, OR
    fewer than :data:`MIN_CLUSTERS_FOR_INFERENCE` DISTINCT games — see that
    constant's own comment for why. Both are an honest "not enough data to
    even estimate this" rather than a number built on noise or a zero built
    on an algebraic accident (or, at just 2-3 games, one that got lucky).
    """
    n = sum(len(xs) for xs in values_by_game.values())
    if n < 2 or len(values_by_game) < MIN_CLUSTERS_FOR_INFERENCE:
        return None
    implied_sd = _clustered_se(values_by_game) * float(n) ** 0.5
    return _minimum_paired_observations(implied_sd, floor=floor, alpha=alpha)


# ===========================================================================
# Scoreboard aggregation (PURE)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class ScoreboardRow:
    """Aggregated CLV stats for one group (a market_type, a prop, or 'overall').

    The HEADLINE metric is :attr:`beat_close_rate` — the fraction of PLACED bets
    that beat the close (clv_prob > 0). ``mean_clv_prob`` / ``mean_model_edge`` are
    means over the placed bets only (0.0 when none were placed).

    SIM-539 adds the honest error bars:

      * ``n_games_placed`` — the distinct game count behind the PLACED bets
        specifically (unlike ``n_games``, which counts every game this
        market was PRICED in, placed or not) — the number to actually check
        against :data:`MIN_CLUSTERS_FOR_INFERENCE`, since it is what the
        fields below are built from. An adversarial review of SIM-539
        confirmed a reader could otherwise mistake ``n_games`` for that
        count.
      * ``clustered_se`` — the game-clustered (CR0) standard error of the
        beat-close indicator (bets in one game are correlated — several
        props from the same start, say — so this is the trustworthy
        uncertainty for a pooled rate, not the naive i.i.d. one). ``nan``
        when fewer than :data:`MIN_CLUSTERS_FOR_INFERENCE` distinct games
        back the rate — the formula is mathematically degenerate (exactly
        0.0) at 1 game, which would misreport "no uncertainty" for the
        LEAST resolved case there is.
      * ``beat_close_ci_low`` / ``beat_close_ci_high`` — a 95%-equivalent
        Wald interval on ``beat_close_rate`` built from ``clustered_se`` (see
        :func:`_row_for`), clamped to [0, 1]; the widest honest interval
        ([0, 1]) when ``clustered_se`` cannot be trusted (see above), or
        when there are no placed bets at all.
      * ``min_bets_recommended`` — the smallest number of placed bets this
        market needs before its rate should be trusted at all (``None`` when
        there is too little data — too few bets or too few distinct games —
        to even estimate it — see :func:`_minimum_observations_clustered`);
        ``underpowered`` is True when ``n_bets_placed`` has not yet reached
        it (or the minimum is ``None``). This can still read 0 (fully
        resolved) from a SMALL sample whose observed spread happens to be
        exactly zero — see the module docstring's "WHAT THIS DOES NOT
        CLAIM" section; clearing :data:`MIN_CLUSTERS_FOR_INFERENCE` makes
        that far less likely, not impossible.
      * ``alpha_used`` — the (Bonferroni-corrected) two-sided significance
        level this row's interval and minimum were built at — see the module
        docstring's "MINIMUM SAMPLE SIZE" section.
    """

    group: str
    trust: str
    n_games: int
    n_games_placed: int
    n_markets_priced: int
    n_bets_placed: int
    beat_close_rate: float
    mean_clv_prob: float
    mean_model_edge: float
    # --- SIM-539 error bars ----------------------------------------------------
    clustered_se: float = 0.0
    beat_close_ci_low: float = 0.0
    beat_close_ci_high: float = 0.0
    min_bets_recommended: float | None = None
    underpowered: bool = True
    alpha_used: float = DEFAULT_ALPHA

    def to_jsonable(self) -> dict[str, Any]:
        """A JSON-safe dict of this row — see :func:`_json_safe` (SIM-539:
        ``clustered_se`` can be ``nan``, which is not legal JSON)."""
        return _json_safe(asdict(self))


def _row_for(group: str, trust: str, bets: Sequence[BetRecord], *, alpha: float) -> ScoreboardRow:
    """Aggregate one bucket of :class:`BetRecord`s into a :class:`ScoreboardRow`.

    ``n_markets_priced`` counts every record (placed or no-bet — the market WAS
    priced); ``n_bets_placed`` counts only placed bets; ``beat_close_rate`` /
    ``mean_clv_prob`` / ``mean_model_edge`` are over the placed bets only.
    ``n_games`` is the distinct game count over EVERY record in the bucket;
    ``n_games_placed`` (SIM-539) is the distinct game count over the PLACED
    bets specifically — the two can differ when a market is priced more
    often than it is bet, and it is ``n_games_placed`` the error bars below
    are actually built from. ``alpha`` is the (already Bonferroni-corrected
    — see :func:`aggregate_scoreboard`) two-sided significance level this
    row's confidence interval and minimum sample size are built at (SIM-539).
    """
    placed = [b for b in bets if b.placed]
    n_placed = len(placed)
    outcomes_by_game: dict[int, list[float]] = {}
    if n_placed:
        beat = sum(1 for b in placed if b.beat_close)
        beat_close_rate = beat / n_placed
        mean_clv = sum(float(b.clv_prob or 0.0) for b in placed) / n_placed
        mean_edge = sum(float(b.model_edge or 0.0) for b in placed) / n_placed
        for b in placed:
            outcomes_by_game.setdefault(int(b.game_pk), []).append(1 if b.beat_close else 0)
    else:
        beat_close_rate = 0.0
        mean_clv = 0.0
        mean_edge = 0.0

    # SIM-539: the game-clustered SE, a Wald CI built from it, and the
    # minimum placed-bet count this market needs before its rate is trusted.
    # _clustered_se is mathematically degenerate (always exactly 0.0) with
    # fewer than MIN_CLUSTERS_FOR_INFERENCE distinct games -- see that
    # constant's own comment. Below that threshold (n_placed == 0 included:
    # zero placed bets is zero distinct games) report the widest honest
    # interval ([0, 1] -- nothing was measured, not "measured a confident
    # 0%") instead of a falsely narrow or falsely confident one, and refuse
    # to state a minimum.
    shrunk_floor = MIN_DETECTABLE_EDGE / DETECTION_MARGIN
    if len(outcomes_by_game) < MIN_CLUSTERS_FOR_INFERENCE:
        clustered_se = float("nan")
        ci_low, ci_high = 0.0, 1.0
        min_bets = None
    else:
        clustered_se = _clustered_se(outcomes_by_game)
        z = _z_two_sided(alpha)
        ci_low = max(0.0, beat_close_rate - z * clustered_se)
        ci_high = min(1.0, beat_close_rate + z * clustered_se)
        min_bets = _minimum_observations_clustered(
            outcomes_by_game, floor=shrunk_floor, alpha=alpha
        )
    underpowered = min_bets is None or n_placed < min_bets

    return ScoreboardRow(
        group=group,
        trust=trust,
        n_games=len({b.game_pk for b in bets}),
        n_games_placed=len(outcomes_by_game),
        n_markets_priced=len(bets),
        n_bets_placed=n_placed,
        beat_close_rate=beat_close_rate,
        mean_clv_prob=mean_clv,
        mean_model_edge=mean_edge,
        clustered_se=clustered_se,
        beat_close_ci_low=ci_low,
        beat_close_ci_high=ci_high,
        min_bets_recommended=min_bets,
        underpowered=underpowered,
        alpha_used=alpha,
    )


def aggregate_scoreboard(
    bets: Sequence[BetRecord], *, base_alpha: float = DEFAULT_ALPHA
) -> dict[str, Any]:
    """PURE: roll a list of :class:`BetRecord`s into the full scoreboard.

    Returns a dict with:
      * ``overall`` — one :class:`ScoreboardRow` over every bet, at the
        UNCORRECTED ``base_alpha`` (it is one top-line figure, not one of
        several rows a reader might scan for "which one looks good" — see
        the module docstring's "MINIMUM SAMPLE SIZE" section);
      * ``by_market`` — one row per distinct ``market`` key (the per-market_type
        game markets + each prop stat), each tagged with its trust label, sorted
        by trust tier then market key for a readable table, at a
        Bonferroni-corrected ``base_alpha`` (SIM-539 — correcting for
        however many rows THIS run actually produces).

    The HEADLINE per row is ``beat_close_rate``. Deterministic; no DB/sim.
    """
    bets = list(bets)
    overall = _row_for("overall", "—", bets, alpha=base_alpha)

    by_market_key: dict[str, list[BetRecord]] = {}
    for b in bets:
        by_market_key.setdefault(b.market, []).append(b)

    # Sort by trust tier (best first) then market key for a stable, readable table.
    _trust_order = {"trustworthy": 0, "loose": 1, "caution": 2, "untrustworthy": 3, "unknown": 4}
    by_market_alpha = _bonferroni_alpha(base_alpha, len(by_market_key))
    rows = [
        _row_for(market, trust_label(market), bucket, alpha=by_market_alpha)
        for market, bucket in by_market_key.items()
    ]
    rows.sort(key=lambda r: (_trust_order.get(r.trust, 9), r.group))

    return {
        "overall": overall.to_jsonable(),
        "by_market": [r.to_jsonable() for r in rows],
    }


# ===========================================================================
# Readable scoreboard table
# ===========================================================================


def format_scoreboard(scoreboard: dict[str, Any], *, params: dict[str, Any]) -> str:
    """Render the aggregated scoreboard as a readable, trust-grouped text table."""
    lines = [
        "=" * 100,
        "SECONDARY DIAGNOSTIC — SIM-429 CLV SCOREBOARD (entry vs. close price; no real",
        "outcome involved). beat_close_rate = % of placed bets that beat the close.",
        "The headline metric is the accuracy comparison below — see SIM-538. The 95%",
        "range and minBets are SIM-539's game-clustered confidence read; UNDERPWR means",
        "this row has not yet reached its own minimum placed-bet count.",
        "=" * 100,
        f"seasons={params.get('seasons')}  iterations={params.get('iterations')}  "
        f"markets={params.get('markets')}  min_edge={params.get('min_edge')}  "
        f"base_seed={params.get('base_seed')}",
        "",
    ]
    header = (
        f"{'market':<14}{'trust':<14}{'games':>6}{'priced':>8}"
        f"{'placed':>8}{'beat%':>9}{'meanCLV':>10}{'95% range':>18}{'minBets':>9}{'pwr':>9}"
    )

    def _fmt_row(r: dict[str, Any]) -> str:
        rng = f"[{r['beat_close_ci_low'] * 100:.1f},{r['beat_close_ci_high'] * 100:.1f}]"
        min_bets = r.get("min_bets_recommended")
        min_bets_s = "n/a" if min_bets is None else f"{min_bets:.0f}"
        pwr = "UNDERPWR" if r.get("underpowered") else "ok"
        return (
            f"{r['group']:<14}{r['trust']:<14}{r['n_games']:>6}{r['n_markets_priced']:>8}"
            f"{r['n_bets_placed']:>8}{r['beat_close_rate'] * 100:>8.1f}%"
            f"{r['mean_clv_prob']:>10.4f}{rng:>18}{min_bets_s:>9}{pwr:>9}"
        )

    o = scoreboard["overall"]
    lines += [
        "--- OVERALL ---",
        header,
        _fmt_row(o),
        "",
        "--- BY MARKET (grouped by trust tier) ---",
        header,
    ]
    last_trust = None
    for r in scoreboard["by_market"]:
        if r["trust"] != last_trust:
            lines.append(f"  [{r['trust']}]")
            last_trust = r["trust"]
        lines.append(_fmt_row(r))
    lines.append("=" * 100)
    return "\n".join(lines)


# ===========================================================================
# SIM-538: the sim-vs-closing-line accuracy comparison (PURE — the headline)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class AccuracyComparisonRow:
    """SIM-538: the paired sim-vs-market accuracy comparison for one group.

    A NEGATIVE ``*_diff_mean`` means the simulator's score was LOWER — better,
    since both Brier score and log loss are lower-is-better proper scoring
    rules — than the market's, on average: the simulator beat the market's
    accuracy. The ``*_ci_low`` / ``*_ci_high`` fields are a 95% bootstrap
    interval on that mean paired difference (see
    :func:`aggregate_accuracy_comparison`); an interval that does not cross
    zero is the "likely real, not chance" read SIM-538's own definition of
    done asks for.

    SIM-539 adds a STATED minimum: ``brier_min_n`` / ``log_loss_min_n`` are
    the smallest paired-OBSERVATION count this market needs before ITS OWN
    GAME-CLUSTERED measured spread (the same clustering the bootstrap CI
    above already accounts for) clears the platform's minimum-detectable-
    edge floor at ``alpha_used`` (see the module docstring's "MINIMUM SAMPLE
    SIZE" section); ``min_n_recommended`` is the larger (more binding) of
    the two — the number to actually check ``n`` against. ``inf`` when there
    are too few observations OR too few distinct games to measure a spread
    from at all. ``underpowered`` is True when ``n`` has not yet reached it.

    ``n_games`` (SIM-539) is the distinct game count behind this row — read
    it alongside ``min_n_recommended`` rather than assuming that number is
    itself a game count: a prop bucket (several players' records can come
    from ONE game) can reach its observation minimum from far fewer games
    than the raw number suggests.
    """

    group: str
    trust: str
    n: int
    n_games: int
    sim_brier: float
    market_brier: float
    brier_diff_mean: float
    brier_diff_ci_low: float
    brier_diff_ci_high: float
    sim_log_loss: float
    market_log_loss: float
    log_loss_diff_mean: float
    log_loss_diff_ci_low: float
    log_loss_diff_ci_high: float
    # --- SIM-539 minimum sample size --------------------------------------------
    brier_min_n: float = float("inf")
    log_loss_min_n: float = float("inf")
    min_n_recommended: float = float("inf")
    underpowered: bool = True
    alpha_used: float = DEFAULT_ALPHA

    def to_jsonable(self) -> dict[str, Any]:
        """A JSON-safe dict of this row — see :func:`_json_safe` (SIM-539:
        several fields here can be ``nan`` at n=0 or ``inf`` when
        underpowered, neither legal JSON)."""
        return _json_safe(asdict(self))


def _bootstrap_paired_diff_ci(
    diffs: np.ndarray, game_pks: np.ndarray, *, n_bootstrap: int, seed: int
) -> tuple[float, float]:
    """95% percentile CLUSTER-bootstrap CI on the MEAN of ``diffs`` (paired
    per-observation score differences — sim's score minus the market's, so
    negative means the sim was more accurate on that observation).

    Resamples GAMES, not individual observations. A single game can
    contribute more than one observation here — up to 3 game markets
    (moneyline/total/runline, all read off the SAME final score) and, for a
    prop bucket, one record per player who has that stat (several batters'
    hits in one game, say). Those records are not independent trials: they
    share one game's park, bullpen, and final score. Resampling them one at
    a time (as an ordinary bootstrap would) treats a busy game as if it
    were several independent games and REPORTS A NARROWER INTERVAL THAN THE
    DATA SUPPORTS — an adversarial review of SIM-538 confirmed this would
    make "the interval does not cross zero" too easy to satisfy from one
    correlated game rather than many independent ones.

    The fix: draw ``n_bootstrap`` samples of GAME IDENTITIES with
    replacement (the real unit of independence), and for each draw, pool
    every observation from the resampled games before taking the mean —
    exactly what resampling the underlying games and keeping all of each
    game's records would give, computed via each game's precomputed
    (sum, count) rather than by concatenating arrays per draw. When every
    game contributes at most one observation to ``diffs`` (true for
    single-player markets like moneyline), each game's count is always 1,
    so this reduces exactly to the ordinary per-observation bootstrap.

    Deterministic given ``seed``: the SAME games at the SAME seed always
    resample the SAME way, so a re-run reproduces the SAME interval — this
    file's existing determinism guarantee (SIM-430's byte-identical-replay
    property), extended to the one place this module now uses an RNG.

    Fewer than 2 observations, or fewer than 2 distinct games, means
    nothing to resample; the caller gets a degenerate ``(mean, mean)`` back
    rather than a false-precision interval.
    """
    n = diffs.size
    if n < 2:
        m = float(diffs.mean()) if n else float("nan")
        return m, m
    unique_games, inverse = np.unique(game_pks, return_inverse=True)
    g = unique_games.size
    if g < 2:
        m = float(diffs.mean())
        return m, m
    # One (sum, count) per distinct game, computed once — a bootstrap draw's
    # mean over its resampled games is then sum(picked sums) / sum(picked
    # counts), so no per-draw array concatenation is needed.
    sums = np.zeros(g, dtype=np.float64)
    counts = np.zeros(g, dtype=np.float64)
    np.add.at(sums, inverse, diffs)
    np.add.at(counts, inverse, 1.0)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, g, size=(int(n_bootstrap), g))
    means = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def _accuracy_row_for(
    group: str,
    trust: str,
    records: Sequence[AccuracyRecord],
    *,
    n_bootstrap: int,
    seed: int,
    alpha: float,
) -> AccuracyComparisonRow:
    """PURE: aggregate one bucket of :class:`AccuracyRecord`s into one row.

    The per-observation Brier/log-loss DIFFERENCES are computed here (not
    inside :func:`binary_brier` / :func:`binary_log_loss`, which only return
    the aggregate) so the paired bootstrap has something to resample — each
    difference is one game/prop's own (sim error) minus (market error). The
    log-loss difference clips both probabilities with the SAME
    :data:`OUTCOME_PROB_EPS` :func:`binary_log_loss` itself uses, so the
    per-observation differences sum to the SAME aggregate figure reported
    alongside them.

    ``alpha`` (already Bonferroni-corrected for by-market rows — see
    :func:`aggregate_accuracy_comparison`) sizes the SIM-539 minimum sample
    size: :data:`MIN_DETECTABLE_EDGE` shrunk by :data:`DETECTION_MARGIN`,
    fed through :func:`_minimum_observations_clustered` at THIS bucket's own
    observed, GAME-CLUSTERED Brier/log-loss-difference spread.
    """
    n = len(records)
    sim_p = np.array([r.sim_prob for r in records], dtype=np.float64)
    mkt_p = np.array([r.market_prob for r in records], dtype=np.float64)
    y = np.array([r.outcome for r in records], dtype=np.int64)
    game_pks = np.array([r.game_pk for r in records], dtype=np.int64)

    sim_brier = binary_brier(sim_p, y)
    market_brier = binary_brier(mkt_p, y)
    sim_ll = binary_log_loss(sim_p, y)
    market_ll = binary_log_loss(mkt_p, y)

    if n:
        # Defensive [0, 1] clip -- the SAME one binary_brier's own
        # _as_prob_outcome applies before scoring sim_brier/market_brier
        # above. Every current producer of sim_prob/market_prob already
        # stays inside [0, 1] (win-prob calibration clamps it; the total/
        # run-line sim probabilities are empirical count/n ratios;
        # devig_two_way's fair probability is a strict (0, 1) normalization),
        # so this clip changes nothing today. It exists so a future producer
        # that is NOT already bounded cannot silently make these
        # per-observation differences sum to something other than the
        # reported aggregate brier_mean below -- an adversarial review of
        # SIM-538 flagged the two code paths as duplicating the same bound
        # rather than sharing one clipped source of truth.
        sim_p_b = np.clip(sim_p, 0.0, 1.0)
        mkt_p_b = np.clip(mkt_p, 0.0, 1.0)
        brier_diffs = (sim_p_b - y) ** 2 - (mkt_p_b - y) ** 2
        eps = OUTCOME_PROB_EPS
        sim_pc = np.clip(sim_p, eps, 1.0 - eps)
        mkt_pc = np.clip(mkt_p, eps, 1.0 - eps)
        sim_ll_i = -(y * np.log(sim_pc) + (1 - y) * np.log(1.0 - sim_pc))
        mkt_ll_i = -(y * np.log(mkt_pc) + (1 - y) * np.log(1.0 - mkt_pc))
        log_loss_diffs = sim_ll_i - mkt_ll_i
    else:
        brier_diffs = np.array([], dtype=np.float64)
        log_loss_diffs = np.array([], dtype=np.float64)

    brier_mean = float(brier_diffs.mean()) if n else float("nan")
    ll_mean = float(log_loss_diffs.mean()) if n else float("nan")
    brier_lo, brier_hi = _bootstrap_paired_diff_ci(
        brier_diffs, game_pks, n_bootstrap=n_bootstrap, seed=seed
    )
    # SIM-538 fix: offset the log-loss seed by a large constant, not +1. The
    # caller assigns each ROW its own seed a small step apart (the 'overall'
    # row gets `seed`, by-market row i gets `seed + i`), so a bare +1 here
    # made row i's log-loss draw and row i+1's Brier draw collide on the
    # EXACT SAME seed -- two conceptually unrelated statistics resampling
    # bit-identically. An adversarial review of SIM-538 confirmed this. The
    # offset below is far larger than any plausible row count, so it cannot
    # collide with another row's seed in practice.
    ll_lo, ll_hi = _bootstrap_paired_diff_ci(
        log_loss_diffs, game_pks, n_bootstrap=n_bootstrap, seed=seed + 1_000_003
    )

    # SIM-539: minimum sample size, from THIS bucket's own observed spread —
    # GAME-CLUSTERED, the SAME way the bootstrap CI above already resamples
    # games rather than rows. A bucket where several observations share one
    # game (three game markets off one score; several players' props from
    # one start) has fewer independent trials than its row count suggests;
    # feeding a naive per-row std into the formula would understate that,
    # exactly the class of bug an adversarial review found and fixed for
    # the bootstrap CI itself. _minimum_observations_clustered returns None
    # (treated as inf here) when there are too few observations OR too few
    # distinct games to measure a spread from at all.
    shrunk_floor = MIN_DETECTABLE_EDGE / DETECTION_MARGIN
    brier_by_game: dict[int, list[float]] = {}
    ll_by_game: dict[int, list[float]] = {}
    for gp, bd, ld in zip(
        game_pks.tolist(), brier_diffs.tolist(), log_loss_diffs.tolist(), strict=True
    ):
        brier_by_game.setdefault(int(gp), []).append(float(bd))
        ll_by_game.setdefault(int(gp), []).append(float(ld))
    brier_min_n_or_none = _minimum_observations_clustered(
        brier_by_game, floor=shrunk_floor, alpha=alpha
    )
    ll_min_n_or_none = _minimum_observations_clustered(ll_by_game, floor=shrunk_floor, alpha=alpha)
    brier_min_n = float("inf") if brier_min_n_or_none is None else brier_min_n_or_none
    log_loss_min_n = float("inf") if ll_min_n_or_none is None else ll_min_n_or_none
    min_n_recommended = max(brier_min_n, log_loss_min_n)
    underpowered = n < min_n_recommended

    return AccuracyComparisonRow(
        group=group,
        trust=trust,
        n=n,
        n_games=len(set(game_pks.tolist())),
        sim_brier=sim_brier,
        market_brier=market_brier,
        brier_diff_mean=brier_mean,
        brier_diff_ci_low=brier_lo,
        brier_diff_ci_high=brier_hi,
        sim_log_loss=sim_ll,
        market_log_loss=market_ll,
        log_loss_diff_mean=ll_mean,
        log_loss_diff_ci_low=ll_lo,
        log_loss_diff_ci_high=ll_hi,
        brier_min_n=brier_min_n,
        log_loss_min_n=log_loss_min_n,
        min_n_recommended=min_n_recommended,
        underpowered=underpowered,
        alpha_used=alpha,
    )


def aggregate_accuracy_comparison(
    records: Sequence[AccuracyRecord],
    *,
    n_bootstrap: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = 538,
    base_alpha: float = DEFAULT_ALPHA,
) -> dict[str, Any]:
    """PURE: roll a list of :class:`AccuracyRecord`s into the accuracy report.

    Mirrors :func:`aggregate_scoreboard`'s shape: an ``overall`` row over every
    record (at the UNCORRECTED ``base_alpha`` — see :func:`aggregate_scoreboard`
    for why), plus ``by_market`` rows (one per distinct market/prop key,
    Bonferroni-corrected by however many rows this run produces — SIM-539),
    sorted by trust tier then market key. Every row carries a bootstrap 95%
    CI on the paired Brier/log-loss difference AND a stated minimum sample
    size (:attr:`AccuracyComparisonRow.min_n_recommended`).

    The bootstrap CI is a bounded, single-run confidence read; the minimum
    sample size is the documented floor SIM-539 adds on top of it — see the
    module docstring's "MINIMUM SAMPLE SIZE" and "WHAT THIS DELIBERATELY
    DOES NOT CLAIM" sections for what it does and does not claim.
    """
    records = list(records)
    overall = _accuracy_row_for(
        "overall", "—", records, n_bootstrap=n_bootstrap, seed=seed, alpha=base_alpha
    )

    by_market_key: dict[str, list[AccuracyRecord]] = {}
    for r in records:
        by_market_key.setdefault(r.market, []).append(r)

    _trust_order = {"trustworthy": 0, "loose": 1, "caution": 2, "untrustworthy": 3, "unknown": 4}
    by_market_alpha = _bonferroni_alpha(base_alpha, len(by_market_key))
    rows = [
        _accuracy_row_for(
            market,
            trust_label(market),
            bucket,
            n_bootstrap=n_bootstrap,
            seed=seed + i,
            alpha=by_market_alpha,
        )
        for i, (market, bucket) in enumerate(sorted(by_market_key.items()), start=1)
    ]
    rows.sort(key=lambda r: (_trust_order.get(r.trust, 9), r.group))

    return {
        "overall": overall.to_jsonable(),
        "by_market": [r.to_jsonable() for r in rows],
    }


def _fmt_min_n(min_n: float | None) -> str:
    """Render a SIM-539 minimum sample size for a text table.

    ``inf`` (fewer than 2 observations — nothing to even estimate a spread
    from) prints as ``"inf"`` rather than a misleadingly huge integer.
    ``None`` — what an ``inf`` becomes after :func:`_json_safe` (``inf`` is
    not legal JSON), so this is what a row rebuilt FROM the JSON report
    actually carries — prints the SAME ``"inf"`` text.
    """
    if min_n is None or min_n == float("inf"):
        return "inf"
    return f"{min_n:.0f}"


def _fmt_or_na(x: float | None, spec: str) -> str:
    """Render a float that may be ``None`` for a text table (SIM-539).

    ``None`` is what :func:`_json_safe` substitutes for a ``nan`` — e.g.
    ``sim_brier`` / ``brier_diff_ci_low`` at ``n == 0`` ("no completed games
    found") — so any row rebuilt from the JSON report can carry it here.
    Renders as ``"n/a"`` rather than crashing the whole table on one empty
    row; this exact case reached production only because a run with zero
    scoreable games prints the row before ever checking ``n``.
    """
    return "n/a" if x is None else format(x, spec)


def format_accuracy_comparison(comparison: dict[str, Any], *, params: dict[str, Any]) -> str:
    """Render the sim-vs-close accuracy comparison as a readable, trust-grouped table."""
    lines = [
        "=" * 100,
        "SIM-538 SIM-VS-CLOSING-LINE ACCURACY COMPARISON  (the headline metric)",
        "Brier / log-loss are lower-is-better. A NEGATIVE diff means the simulator was",
        "MORE ACCURATE than the de-vigged closing line, on average, over these games.",
        "The 95% range is a paired bootstrap interval on that mean difference. minN is",
        "SIM-539's stated minimum sample size for THIS market (a Bonferroni-corrected,",
        "game-clustering-aware read — see the module docstring), counted in OBSERVATIONS",
        "(n), not games — several props can share one game, so compare minN against n,",
        "not against games. UNDERPWR means n has not yet reached minN, so a range that",
        "does not cross zero here may still be chance, not signal.",
        "=" * 100,
        f"seasons={params.get('seasons')}  iterations={params.get('iterations')}  "
        f"markets={params.get('markets')}  base_seed={params.get('base_seed')}  "
        f"calibrated={params.get('calibration_applied')}",
        "",
    ]
    header = (
        f"{'market':<14}{'trust':<14}{'n':>7}{'games':>7}{'simBrier':>10}{'mktBrier':>10}"
        f"{'brierDiff':>11}{'95% range':>18}{'minN':>7}{'pwr':>10}"
    )

    def _fmt_row(r: dict[str, Any]) -> str:
        # sim_brier / brier_diff_mean / the CI bounds are all nan at n == 0
        # ("no completed games found" -- a real run outcome, not a
        # hypothetical), and _json_safe (applied before this dict ever
        # reaches here — see aggregate_accuracy_comparison) turns that nan
        # into None; format every one of them through _fmt_or_na so a
        # single empty row can never crash the whole table.
        rng = f"[{_fmt_or_na(r['brier_diff_ci_low'], '.4f')},{_fmt_or_na(r['brier_diff_ci_high'], '.4f')}]"
        pwr = "UNDERPWR" if r.get("underpowered") else "ok"
        return (
            f"{r['group']:<14}{r['trust']:<14}{r['n']:>7}{r['n_games']:>7}"
            f"{_fmt_or_na(r['sim_brier'], '.4f'):>10}{_fmt_or_na(r['market_brier'], '.4f'):>10}"
            f"{_fmt_or_na(r['brier_diff_mean'], '.4f'):>11}{rng:>18}"
            f"{_fmt_min_n(r['min_n_recommended']):>7}{pwr:>10}"
        )

    o = comparison["overall"]
    lines += [
        "--- OVERALL ---",
        header,
        _fmt_row(o),
        "",
        "--- BY MARKET (grouped by trust tier; log-loss in the JSON report) ---",
        header,
    ]
    last_trust = None
    for r in comparison["by_market"]:
        if r["trust"] != last_trust:
            lines.append(f"  [{r['trust']}]")
            last_trust = r["trust"]
        lines.append(_fmt_row(r))
    lines.append("=" * 100)
    return "\n".join(lines)


# ===========================================================================
# DB readers (lazy asyncpg import — the unit test never reaches these)
# ===========================================================================


async def _fetch_final_games(dsn: str, seasons: list[int], max_games: int | None) -> list[int]:
    """Return ``game_pk``s for Final games in the requested seasons (ordered).

    SIM-432: the live schema stores the final score in ``home_score_final`` /
    ``away_score_final`` — we gate on those being present (a real Final game).
    """
    import asyncpg

    sl = ", ".join(str(int(s)) for s in seasons)
    limit = f"LIMIT {int(max_games)}" if max_games else ""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            f"""
            SELECT game_pk
            FROM raw.games
            WHERE status = 'Final'
              AND season IN ({sl})
              AND home_score_final IS NOT NULL AND away_score_final IS NOT NULL
            ORDER BY game_pk
            {limit}
            """
        )
    finally:
        await conn.close()
    return [int(r["game_pk"]) for r in rows]


async def _fetch_game_odds(pool, game_pk: int) -> dict[str, dict[str, dict[str, Any]]]:
    """Return ``{market_type: {line_type: row_dict}}`` for one game's game odds.

    Reads the most-recent ``raw.game_odds`` row per (market_type, line_type) — the
    closing snapshot at each line_type (there can be many 'opening' / 'closing'
    rows; the latest ``fetched_at`` is the authoritative one). Only the opening /
    closing line_types are kept (the two CLV reference points).
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (market_type, line_type)
               market_type, line_type,
               home_ml, away_ml,
               home_spread, home_spread_ml, away_spread, away_spread_ml,
               total_line, over_ml, under_ml
        FROM raw.game_odds
        WHERE game_pk = $1 AND line_type IN ('opening', 'closing')
        ORDER BY market_type, line_type, fetched_at DESC
        """,
        int(game_pk),
    )
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r["market_type"]), {})[str(r["line_type"])] = dict(r)
    return out


async def _fetch_prop_odds(pool, game_pk: int) -> dict[tuple[int, str], dict[str, dict[str, Any]]]:
    """Return ``{(player_id, prop_stat): {line_type: row_dict}}`` for one game.

    Reads the latest ``raw.prop_odds`` row per (player, prop_stat, line_type),
    restricted to the opening / closing line_types.
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (player_id, prop_stat, line_type)
               player_id, prop_stat, line_type, line, over_ml, under_ml
        FROM raw.prop_odds
        WHERE game_pk = $1 AND line_type IN ('opening', 'closing')
        ORDER BY player_id, prop_stat, line_type, fetched_at DESC
        """,
        int(game_pk),
    )
    out: dict[tuple[int, str], dict[str, dict[str, Any]]] = {}
    for r in rows:
        key = (int(r["player_id"]), str(r["prop_stat"]))
        out.setdefault(key, {})[str(r["line_type"])] = dict(r)
    return out


async def _fetch_final_score(pool: Any, game_pk: int) -> tuple[int, int] | None:
    """SIM-538: the real final score for one game, or None when unavailable.

    Reads the same columns :func:`_fetch_final_games` already gates the game
    list on (``home_score_final`` / ``away_score_final``); fetched again here,
    per game, rather than carried from the parent through ``worker_params`` —
    that dict is pickled once per submitted game in the parallel path, so
    carrying every game's score in it would copy a growing whole-season dict
    on every submission instead of one small indexed lookup.
    """
    row = await pool.fetchrow(
        "SELECT home_score_final, away_score_final FROM raw.games WHERE game_pk = $1",
        int(game_pk),
    )
    if row is None or row["home_score_final"] is None or row["away_score_final"] is None:
        return None
    return int(row["home_score_final"]), int(row["away_score_final"])


async def _fetch_pa_events(pool: Any, game_pk: int) -> list[tuple]:
    """SIM-538: one ``(batter, pitcher, events)`` tuple per completed plate
    appearance — the SAME reader ``scripts/validate_props.py`` uses, feeding
    :func:`simulation.prop_validation.real_props_from_pa_events`."""
    rows = await pool.fetch(
        "SELECT batter, pitcher, events FROM raw.pitches "
        "WHERE game_pk = $1 AND events IS NOT NULL AND events <> ''",
        int(game_pk),
    )
    return [(r["batter"], r["pitcher"], r["events"]) for r in rows]


# ===========================================================================
# Per-game scoring (wires the sim output into the pure evaluation)
# ===========================================================================


def _game_prices(
    odds: dict[str, dict[str, dict[str, Any]]],
    market_type: str,
    side_col: str,
    other_col: str,
    line_col: str | None,
) -> TwoWayPrices | None:
    """Build :class:`TwoWayPrices` for a game market from its opening/closing rows.

    Returns None when either the opening or closing row is missing, or any of the
    two-way prices is NULL (an unscoreable market).
    """
    by_lt = odds.get(market_type)
    if not by_lt:
        return None
    op = by_lt.get("opening")
    cl = by_lt.get("closing")
    if op is None or cl is None:
        return None
    open_side = op.get(side_col)
    open_other = op.get(other_col)
    close_side = cl.get(side_col)
    close_other = cl.get(other_col)
    if None in (open_side, open_other, close_side, close_other):
        return None
    line = op.get(line_col) if line_col else None
    return TwoWayPrices(
        open_side=float(open_side),
        open_other=float(open_other),
        close_side=float(close_side),
        close_other=float(close_other),
        line=None if line is None else float(line),
    )


@dataclass(frozen=True, slots=True)
class ClosingPrices:
    """SIM-538: the CLOSING two-way price for one market, with the CLOSING
    row's OWN line — needs no opening row at all.

    This is what fixes the "cross-line" gap the legacy CLV step still has
    (:func:`_game_prices` reads the market's line from the OPENING row only,
    so a total/runline/prop whose LINE moved is scored against a stale line).
    The accuracy comparison has no entry side to speak of, so there is nothing
    to keep consistent WITH — only the closing quote and the closing line ever
    enter the calculation.
    """

    side: float
    other: float
    line: float | None = None


def _closing_prices(
    odds: dict[str, dict[str, dict[str, Any]]],
    market_type: str,
    side_col: str,
    other_col: str,
    line_col: str | None,
) -> ClosingPrices | None:
    """Build :class:`ClosingPrices` for a game market from its CLOSING row only.

    Returns None when there is no closing row, or either two-way price is NULL.
    Unlike :func:`_game_prices`, an opening row is never required — this is the
    SIM-541 spirit (the closing line is nearly always collected; a matched
    opening line is not) already realized for the one consumer that only ever
    needed the close.
    """
    by_lt = odds.get(market_type)
    if not by_lt:
        return None
    cl = by_lt.get("closing")
    if cl is None:
        return None
    side = cl.get(side_col)
    other = cl.get(other_col)
    if None in (side, other):
        return None
    line = cl.get(line_col) if line_col else None
    return ClosingPrices(
        side=float(side),
        other=float(other),
        line=None if line is None else float(line),
    )


def score_game_markets(
    game_pk: int,
    win_prob: Any,
    summary: Any,
    odds: dict[str, dict[str, dict[str, Any]]],
    *,
    min_edge: float = 0.0,
) -> list[BetRecord]:
    """Score the three GAME markets (moneyline / total / runline) for one game.

    Wires the sim output (``win_prob`` / ``summary``) into the pure
    :func:`evaluate_two_way_market` via per-market edge-report closures. A market
    with a missing/NULL opening-or-closing price is skipped (not in the result).
    Degenerate sim probabilities (0/1, which ``clv_engine`` raises on) are caught
    so one bad market never sinks the game.
    """
    records: list[BetRecord] = []

    # --- moneyline (HOME/AWAY at home_ml/away_ml) ---
    prices = _game_prices(odds, "moneyline", "home_ml", "away_ml", None)
    if prices is not None:
        try:
            records.append(
                evaluate_two_way_market(
                    game_pk,
                    "moneyline",
                    "moneyline",
                    MarketSide.HOME,
                    MarketSide.AWAY,
                    lambda side, mkt: moneyline_edge_report(win_prob, mkt, side=side),
                    prices,
                    min_edge=min_edge,
                )
            )
        except ValueError as exc:
            log.info("game %s moneyline skipped (degenerate): %s", game_pk, exc)

    # --- total (OVER/UNDER at total_line) ---
    prices = _game_prices(odds, "total", "over_ml", "under_ml", "total_line")
    if prices is not None and prices.line is not None:
        try:
            records.append(
                evaluate_two_way_market(
                    game_pk,
                    "total",
                    "total",
                    MarketSide.OVER,
                    MarketSide.UNDER,
                    lambda side, mkt: total_over_under_edge_report(summary, mkt, side=side),
                    prices,
                    min_edge=min_edge,
                )
            )
        except ValueError as exc:
            log.info("game %s total skipped (degenerate): %s", game_pk, exc)

    # --- runline (HOME/AWAY at home_spread) ---
    prices = _game_prices(odds, "runline", "home_spread_ml", "away_spread_ml", "home_spread")
    if prices is not None and prices.line is not None:
        try:
            eff_line = prices.line
            records.append(
                evaluate_two_way_market(
                    game_pk,
                    "runline",
                    "runline",
                    MarketSide.HOME,
                    MarketSide.AWAY,
                    lambda side, mkt: run_line_edge_report(summary, mkt, side=side, line=eff_line),
                    prices,
                    min_edge=min_edge,
                )
            )
        except ValueError as exc:
            log.info("game %s runline skipped (degenerate): %s", game_pk, exc)

    return records


def score_game_accuracy(
    game_pk: int,
    win_prob: Any,
    summary: Any,
    odds: dict[str, dict[str, dict[str, Any]]],
    home_score: int,
    away_score: int,
) -> list[AccuracyRecord]:
    """SIM-538: score the three GAME markets on a FIXED reference side against
    the CLOSING line and the real final score.

    Reference side per market — HOME for moneyline and the run line, OVER for
    the total — chosen once and never by model preference (see
    :class:`AccuracyRecord`). A market with no closing price is skipped. A
    total/run-line observation that landed EXACTLY on the closing line (a push)
    contributes no record: the probability being scored is a STRICT
    over/cover probability (mirrors :func:`betting.clv_engine.total_over_under_edge_report`
    / :func:`betting.clv_engine.spread_cover_prob`'s own push handling), so a
    push outcome has no well-defined 0/1 label to score it against.
    """
    records: list[AccuracyRecord] = []

    # --- moneyline (HOME at home_ml, vs away_ml) ---
    cp = _closing_prices(odds, "moneyline", "home_ml", "away_ml", None)
    if cp is not None:
        try:
            market = TwoWayMarket(
                side=MarketSide.HOME,
                entry=OddsQuote(side=cp.side, other=cp.other, line=None),
            )
            er = moneyline_edge_report(win_prob, market, side=MarketSide.HOME)
            outcome = 1 if home_score > away_score else 0
            records.append(
                AccuracyRecord(
                    game_pk=int(game_pk),
                    market="moneyline",
                    market_type="moneyline",
                    sim_prob=float(er.sim_prob),
                    market_prob=float(er.market_fair_prob),
                    outcome=outcome,
                )
            )
        except ValueError as exc:
            log.info("game %s moneyline accuracy skipped (degenerate): %s", game_pk, exc)

    # --- total (OVER at total_line) ---
    cp = _closing_prices(odds, "total", "over_ml", "under_ml", "total_line")
    if cp is not None and cp.line is not None:
        actual_total = float(home_score + away_score)
        if actual_total != cp.line:
            try:
                market = TwoWayMarket(
                    side=MarketSide.OVER,
                    entry=OddsQuote(side=cp.side, other=cp.other, line=cp.line),
                )
                er = total_over_under_edge_report(summary, market, side=MarketSide.OVER)
                outcome = 1 if actual_total > cp.line else 0
                records.append(
                    AccuracyRecord(
                        game_pk=int(game_pk),
                        market="total",
                        market_type="total",
                        sim_prob=float(er.sim_prob),
                        market_prob=float(er.market_fair_prob),
                        outcome=outcome,
                    )
                )
            except ValueError as exc:
                log.info("game %s total accuracy skipped (degenerate): %s", game_pk, exc)

    # --- runline (HOME covers at home_spread) ---
    cp = _closing_prices(odds, "runline", "home_spread_ml", "away_spread_ml", "home_spread")
    if cp is not None and cp.line is not None:
        margin = float(home_score - away_score)
        threshold = -cp.line  # mirrors betting.clv_engine.spread_cover_prob
        if margin != threshold:
            try:
                market = TwoWayMarket(
                    side=MarketSide.HOME,
                    entry=OddsQuote(side=cp.side, other=cp.other, line=cp.line),
                )
                er = run_line_edge_report(summary, market, side=MarketSide.HOME, line=cp.line)
                outcome = 1 if margin > threshold else 0
                records.append(
                    AccuracyRecord(
                        game_pk=int(game_pk),
                        market="runline",
                        market_type="runline",
                        sim_prob=float(er.sim_prob),
                        market_prob=float(er.market_fair_prob),
                        outcome=outcome,
                    )
                )
            except ValueError as exc:
                log.info("game %s runline accuracy skipped (degenerate): %s", game_pk, exc)

    return records


def score_prop_markets(
    game_pk: int,
    pset: Any,
    prop_odds: dict[tuple[int, str], dict[str, dict[str, Any]]],
    *,
    min_edge: float = 0.0,
) -> list[BetRecord]:
    """Score the player-prop markets for one game.

    For each (player, odds prop_stat) with both an opening and a closing line, map
    the odds stat to the model prop (:data:`PROP_VOCAB_MAP`), look up that player's
    :class:`PropDistribution`, and score OVER/UNDER via the pure
    :func:`evaluate_two_way_market`. Players/props with no model distribution (the
    player never appeared) or a missing/NULL price are skipped.
    """
    records: list[BetRecord] = []
    for (player_id, odds_stat), by_lt in prop_odds.items():
        model_stat = PROP_VOCAB_MAP.get(odds_stat)
        if model_stat is None:
            continue
        op = by_lt.get("opening")
        cl = by_lt.get("closing")
        if op is None or cl is None:
            continue
        open_over, open_under = op.get("over_ml"), op.get("under_ml")
        close_over, close_under = cl.get("over_ml"), cl.get("under_ml")
        line = op.get("line")
        if None in (open_over, open_under, close_over, close_under, line):
            continue

        dist = pset.get(int(player_id), model_stat) if pset is not None else None
        if dist is None:
            continue

        prices = TwoWayPrices(
            open_side=float(open_over),
            open_other=float(open_under),
            close_side=float(close_over),
            close_other=float(close_under),
            line=float(line),
        )
        try:
            records.append(
                evaluate_two_way_market(
                    game_pk,
                    model_stat,
                    "prop",
                    MarketSide.OVER,
                    MarketSide.UNDER,
                    lambda side, mkt, _d=dist: prop_edge_report(_d, mkt, side=side),
                    prices,
                    min_edge=min_edge,
                    player_id=int(player_id),
                )
            )
        except ValueError as exc:
            log.info(
                "game %s prop %s/%s skipped (degenerate): %s", game_pk, player_id, model_stat, exc
            )
    return records


#: SIM-538: derivable straight from an event LABEL (see
#: simulation.prop_validation.real_props_from_pa_events) — RBI/ER are omitted
#: for the same reason validate_props.py omits them: no reliable per-PA-event
#: ground truth exists for either.
_ACCURACY_SCORED_PROPS = frozenset({"K", "BB", "H", "HR", "TB"})


def score_prop_accuracy(
    game_pk: int,
    pset: Any,
    prop_odds: dict[tuple[int, str], dict[str, dict[str, Any]]],
    batter_actuals: dict[int, dict[str, int]],
    pitcher_actuals: dict[int, dict[str, int]],
) -> list[AccuracyRecord]:
    """SIM-538: score player-prop markets on the OVER side against the CLOSING
    line and the real per-player total.

    ``batter_actuals`` / ``pitcher_actuals`` come from
    ``simulation.prop_validation.real_props_from_pa_events`` — covers H/HR/TB
    (batter) and K/BB (pitcher) only (see :data:`_ACCURACY_SCORED_PROPS`); a
    player/prop outside that set (RBI, ER) or with no real-outcome entry at all
    (no plate appearance this game) is skipped, not scored with a guessed
    ground truth. A push (the real total lands exactly on the closing line) is
    also skipped, mirroring :func:`score_game_accuracy`.
    """
    records: list[AccuracyRecord] = []
    for (player_id, odds_stat), by_lt in prop_odds.items():
        model_stat = PROP_VOCAB_MAP.get(odds_stat)
        if model_stat is None or model_stat not in _ACCURACY_SCORED_PROPS:
            continue
        cl = by_lt.get("closing")
        if cl is None:
            continue
        close_over, close_under, line = cl.get("over_ml"), cl.get("under_ml"), cl.get("line")
        if None in (close_over, close_under, line):
            continue

        dist = pset.get(int(player_id), model_stat) if pset is not None else None
        if dist is None:
            continue

        pid = int(player_id)
        actual = batter_actuals.get(pid, {}).get(model_stat)
        if actual is None:
            actual = pitcher_actuals.get(pid, {}).get(model_stat)
        if actual is None:
            continue  # no plate appearance recorded for this player this game

        line_f = float(line)
        if float(actual) == line_f:
            continue  # push

        try:
            market = TwoWayMarket(
                side=MarketSide.OVER,
                entry=OddsQuote(side=float(close_over), other=float(close_under), line=line_f),
            )
            er = prop_edge_report(dist, market, side=MarketSide.OVER)
        except ValueError as exc:
            log.info(
                "game %s prop-accuracy %s/%s skipped (degenerate): %s",
                game_pk,
                player_id,
                model_stat,
                exc,
            )
            continue

        outcome = 1 if float(actual) > line_f else 0
        records.append(
            AccuracyRecord(
                game_pk=int(game_pk),
                market=model_stat,
                market_type="prop",
                sim_prob=float(er.sim_prob),
                market_prob=float(er.market_fair_prob),
                outcome=outcome,
                player_id=pid,
            )
        )
    return records


# ===========================================================================
# Sim replay (reuses the validate_props / betting.py seam)
# ===========================================================================


def _collect_game_results(state, n_iter: int, base_seed: int | None) -> list:
    """Replay one already-resolved game N times; return the per-iteration results.

    The SAME ``record_game_plays`` seam ``scripts/validate_props.py`` uses: builds
    the live machine from the production factory ref + the resolved GameState's
    sim_kwargs and runs ``simulate_game`` at each derived seed, collecting the
    ``GameSimResult`` (carrying ``.boxscore`` + the score) per iteration. Sync +
    CPU-bound, so the async caller offloads it via ``asyncio.to_thread``.

    This is the SINGLE serial per-game replay both execution modes use, so the
    per-iteration seeds (``derive_seed(base_seed, i)``) — and therefore the sims —
    are IDENTICAL whether a game runs in the parent (``--workers 1``) or inside a
    parallel worker (``--workers > 1``).

    SIM-452: ``state`` must arrive with its venue park factor ALREADY resolved.
    :func:`_score_one_game` resolves it, because the lookup is async and this body
    runs in a worker thread with no event loop. The builder enforces the ordering: a
    state that skipped the lookup still carries ``UNRESOLVED_PARK_FACTOR`` and
    ``sim_kwargs_from_state`` raises on it rather than replaying park-blind. That is
    what this script did on every run before SIM-452, so every CLV number it has
    ever produced came from a park-blind simulator.
    """
    from api.routes.games import _sim_kwargs_from_state
    from simulation.batch_runner import derive_seed
    from simulation.play_recorder import record_game_plays

    sim_kwargs = _sim_kwargs_from_state(state)
    results = []
    for i in range(int(n_iter)):
        seed = derive_seed(base_seed, i)
        result, _plays = record_game_plays(
            factory_ref=_FACTORY_REF, seed=seed, sim_kwargs=sim_kwargs
        )
        results.append(result)
    return results


# ===========================================================================
# The per-game unit of work — shared by the serial AND parallel execution paths
# ===========================================================================


async def _score_one_game(
    pool: Any,
    game_pk: int,
    *,
    duck: Any,
    do_game: bool,
    do_props: bool,
    score_accuracy: bool,
    iterations: int,
    base_seed: int,
    min_edge: float,
    calibration_map: Any = IDENTITY_CALIBRATION,
) -> tuple[list[BetRecord], list[AccuracyRecord], str, float]:
    """Resolve, replay, and score ONE game; return ``(bet_records,
    accuracy_records, status, park_factor)``.

    This is the ENTIRE per-game pipeline, factored out of :func:`run`'s old loop so
    BOTH the serial in-process path and a parallel worker call the exact same code:

      1. read the game's opening+closing odds (``raw.game_odds`` / ``raw.prop_odds``);
      2. resolve the :class:`GameState` (lineup → state);
      3. resolve the venue park factor AND the point-in-time cutoff onto that
         state (SIM-452 / SIM-538 — the two steps this script used to skip,
         which made every number it ever produced a park-blind, leak-prone
         measurement);
      4. replay ``iterations`` sims via the SAME :func:`_collect_game_results` seam
         (per-iteration seed = ``derive_seed(base_seed, i)`` — deterministic per game);
      5. build the :class:`GameSimSummary` + a :class:`WinProbability` scored
         through ``calibration_map`` (+ the :class:`PropDistributionSet` for props);
      6. produce the per-bet records via the pure ``score_game_markets`` /
         ``score_prop_markets`` (SIM-429, secondary diagnostic) AND, when
         ``score_accuracy`` is set, the accuracy records via
         ``score_game_accuracy`` / ``score_prop_accuracy`` (SIM-538, the
         headline metric) for whichever of ``do_game`` / ``do_props`` is set —
         the SAME market scope governs both steps.

    ``status`` is one of ``"scored"`` / ``"no_odds"`` / ``"unresolved"`` / ``"empty"``
    so the caller can keep the SAME run counters. A degenerate/failed game yields no
    records and a non-``"scored"`` status; it NEVER raises out of here — EXCEPT for
    :class:`UnresolvedParkFactorError`, which means the code itself regressed and
    must not be tallied as one skipped game.

    ``park_factor`` is the factor this game actually ran with. The caller counts how
    many games got a non-neutral one, so an operator can see whether the park nudge
    was live for this run.

    Deterministic: the only RNG is the per-iteration seed derived from ``base_seed``,
    so the returned records are byte-identical regardless of where this runs.
    """
    from api.routes.games import _resolve_state_or_error
    from simulation.prop_distributions import PropDistributionSet
    from simulation.results import GameSimSummary
    from simulation.sim_kwargs import resolve_asof_ymd, resolve_park_factor_onto_state
    from simulation.win_probability import win_probability

    # Read odds first — a game with NO odds rows is skipped (counts as 'no_odds').
    game_odds = await _fetch_game_odds(pool, game_pk) if do_game else {}
    prop_odds = await _fetch_prop_odds(pool, game_pk) if do_props else {}
    if not game_odds and not prop_odds:
        return [], [], "no_odds", 1.0

    try:
        state = await _resolve_state_or_error(pool, game_pk)
    except Exception as exc:  # noqa: BLE001 — skip games with UNRESOLVABLE DATA
        # SIM-454: the same split as _process_one_game. A missing lineup is one bad
        # game. A dead Postgres is every game, so it must not be booked as one.
        if _is_process_broken(exc):
            log.error("game %s: process-level failure while resolving state", game_pk)
            raise
        log.info("skip game %s (state unresolved: %s)", game_pk, type(exc).__name__)
        return [], [], "unresolved", 1.0

    # SIM-452: resolve the park factor HERE, where the connections live. The replay
    # below runs in a worker thread with no event loop, and the builder rejects an
    # unresolved state, so an edit that drops this line fails loudly instead of
    # replaying park-blind.
    park_factor = await resolve_park_factor_onto_state(state, pool, duck, int(game_pk))
    # SIM-538: resolve the point-in-time cutoff the SAME way — the day before this
    # game, so the replay below can never draw on a pool row or a player profile
    # dated after the game it is predicting. sim_kwargs_from_state (called inside
    # _collect_game_results) reads state.asof_ymd and, when set, carries it into
    # the simulator as "_asof_ymd" — see simulation/sim_kwargs.py.
    #
    # resolve_asof_ymd returns None in two DIFFERENT situations: a live game,
    # where no cutoff is needed (not this script — every game here is a past,
    # completed game), and a lookup failure (a DB hiccup, or a game_pk this
    # script was told to score that is not in raw.games). This script only
    # ever replays the second kind of None, so treat it as an unresolved
    # game and skip the replay — the same refusal SIM-452's park-factor
    # resolution already applies above, and for the same reason: replaying
    # anyway would let the sampler draw from the WHOLE pool, including this
    # game's own real plays, which is the exact leak this cutoff exists to
    # close.
    asof_ymd = await resolve_asof_ymd(pool, int(game_pk))
    if asof_ymd is None:
        log.info("skip game %s (point-in-time cutoff unresolved)", game_pk)
        return [], [], "unresolved", park_factor
    state.asof_ymd = asof_ymd

    results = await asyncio.to_thread(_collect_game_results, state, iterations, base_seed)
    if not results:
        return [], [], "empty", park_factor

    summary = GameSimSummary.from_results(results)
    wp = win_probability(summary, calibration_map=calibration_map)

    bets: list[BetRecord] = []
    accuracy: list[AccuracyRecord] = []
    pset = PropDistributionSet.from_results(results) if (do_props and prop_odds) else None

    if do_game and game_odds:
        bets.extend(score_game_markets(game_pk, wp, summary, game_odds, min_edge=min_edge))
        if score_accuracy:
            final_score = await _fetch_final_score(pool, game_pk)
            if final_score is not None:
                home_score, away_score = final_score
                accuracy.extend(
                    score_game_accuracy(game_pk, wp, summary, game_odds, home_score, away_score)
                )
    if do_props and prop_odds and pset is not None:
        bets.extend(score_prop_markets(game_pk, pset, prop_odds, min_edge=min_edge))
        if score_accuracy:
            pa_events = await _fetch_pa_events(pool, game_pk)
            batter_actuals, pitcher_actuals = real_props_from_pa_events(pa_events)
            accuracy.extend(
                score_prop_accuracy(game_pk, pset, prop_odds, batter_actuals, pitcher_actuals)
            )

    return bets, accuracy, "scored", park_factor


# ===========================================================================
# ACROSS-GAMES parallelism: a module-level, picklable worker + per-worker init
# ===========================================================================
#
# The lever (per the SIM-430 profiling note): per-game cost is the irreducible
# per-PA full-pool scoring (~1.5 s/iter) and the host is core-bound, so a single
# game can't go below ~30 s.  The fix is to run ~6 WHOLE GAMES AT ONCE: each
# forkserver worker runs one game serially (resolve → N sims → prop dists → read
# odds → CLV bet records) and holds only its own ~373 MB full-pool sampler cache
# (SIM-430), so 6 workers fit in ~2.2 GB.  The PARENT stays lean — it loads NO
# engine artifacts — so the forkserver workers never COW-inherit a big parent.


class WorkerInitError(RuntimeError):
    """A worker process could not build the resources every game needs (SIM-454).

    This is NOT "one bad game". It says the PROCESS is broken: no event loop, no
    Postgres pool, or no sim DuckDB. Every game this worker is handed will fail the
    same way, so :func:`_process_one_game` lets it out and :func:`_run_parallel`
    stops the run on it. Booking it as one more ``"unresolved"`` game is what turned
    a broken worker into a full season of silence that exited 0.
    """


def _is_process_broken(exc: BaseException) -> bool:
    """Say whether ``exc`` means "this PROCESS is broken", not "this game is bad".

    The per-game handlers must swallow bad DATA (a game with no ingested lineup, a
    degenerate boxscore) and must NOT swallow broken INFRASTRUCTURE. The difference
    matters because a swallowed infrastructure failure repeats on every remaining
    game: the run books 2,378 "unresolved" rows, prints an empty scoreboard, and
    exits 0. Bad data affects one game. Broken infrastructure affects all of them.

    A ``True`` here makes the caller re-raise, which stops the run with a non-zero
    exit and a named cause.
    """
    if isinstance(exc, WorkerInitError | ImportError | MemoryError):
        return True
    # A dead / unreachable Postgres, a closed pool, an exhausted connection limit.
    if isinstance(exc, OSError):
        return True
    try:
        import asyncpg
    except Exception:  # noqa: BLE001 — asyncpg absent → nothing more to classify
        return False
    # SIM-454: a real server under load reports exhaustion and shutdown as SERVER
    # errors, not as connection errors, so `PostgresConnectionError` (class 08)
    # alone misses them. `InsufficientResourcesError` is class 53 — too many
    # connections (53300), out of memory (53200). `OperatorInterventionError` is
    # class 57 — admin shutdown (57P01), crash shutdown (57P02), cannot connect
    # now (57P03). Every one of those breaks every remaining game, not this one.
    return isinstance(
        exc,
        asyncpg.InterfaceError
        | asyncpg.PostgresConnectionError
        | asyncpg.exceptions.InsufficientResourcesError
        | asyncpg.exceptions.OperatorInterventionError,
    )


#: Per-worker process globals (one set per forkserver worker). ``_WORKER_INITED``
#: guards the one-time lazy init; ``_WORKER_LOOP`` is this worker's dedicated
#: asyncio event loop; ``_WORKER_POOL`` is its single asyncpg connection pool. All
#: are amortized across every game the worker handles.
_WORKER_INITED: bool = False
_WORKER_LOOP: Any = None
_WORKER_POOL: Any = None
_WORKER_DSN: str = DEFAULT_DSN
#: SIM-452: this worker's OWN read-only sim-DuckDB connection. A DuckDB connection
#: is not fork-safe, so each worker opens its own instead of inheriting the parent's.
_WORKER_DUCK: Any = None
#: SIM-538: this worker's OWN loaded win-probability calibration map — mirrors the
#: DuckDB connection above (loaded once per worker, best-effort, never fatal;
#: falls back to IDENTITY_CALIBRATION so a missing/corrupt report degrades the
#: SAME way it does at API boot rather than crashing the worker).
_WORKER_CALIBRATION_MAP: Any = IDENTITY_CALIBRATION
#: SIM-454: the LATCHED init failure. Once init fails, this worker is broken for
#: good, so the next call re-raises this instead of building the loop and the pool
#: again. Without the latch every game retried the init and leaked one event loop
#: and one Postgres pool per retry.
_WORKER_INIT_ERROR: BaseException | None = None


def _close_worker_resources(loop: Any, pool: Any, duck: Any) -> None:
    """Release whatever a failed init managed to build (SIM-454).

    Order matters: the pool must close ON the loop that created it, so the loop
    closes last. Every step is best-effort — a cleanup that raises would mask the
    init failure the caller is about to report.
    """
    if pool is not None and loop is not None:
        try:
            loop.run_until_complete(pool.close())
        except Exception:  # noqa: BLE001 — best-effort release
            log.debug("worker init cleanup: pool.close() failed", exc_info=True)
    if duck is not None:
        try:
            duck.close()
        except Exception:  # noqa: BLE001 — best-effort release
            log.debug("worker init cleanup: duckdb.close() failed", exc_info=True)
    if loop is not None:
        try:
            asyncio.set_event_loop(None)
            loop.close()
        except Exception:  # noqa: BLE001 — best-effort release
            log.debug("worker init cleanup: loop.close() failed", exc_info=True)


def _worker_lazy_init(
    dsn: str,
    duckdb_path: str,
    *,
    allow_neutral_parks: bool = False,
    calibration_path: str | None = DEFAULT_CALIBRATION_PATH,
) -> None:
    """One-time per-worker setup (first call only; cheap no-op thereafter).

    Amortizes the big per-worker costs across all the games this worker
    handles:

      * ``(a)`` warm THIS worker's ~373 MB full-pool sampler cache ONCE via
        :func:`simulation.production_factory.warm_worker_cache` (the SIM-402 seam),
        so the worker's FIRST game is a warm-cache hit, not a cold artifact-load;
      * ``(b)`` open ONE dedicated asyncio loop + ONE asyncpg pool for this worker
        (each worker reads odds / resolves state on its own connection);
      * ``(c)`` open ONE read-only sim-DuckDB connection for this worker (SIM-452),
        which the park-factor lookup reads;
      * ``(d)`` load THIS worker's own win-probability calibration map once
        (SIM-538) — best-effort, like ``(a)``: a missing or corrupt report
        degrades to :data:`IDENTITY_CALIBRATION`, the same graceful fallback
        ``api/main.py``'s boot lifespan uses, never a worker failure.

    Guarded by the ``_WORKER_INITED`` module global so it runs exactly once per
    forkserver worker. The warm step is best-effort (full-pool off / missing
    artifacts → it returns False and the per-tile path warms lazily); the pool is
    required (a worker that can't reach Postgres can't score a game).

    The DuckDB open is required too — UNLESS the operator passed
    ``--allow-neutral-parks``. :func:`run` honours that flag and runs park-neutral.
    The worker used to ignore it and raise anyway, so that flag combination broke
    every worker while the parent believed it had permission to continue.

    SIM-454 — THIS FUNCTION IS ATOMIC. It builds the loop, the pool and the DuckDB
    connection into LOCALS, and publishes them to the module globals only when all
    three succeed. Any failure releases everything it built and raises
    :class:`WorkerInitError`. It used to publish each resource as it built it and
    set ``_WORKER_INITED = True`` only at the very end, so a failure at step (c)
    left an open loop and an open Postgres pool behind with the flag still False —
    and the caller's blanket ``except`` booked the game as ``"unresolved"`` and
    called the function again for the next game. At ``--workers 6`` over a 2,378-game
    season that leaked roughly 400 event loops and 400 Postgres pools per worker,
    and the run still exited 0.

    The failure is also LATCHED. A worker that failed once cannot succeed later, so
    the next call re-raises immediately and builds nothing.
    """
    global _WORKER_INITED, _WORKER_LOOP, _WORKER_POOL, _WORKER_DSN, _WORKER_DUCK
    global _WORKER_INIT_ERROR, _WORKER_CALIBRATION_MAP
    if _WORKER_INITED:
        return
    if _WORKER_INIT_ERROR is not None:
        # Latched: this worker is broken for good. Build nothing, leak nothing.
        raise _WORKER_INIT_ERROR

    loop: Any = None
    pool: Any = None
    duck: Any = None
    calib_map: Any = IDENTITY_CALIBRATION
    try:
        # (a) warm the full-pool sampler cache ONCE for this worker (best-effort).
        try:
            from simulation.production_factory import warm_worker_cache

            warm_worker_cache(None)
        except Exception:  # noqa: BLE001 — full-pool off / no artifacts → lazy warm
            pass

        # (b) one event loop + one asyncpg pool, owned by this worker for its lifetime.
        import asyncpg

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        pool = loop.run_until_complete(asyncpg.create_pool(dsn, min_size=1, max_size=2))

        # (c) this worker's own read-only sim DuckDB (SIM-452).
        from simulation.sim_kwargs import open_sim_duckdb

        duck = open_sim_duckdb(duckdb_path)
        if duck is None and not allow_neutral_parks:
            raise WorkerInitError(
                f"SIM-452: worker could not open the sim DuckDB at {duckdb_path!r}, so every "
                "park factor would silently fall back to a neutral 1.0. Fix --duckdb, or "
                "pass --allow-neutral-parks to accept a park-neutral run."
            )

        # (d) SIM-538: this worker's own calibration map (best-effort — never fatal).
        try:
            from api.state import load_calibration_report

            report = load_calibration_report(calibration_path)
            calib_map = (
                CalibrationMap.from_report(report) if report is not None else (IDENTITY_CALIBRATION)
            )
        except Exception as exc:  # noqa: BLE001 — a bad report must not sink the worker
            log.warning(
                "SIM-538: worker could not load calibration from %r (%s) — using identity.",
                calibration_path,
                exc,
            )
            calib_map = IDENTITY_CALIBRATION
    except BaseException as exc:
        # Release EVERYTHING this attempt built, whatever went wrong. This is the
        # whole point of building into locals: there is exactly one place that owns
        # the half-built state, and it is here.
        _close_worker_resources(loop, pool, duck)
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        if isinstance(exc, WorkerInitError):
            _WORKER_INIT_ERROR = exc
            raise
        err = WorkerInitError(f"worker init failed: {type(exc).__name__}: {exc}")
        _WORKER_INIT_ERROR = err
        raise err from exc

    # Publish only now: either every resource exists, or none of them do.
    _WORKER_DSN = dsn
    _WORKER_LOOP = loop
    _WORKER_POOL = pool
    _WORKER_DUCK = duck
    _WORKER_CALIBRATION_MAP = calib_map
    _WORKER_INITED = True
    atexit.register(_worker_shutdown)


def _worker_shutdown() -> None:
    """Release this worker's loop / pool / DuckDB at process exit (SIM-454).

    Registered with :mod:`atexit` after a successful init, so a worker hands its
    Postgres backends back when the pool shuts down instead of holding them until
    the OS reaps the process.
    """
    global _WORKER_INITED, _WORKER_LOOP, _WORKER_POOL, _WORKER_DUCK
    if not _WORKER_INITED:
        return
    _WORKER_INITED = False
    _close_worker_resources(_WORKER_LOOP, _WORKER_POOL, _WORKER_DUCK)
    _WORKER_LOOP = _WORKER_POOL = _WORKER_DUCK = None


def _process_one_game(game_pk: int, params: dict[str, Any]) -> dict[str, Any]:
    """Module-level, picklable across-games worker: score ONE game end-to-end.

    The function the ``ProcessPoolExecutor`` maps across the slate's ``game_pk``s.
    On its FIRST call in a given worker it runs :func:`_worker_lazy_init` (warm the
    sampler cache + open the asyncpg pool); every call then drives the SAME
    :func:`_score_one_game` pipeline on this worker's dedicated loop + pool.

    Returns a fully-picklable payload ``{"status": str, "bets": list[dict],
    "accuracy_records": list[dict]}`` where each dict is
    ``BetRecord.to_jsonable()`` / ``AccuracyRecord.to_jsonable()`` — the parent
    turns those back into their dataclasses and folds ``status`` into the SAME
    run counters the serial path keeps. Returning plain dicts (not the
    dataclasses) keeps the cross-process boundary robust to how this script
    module is named in the worker.

    A game with BAD DATA logs and contributes NO bets (status ``"unresolved"``) — it
    NEVER raises out, so one bad game can never sink the parallel run.

    A BROKEN PROCESS is the opposite case and it propagates:

      * :class:`UnresolvedParkFactorError` (SIM-452) says the code skipped the
        park-factor lookup. That is a defect in this script, not a bad game, and the
        old blanket ``except`` booked it as one more "unresolved" game while the
        whole run went park-blind.
      * :class:`WorkerInitError` (SIM-454) says this worker has no loop, no pool or
        no park-factor source. Every game it is handed fails identically, so
        swallowing it converts a broken worker into a whole season of "unresolved"
        rows and a zero exit code.
      * Anything :func:`_is_process_broken` recognises — a dead Postgres, an
        exhausted connection limit, an ``ImportError`` — for the same reason.
    """
    from simulation.sim_kwargs import UnresolvedParkFactorError

    dsn = str(params.get("dsn", DEFAULT_DSN))
    duckdb_path = str(params.get("duckdb", DEFAULT_DUCKDB_PATH))
    allow_neutral = bool(params.get("allow_neutral_parks", False))
    calibration_path = params.get("calibration_path", DEFAULT_CALIBRATION_PATH)
    try:
        _worker_lazy_init(
            dsn,
            duckdb_path,
            allow_neutral_parks=allow_neutral,
            calibration_path=calibration_path,
        )
        bets, accuracy_records, status, park_factor = _WORKER_LOOP.run_until_complete(
            _score_one_game(
                _WORKER_POOL,
                int(game_pk),
                duck=_WORKER_DUCK,
                do_game=bool(params["do_game"]),
                do_props=bool(params["do_props"]),
                score_accuracy=bool(params.get("score_accuracy", True)),
                iterations=int(params["iterations"]),
                base_seed=int(params["base_seed"]),
                min_edge=float(params["min_edge"]),
                calibration_map=_WORKER_CALIBRATION_MAP,
            )
        )
        return {
            "status": status,
            "bets": [b.to_jsonable() for b in bets],
            "accuracy_records": [a.to_jsonable() for a in accuracy_records],
            "park_run_factor": float(park_factor),
        }
    except UnresolvedParkFactorError:
        raise
    except Exception as exc:  # noqa: BLE001 — one BAD GAME never sinks the run
        if _is_process_broken(exc):
            # SIM-454: this worker is broken, not this game. Let it out so the run
            # stops with a named cause instead of booking every remaining game.
            log.error("worker: process-level failure on game %s — stopping the run", game_pk)
            raise
        log.warning("worker: game %s failed (%s) — no bets", game_pk, type(exc).__name__)
        return {"status": "unresolved", "bets": [], "accuracy_records": [], "park_run_factor": 1.0}


@dataclass
class _Counters:
    """Running tallies for the run summary log."""

    games_attempted: int = 0
    games_scored: int = 0
    games_no_odds: int = 0
    games_unresolved: int = 0
    bets: list[BetRecord] = field(default_factory=list)
    #: SIM-538: the sim-vs-closing-line accuracy comparison's raw observations.
    accuracy_records: list[AccuracyRecord] = field(default_factory=list)
    #: SIM-452: games that ran with a park factor the lookup actually moved off 1.0.
    #: A scored run with zero of these means the park nudge did nothing all run, and
    #: the operator gets to see that instead of guessing.
    games_park_nonneutral: int = 0


def _tally(counters: _Counters, status: str, park_factor: float = 1.0) -> None:
    """Fold one game's status into the run counters (shared by both paths)."""
    counters.games_attempted += 1
    if status == "scored" and abs(float(park_factor) - 1.0) > 1e-9:
        counters.games_park_nonneutral += 1
    if status == "no_odds":
        counters.games_no_odds += 1
    elif status == "unresolved":
        counters.games_unresolved += 1
    elif status == "scored":
        counters.games_scored += 1
    # "empty" -> attempted but produced no results: no scored/skip bucket (as before).


async def _run_serial(
    game_pks: list[int],
    *,
    duck: Any,
    do_game: bool,
    do_props: bool,
    score_accuracy: bool,
    calibration_map: Any,
    args: argparse.Namespace,
) -> _Counters:
    """SERIAL fallback (``--workers 1``): the original in-process path.

    Opens ONE asyncpg pool in this process and scores each game in turn via the
    SAME :func:`_score_one_game` pipeline the workers use — so this is the
    no-pool debug mode AND the reference the verify step compares against.

    ``duck`` is the parent's read-only sim-DuckDB connection (SIM-452); the park
    factor comes from it. ``calibration_map`` is the ONE calibration map this
    whole serial run scores every game's win probability through (SIM-538) —
    loaded once by :func:`run`, unlike the parallel path where each worker
    loads its own copy.
    """
    import asyncpg

    counters = _Counters()
    pool = None
    try:
        if game_pks:
            pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=4)
        for game_pk in game_pks:
            bets, accuracy_records, status, park_factor = await _score_one_game(
                pool,
                game_pk,
                duck=duck,
                do_game=do_game,
                do_props=do_props,
                score_accuracy=score_accuracy,
                iterations=args.iterations,
                base_seed=args.base_seed,
                min_edge=args.min_edge,
                calibration_map=calibration_map,
            )
            _tally(counters, status, park_factor)
            counters.bets.extend(bets)
            counters.accuracy_records.extend(accuracy_records)
            if counters.games_scored and counters.games_scored % 25 == 0:
                log.info(
                    "  scored %d/%d games (%d bet rows) ...",
                    counters.games_scored,
                    len(game_pks),
                    len(counters.bets),
                )
    finally:
        if pool is not None:
            await pool.close()
    return counters


def _run_parallel(
    game_pks: list[int],
    *,
    workers: int,
    do_game: bool,
    do_props: bool,
    score_accuracy: bool,
    calibration_path: str,
    args: argparse.Namespace,
) -> _Counters:
    """ACROSS-GAMES parallel path (``--workers > 1``).

    Builds a ``ProcessPoolExecutor`` with ``mp_context=forkserver`` (SIM-430) and
    maps :func:`_process_one_game` across the ``game_pk``s. Each worker runs a WHOLE
    game serially (its own ~373 MB sampler cache + asyncpg pool, lazily inited once);
    ``workers`` games run at once. Bet records are collected AS THEY COMPLETE, then
    the parent aggregates with the SAME :func:`aggregate_scoreboard` and emits the
    SAME scoreboard/JSON. The PARENT loads NO engine artifacts (stays lean so the
    forkserver workers never COW-inherit a big parent).

    Byte-identical to the serial path for the SAME (game set, base_seed, iterations):
    each game is independent and deterministic from its per-iteration seed
    (``derive_seed(base_seed, i)``), so completion order — the only thing parallelism
    changes — does not affect any game's bet records.
    """
    from concurrent.futures import BrokenExecutor, ProcessPoolExecutor, as_completed

    from simulation.sim_kwargs import UnresolvedParkFactorError

    worker_params: dict[str, Any] = {
        "do_game": do_game,
        "do_props": do_props,
        "score_accuracy": score_accuracy,
        "iterations": int(args.iterations),
        "base_seed": int(args.base_seed),
        "min_edge": float(args.min_edge),
        "dsn": args.dsn,
        # SIM-452: each worker opens this path read-only for the park-factor lookup.
        "duckdb": args.duckdb,
        # SIM-454: the workers must honour the SAME escape hatch the parent honours.
        # The parent used to accept --allow-neutral-parks and continue while every
        # worker still raised on the missing DuckDB, so the whole run booked
        # "unresolved" and exited 0.
        "allow_neutral_parks": bool(getattr(args, "allow_neutral_parks", False)),
        # SIM-538: a path, not a loaded map — trivially picklable. Each worker
        # loads its OWN calibration map once (_worker_lazy_init step (d)) rather
        # than the parent pickling one shared object into every submitted game.
        "calibration_path": calibration_path,
    }
    counters = _Counters()
    if not game_pks:
        return counters

    log.info(
        "ACROSS-GAMES parallel: %d games over %d forkserver workers (~373 MB each).",
        len(game_pks),
        workers,
    )
    with ProcessPoolExecutor(max_workers=workers, mp_context=_pool_mp_context()) as pool:
        futures = {
            pool.submit(_process_one_game, int(pk), worker_params): int(pk) for pk in game_pks
        }
        done = 0
        for fut in as_completed(futures):
            game_pk = futures[fut]
            try:
                payload = fut.result()
            except Exception as exc:  # noqa: BLE001 — split into fatal vs one bad game
                # A defect / a broken worker / a dead pool is NOT one bad game.
                #
                # SIM-454 (round 3): the fatal branch used to name a FIXED tuple —
                # (UnresolvedParkFactorError, WorkerInitError, BrokenExecutor) — while
                # `_is_process_broken` ALREADY classified OSError, MemoryError,
                # ImportError and the asyncpg connection errors as fatal. The two
                # lists had drifted, so a worker raising any of those was booked
                # "unresolved" and the run carried on to book the whole season the
                # same way and exit 0. That is the exact silent no-op this guard
                # exists to stop. One classifier now decides, so they cannot drift
                # apart again.
                fatal = isinstance(
                    exc, UnresolvedParkFactorError | BrokenExecutor
                ) or _is_process_broken(exc)
                if fatal:
                    # Cancel the queue FIRST: the `with` block's exit waits for every
                    # pending future, so without this the run would keep feeding games
                    # to broken workers for the rest of the season before the error
                    # surfaced.
                    log.error(
                        "STOPPING the run at game %s — %s: %s",
                        game_pk,
                        type(exc).__name__,
                        exc,
                    )
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
                log.warning("game %s worker crashed (%s)", game_pk, type(exc).__name__)
                _tally(counters, "unresolved")
                continue
            _tally(
                counters,
                str(payload.get("status", "unresolved")),
                float(payload.get("park_run_factor", 1.0)),
            )
            counters.bets.extend(BetRecord.from_jsonable(d) for d in payload.get("bets", []))
            counters.accuracy_records.extend(
                AccuracyRecord.from_jsonable(d) for d in payload.get("accuracy_records", [])
            )
            done += 1
            if done % 25 == 0:
                log.info(
                    "  completed %d/%d games (%d bet rows) ...",
                    done,
                    len(game_pks),
                    len(counters.bets),
                )
    return counters


def _open_parent_duckdb(args: argparse.Namespace) -> Any:
    """Open the sim DuckDB for the parent process, or return ``None`` (SIM-452).

    The park factor lives in ``derived.park_factors`` in this file. Without the file
    every game runs at a neutral 1.0 and ``SIM_PARK_FACTOR`` does nothing, so the
    whole backtest measures a different simulator than the one production serves.

    :func:`run` turns a ``None`` here into a non-zero exit unless the operator passes
    ``--allow-neutral-parks``.
    """
    from simulation.sim_kwargs import open_sim_duckdb

    return open_sim_duckdb(args.duckdb)


async def run(args: argparse.Namespace) -> int:
    from api.state import load_calibration_report
    from simulation.sim_kwargs import UnresolvedParkFactorError as _UnresolvedParkFactorError

    seasons = sorted({int(s) for s in args.seasons})
    do_game = args.markets in ("game", "all")
    do_props = args.markets in ("props", "all")
    workers = max(1, int(args.workers))
    score_accuracy = not args.no_accuracy_comparison
    log.info(
        "SIM-538 sim-vs-closing-line accuracy comparison — seasons=%s iterations=%d "
        "markets=%s workers=%d (secondary SIM-429 CLV report: min_edge=%s)",
        seasons,
        args.iterations,
        args.markets,
        workers,
        args.min_edge,
    )

    # SIM-538: load the SAME fitted calibration the live API applies at boot, ONCE,
    # in the parent. The serial path passes this map directly; the parallel path
    # passes only the PATH (worker_params must stay picklable) and each worker
    # loads its own copy (_worker_lazy_init step (d)) — best-effort either way, so
    # a missing/corrupt report degrades to IDENTITY_CALIBRATION rather than
    # aborting the run.
    calibration_report = await asyncio.to_thread(load_calibration_report, args.calibration_path)
    calibration_map = (
        CalibrationMap.from_report(calibration_report)
        if calibration_report is not None
        else IDENTITY_CALIBRATION
    )
    log.info(
        "SIM-538: win probability scored through %s.",
        calibration_map.name
        if calibration_report is not None
        else "IDENTITY_CALIBRATION (no report found)",
    )

    # SIM-452 PRECONDITION. Check the park-factor source BEFORE any sim runs. A
    # backtest that cannot read it is not a weaker backtest, it is a measurement of
    # a simulator nobody ships, so it refuses to start.
    duck = _open_parent_duckdb(args)
    if duck is None:
        if not args.allow_neutral_parks:
            log.error(
                "SIM-452: the sim DuckDB at %r would not open, so every park factor "
                "falls back to a neutral 1.0 and SIM_PARK_FACTOR is a no-op. This run "
                "would score CLV for a simulator production does not serve. Fix the "
                "path (--duckdb) or pass --allow-neutral-parks to accept it.",
                args.duckdb,
            )
            return EXIT_NO_PARK_SOURCE
        log.warning(
            "SIM-452: running WITHOUT park factors (--allow-neutral-parks). Every game "
            "uses a neutral 1.0 and SIM_PARK_FACTOR does nothing this run."
        )

    try:
        game_pks = await _fetch_final_games(args.dsn, seasons, args.max_games)
    except Exception:
        # SIM-454: the parent cannot reach Postgres. That used to escape as a raw
        # traceback out of asyncio.run; name it and exit on a documented code.
        log.exception("Could not read the game list from Postgres at %r.", args.dsn)
        if duck is not None:
            try:
                duck.close()
            except Exception:  # noqa: BLE001 — a close failure never masks the cause
                pass
        return EXIT_INFRASTRUCTURE
    log.info("Found %d completed games to backtest.", len(game_pks))
    if not game_pks:
        log.warning("No completed games found — nothing to backtest.")

    try:
        if workers <= 1:
            # SERIAL fallback (the original in-process path; the verify reference).
            counters = await _run_serial(
                game_pks,
                duck=duck,
                do_game=do_game,
                do_props=do_props,
                score_accuracy=score_accuracy,
                calibration_map=calibration_map,
                args=args,
            )
        else:
            # ACROSS-GAMES parallel — the pool is sync, so offload it off the loop.
            counters = await asyncio.to_thread(
                _run_parallel,
                game_pks,
                workers=workers,
                do_game=do_game,
                do_props=do_props,
                score_accuracy=score_accuracy,
                calibration_path=args.calibration_path,
                args=args,
            )
    except WorkerInitError:
        # SIM-454: a worker could not build its loop / pool / park-factor source.
        # Every remaining game would fail the same way. Stop with a named cause
        # instead of writing a season of "unresolved" rows and exiting 0.
        log.exception("SIM-454: worker initialisation failed — the run is ABANDONED.")
        return EXIT_WORKER_INIT
    except _UnresolvedParkFactorError:
        log.exception("SIM-452: a game replayed park-blind — the run is ABANDONED.")
        return EXIT_UNRESOLVED_PARK
    finally:
        if duck is not None:
            try:
                duck.close()
            except Exception:  # noqa: BLE001 — a close failure never sinks the run
                pass

    # SIM-452: say out loud how many scored games actually got a non-neutral park
    # factor. All-neutral over a real slate means the park nudge did nothing.
    if counters.games_scored and not counters.games_park_nonneutral:
        log.warning(
            "SIM-452: %d games scored and NOT ONE used a non-neutral park factor. "
            "SIM_PARK_FACTOR had no effect on this backtest.",
            counters.games_scored,
        )
    else:
        log.info(
            "SIM-452: %d/%d scored games ran with a non-neutral park factor.",
            counters.games_park_nonneutral,
            counters.games_scored,
        )

    scoreboard = aggregate_scoreboard(counters.bets)
    params = {
        "seasons": seasons,
        "iterations": int(args.iterations),
        "markets": args.markets,
        "min_edge": float(args.min_edge),
        "base_seed": int(args.base_seed),
        "workers": workers,
        "dsn": args.dsn,
        "duckdb": args.duckdb,
        "park_factors_available": duck is not None,
        "factory_ref": _FACTORY_REF,
        # SIM-538. "calibration_applied" reports whether the WIN-PROBABILITY MAP
        # actually used is calibrated -- not merely whether a report file
        # loaded. A report can load fine but carry no fitted reliability curve
        # (make calibrate ran; make validate-props --write-calibration has
        # not), in which case CalibrationMap.from_report falls back to the
        # IDENTITY_CALIBRATION singleton and every game is scored uncalibrated
        # even though the file exists. `calibration_report is not None` missed
        # that case (an adversarial review of SIM-538 confirmed it); comparing
        # the resolved map against the singleton is the honest check.
        "calibration_path": args.calibration_path,
        "calibration_applied": calibration_map is not IDENTITY_CALIBRATION,
        "score_accuracy": score_accuracy,
        "bootstrap_samples": int(args.bootstrap_samples),
        "bootstrap_seed": int(args.bootstrap_seed),
        # SIM-539: the minimum-sample-size methodology, recorded so a reader
        # of the JSON report knows exactly what "underpowered" was measured
        # against — see the module docstring's "MINIMUM SAMPLE SIZE" section.
        "min_detectable_edge": MIN_DETECTABLE_EDGE,
        "detection_margin": DETECTION_MARGIN,
        "base_alpha": DEFAULT_ALPHA,
    }

    accuracy_comparison: dict[str, Any] | None = None
    if score_accuracy:
        accuracy_comparison = aggregate_accuracy_comparison(
            counters.accuracy_records,
            n_bootstrap=int(args.bootstrap_samples),
            seed=int(args.bootstrap_seed),
        )
        print(format_accuracy_comparison(accuracy_comparison, params=params))
        print()
    print(format_scoreboard(scoreboard, params=params))

    report: dict[str, Any] = {
        "params": params,
        "counters": {
            "games_attempted": counters.games_attempted,
            "games_scored": counters.games_scored,
            "games_no_odds": counters.games_no_odds,
            "games_unresolved": counters.games_unresolved,
            "games_park_nonneutral": counters.games_park_nonneutral,
            "n_bets": len(counters.bets),
            "n_accuracy_records": len(counters.accuracy_records),
        },
        "scoreboard": scoreboard,
        "bets": [b.to_jsonable() for b in counters.bets],
    }
    if accuracy_comparison is not None:
        report["accuracy_comparison"] = accuracy_comparison
        report["accuracy_records"] = [a.to_jsonable() for a in counters.accuracy_records]
    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    log.info("Wrote CLV backtest report -> %s", args.output)

    # SIM-454: a run that priced NOTHING is a failure, not a result. The report is
    # already written (an operator still gets the artifact to diagnose from), but
    # the exit code says the backtest measured nothing. A whole season booked as
    # "unresolved" with a zero exit is the silent no-op this programme exists to end.
    if counters.games_scored == 0:
        log.error(
            "SIM-454: the backtest scored 0 games (attempted=%d, no_odds=%d, "
            "unresolved=%d) and placed %d bets. This run measured NOTHING. "
            "Exiting %d.",
            counters.games_attempted,
            counters.games_no_odds,
            counters.games_unresolved,
            len(counters.bets),
            EXIT_NOTHING_SCORED,
        )
        return EXIT_NOTHING_SCORED

    # A run that scored SOME games but lost most of them is still suspect. Say so
    # out loud; do not fail it, because a season legitimately holds bad games.
    if counters.games_unresolved > counters.games_scored:
        log.warning(
            "SIM-454: %d games came back UNRESOLVED against %d scored. Check the "
            "worker logs before you read the scoreboard as a measurement.",
            counters.games_unresolved,
            counters.games_scored,
        )
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Sim-vs-closing-line accuracy comparison (SIM-538), plus the secondary "
            "SIM-429 CLV scoreboard."
        )
    )
    p.add_argument("--seasons", type=int, nargs="+", required=True, help="Seasons to backtest.")
    p.add_argument("--max-games", type=int, default=None, help="Cap games (smoke run).")
    p.add_argument("--iterations", type=int, default=100, help="Monte-Carlo iters per game.")
    p.add_argument(
        "--markets",
        choices=("game", "props", "all"),
        default="all",
        help="Which markets to score (default all).",
    )
    p.add_argument(
        "--min-edge",
        type=float,
        default=0.0,
        help="Minimum model edge to PLACE a bet (default 0.0).",
    )
    p.add_argument("--base-seed", type=int, default=0, help="Reproducibility seed.")
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            "ACROSS-GAMES parallel workers (default %(default)s). Each forkserver "
            "worker runs a WHOLE game serially (~373 MB sampler cache each). "
            "--workers 1 is the SERIAL in-process fallback."
        ),
    )
    p.add_argument("--output", default=DEFAULT_OUTPUT, help="CLV backtest JSON report path.")
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (completed games + odds).")
    p.add_argument("--duckdb", default=DEFAULT_DUCKDB_PATH, help="Sim DuckDB path.")
    p.add_argument(
        "--allow-neutral-parks",
        action="store_true",
        help=(
            "SIM-452 escape hatch. Run even when the sim DuckDB will not open, with "
            "every park factor at a neutral 1.0. The run then measures a simulator "
            "production does not serve, so use it only for a plumbing smoke test."
        ),
    )
    p.add_argument(
        "--calibration-path",
        default=DEFAULT_CALIBRATION_PATH,
        help=(
            f"SIM-538: CalibrationReport JSON to score win probability through — "
            f"the SAME one the live API applies at boot (default: "
            f"{DEFAULT_CALIBRATION_PATH}). Missing/corrupt degrades to the identity "
            "map, same as the API's own boot fallback."
        ),
    )
    p.add_argument(
        "--no-accuracy-comparison",
        action="store_true",
        help=(
            "SIM-538: skip the sim-vs-closing-line accuracy comparison and produce "
            "only the secondary SIM-429 CLV scoreboard (faster smoke run)."
        ),
    )
    p.add_argument(
        "--bootstrap-samples",
        type=int,
        default=DEFAULT_BOOTSTRAP_SAMPLES,
        help=(
            "SIM-538: resamples for the paired-difference confidence interval "
            f"(default {DEFAULT_BOOTSTRAP_SAMPLES})."
        ),
    )
    p.add_argument(
        "--bootstrap-seed",
        type=int,
        default=538,
        help="SIM-538: seed for the paired bootstrap (deterministic re-runs).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
