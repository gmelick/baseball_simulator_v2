"""
tests/unit/test_sim421_backtest_props.py
========================================
The backtest wiring for the new prop markets (SIM-421) and the official
box-score ground truth (SIM-545), in ``scripts/clv_backtest.py`` and
``scripts/validate_props.py``.

What changed, in plain words: the platform now prices fifteen player-prop
markets, and a prop can only be graded against what the player really did.
The official box score (``raw.game_player_stats``) grades every priced
market — RBI and ER included, two props the platform priced but could not
grade before — and a player who did not play is absent from it, so his bet
is skipped the way the book voids it. A game with no box-score rows falls
back to the event label (``raw.pitches.events``), which grades only five
props. The scorer takes the per-game set of gradable props from whichever
source the game used, and the report counts games per source.

No DB, no network, no real sim. The ground-truth rows come from the real
captured payload ``tests/fixtures/mlb/boxscore_746437.json`` through the
SIM-545 parser and reader; the pool is a fake that routes each SELECT by the
table it names.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import types
from datetime import date

import pytest

# Import the two script modules by path (scripts/ is not a package). Each MUST
# be registered in sys.modules before exec_module so that
# @dataclass(slots=True) can resolve its module namespace.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))


def _load_script(name: str):
    path = os.path.join(_REPO_ROOT, "scripts", f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


clv_backtest = _load_script("clv_backtest")
validate_props = _load_script("validate_props")

from pipeline.etl.boxscore_ingest import parse_boxscore  # noqa: E402
from simulation.prop_distributions import (  # noqa: E402
    ALL_PROPS,
    PropDistribution,
    PropDistributionSet,
)
from simulation.prop_validation import (  # noqa: E402
    BOXSCORE_BATTER_PROPS,
    BOXSCORE_PITCHER_PROPS,
    DEFAULT_PROP_LINES,
    real_props_from_boxscore_rows,
)

PROP_VOCAB_MAP = clv_backtest.PROP_VOCAB_MAP
BOXSCORE_SCORED_PROPS = clv_backtest.BOXSCORE_SCORED_PROPS
EVENT_LABEL_SCORED_PROPS = clv_backtest.EVENT_LABEL_SCORED_PROPS
GROUND_TRUTH_OFFICIAL = clv_backtest.GROUND_TRUTH_OFFICIAL
GROUND_TRUTH_EVENT_LABEL = clv_backtest.GROUND_TRUTH_EVENT_LABEL
score_prop_accuracy = clv_backtest.score_prop_accuracy

_FIXTURE = os.path.join(_REPO_ROOT, "tests", "fixtures", "mlb", "boxscore_746437.json")
GAME_PK = 746437

# Players read off the fixture (see _fixture_rows). The arithmetic the tests
# assert is the reader's own: 1B = h - b2 - b3 - hr; HRR = r + h + rbi.
#: Home batter: h=2, hr=1, r=1, rbi=2, tb=5 -> 1B 1, HRR 5.
_SLUGGER = 595879
#: Home batter: h=1, r=1, sb=2.
_RUNNER = 678009
#: Away batter: h=1 (a double), sb=1 -> 2B 1, 1B 0.
_ROBLES = 645302
#: Away starter: p_outs=21, p_h=2, p_er=0, p_k=9, p_bb=0.
_STARTER = 682243
#: Home reliever: p_outs=17, p_h=1, p_er=0, p_k=5, p_bb=1.
_RELIEVER = 628317
#: Home pitcher listed on the roster with EMPTY batting and pitching stats —
#: he did not play, so the parser writes no row and the reader has no actual.
_DNP = 676428


def _fixture_rows() -> list[dict]:
    """The official box-score rows for the captured game, as plain dicts keyed
    by the ``raw.game_player_stats`` column names (the shape the SELECT returns)."""
    with open(_FIXTURE, encoding="utf-8") as fh:
        teams = json.load(fh)["teams"]
    parsed = parse_boxscore(teams, game_pk=GAME_PK, season=2024, game_date=date(2024, 8, 15))
    return [r.as_dict() for r in parsed]


def _dist(player_id: int, prop: str, samples) -> PropDistribution:
    return PropDistribution.from_samples(player_id=player_id, prop=prop, samples=samples)


def _pset(*dists: PropDistribution) -> PropDistributionSet:
    by_player: dict[int, dict[str, PropDistribution]] = {}
    for d in dists:
        by_player.setdefault(d.player_id, {})[d.prop] = d
    return PropDistributionSet(n_iterations=1000, by_player=by_player)


def _odds(odds_stat: str, player_id: int, line: float) -> dict:
    """One closing prop-odds row at a symmetric -110 / -110 price."""
    return {
        (player_id, odds_stat): {"closing": {"over_ml": -110.0, "under_ml": -110.0, "line": line}}
    }


#: A varied sample so every sim probability lands strictly inside (0, 1) —
#: prob_to_american raises on an exact 0 or 1 and the scorer would skip the
#: record as degenerate instead of exercising the path under test.
_SPREAD = [0, 0, 1, 1, 1, 2, 2, 3, 4, 5]
_WIDE = [1, 3, 5, 6, 8, 10, 12, 15, 18, 21]


class _FakePool:
    """Routes each SELECT by the table it names; records what was read.

    ``boxscore_rows`` is what ``raw.game_player_stats`` returns; ``pa_events``
    is what ``raw.pitches`` returns (already shaped as records with
    ``batter`` / ``pitcher`` / ``events`` keys). ``raise_missing_table`` makes
    the box-score SELECT raise asyncpg's ``UndefinedTableError``, the error a
    database without migration 0023 raises.
    """

    def __init__(self, *, boxscore_rows=(), pa_events=(), raise_missing_table=False):
        self.boxscore_rows = list(boxscore_rows)
        self.pa_events = list(pa_events)
        self.raise_missing_table = raise_missing_table
        self.tables_read: list[str] = []

    async def fetch(self, sql: str, *args):
        if "raw.game_player_stats" in sql:
            self.tables_read.append("raw.game_player_stats")
            if self.raise_missing_table:
                import asyncpg

                raise asyncpg.exceptions.UndefinedTableError(
                    'relation "raw.game_player_stats" does not exist'
                )
            return list(self.boxscore_rows)
        if "raw.pitches" in sql:
            self.tables_read.append("raw.pitches")
            return list(self.pa_events)
        if "raw.prop_odds" in sql:
            self.tables_read.append("raw.prop_odds")
            return []
        raise AssertionError(f"unexpected SELECT: {sql!r}")


# ---------------------------------------------------------------------------
# (a) the per-source prop sets
# ---------------------------------------------------------------------------


def test_official_set_grades_every_priced_market():
    """Every model prop the vocab maps to is gradable on the official box score
    — including RBI and ER, and every SIM-421 market."""
    assert set(PROP_VOCAB_MAP.values()) == BOXSCORE_SCORED_PROPS
    assert set(ALL_PROPS) == BOXSCORE_SCORED_PROPS
    assert set(BOXSCORE_BATTER_PROPS) | set(BOXSCORE_PITCHER_PROPS) == BOXSCORE_SCORED_PROPS
    assert {"RBI", "ER", "1B", "2B", "3B", "R", "SB", "HRR", "OUTS", "H_ALLOWED"} <= (
        BOXSCORE_SCORED_PROPS
    )


def test_fallback_set_is_the_five_event_label_props():
    assert {"H", "HR", "TB", "K", "BB"} == EVENT_LABEL_SCORED_PROPS
    assert EVENT_LABEL_SCORED_PROPS < BOXSCORE_SCORED_PROPS


# ---------------------------------------------------------------------------
# (b) _fetch_official_boxscore / _fetch_prop_ground_truth
# ---------------------------------------------------------------------------


async def test_fetch_official_boxscore_returns_every_row_as_a_dict():
    rows = _fixture_rows()
    pool = _FakePool(boxscore_rows=rows)
    out = await clv_backtest._fetch_official_boxscore(pool, GAME_PK)
    assert len(out) == len(rows) == 27
    assert all(isinstance(r, dict) for r in out)
    assert pool.tables_read == ["raw.game_player_stats"]


async def test_fetch_official_boxscore_selects_every_column():
    """The SELECT names every column of raw.game_player_stats, so the reader
    sees the whole row and a new prop never needs a second query."""
    sql = clv_backtest._OFFICIAL_BOXSCORE_SQL
    for column in _fixture_rows()[0]:
        assert column in sql, f"{column} is not selected"
    assert "fetched_at" in sql
    assert "WHERE game_pk = $1" in sql


async def test_missing_table_reads_as_no_rows_and_warns_once(monkeypatch, caplog):
    """A database without migration 0023 has no box-score table. The run must
    not fail on it: every game falls back to the event label, and the
    warning prints ONCE, not once per game."""
    monkeypatch.setattr(clv_backtest, "_OFFICIAL_BOXSCORE_TABLE_MISSING_WARNED", False)
    pool = _FakePool(raise_missing_table=True)
    with caplog.at_level(logging.WARNING, logger="clv_backtest"):
        assert await clv_backtest._fetch_official_boxscore(pool, 1) == []
        assert await clv_backtest._fetch_official_boxscore(pool, 2) == []
    warnings = [r for r in caplog.records if "SIM-545" in r.getMessage()]
    assert len(warnings) == 1
    assert "migration 0023" in warnings[0].getMessage()


async def test_ground_truth_prefers_the_official_box_score():
    """A game with box-score rows is graded on them; the event label is never
    read for it."""
    pool = _FakePool(
        boxscore_rows=_fixture_rows(),
        pa_events=[{"batter": _SLUGGER, "pitcher": _STARTER, "events": "single"}],
    )
    truth = await clv_backtest._fetch_prop_ground_truth(pool, GAME_PK)
    assert truth.source == GROUND_TRUTH_OFFICIAL
    assert truth.scorable_props == BOXSCORE_SCORED_PROPS
    assert pool.tables_read == ["raw.game_player_stats"]
    expected_batter, expected_pitcher = real_props_from_boxscore_rows(_fixture_rows())
    assert truth.batter_actuals == expected_batter
    assert truth.pitcher_actuals == expected_pitcher
    assert truth.batter_actuals[_SLUGGER] == {
        "H": 2,
        "HR": 1,
        "TB": 5,
        "RBI": 2,
        "1B": 1,
        "2B": 0,
        "3B": 0,
        "R": 1,
        "SB": 0,
        "HRR": 5,
    }
    assert truth.pitcher_actuals[_STARTER] == {
        "K": 9,
        "BB": 0,
        "ER": 0,
        "OUTS": 21,
        "H_ALLOWED": 2,
    }


async def test_ground_truth_falls_back_to_the_event_label_when_no_rows():
    pool = _FakePool(
        boxscore_rows=[],
        pa_events=[
            {"batter": _SLUGGER, "pitcher": _STARTER, "events": "home_run"},
            {"batter": _SLUGGER, "pitcher": _STARTER, "events": "strikeout"},
        ],
    )
    truth = await clv_backtest._fetch_prop_ground_truth(pool, GAME_PK)
    assert truth.source == GROUND_TRUTH_EVENT_LABEL
    assert truth.scorable_props == EVENT_LABEL_SCORED_PROPS
    assert pool.tables_read == ["raw.game_player_stats", "raw.pitches"]
    # The event label carries only the five props — no RBI, no ER.
    assert truth.batter_actuals[_SLUGGER] == {"H": 1, "HR": 1, "TB": 4}
    assert truth.pitcher_actuals[_STARTER] == {"K": 1, "BB": 0}


# ---------------------------------------------------------------------------
# (c) score_prop_accuracy on the official path: every new prop, RBI and ER
# ---------------------------------------------------------------------------


def _every_market_case():
    """Fifteen odds rows — one per priced market — each with a PropDistribution
    and a real total on the fixture's own rows, plus the expected outcome."""
    pset = _pset(
        # batter markets
        _dist(_SLUGGER, "H", _SPREAD),
        _dist(_SLUGGER, "HR", _SPREAD),
        _dist(_SLUGGER, "TB", _WIDE),
        _dist(_SLUGGER, "RBI", _SPREAD),
        _dist(_SLUGGER, "1B", _SPREAD),
        _dist(_ROBLES, "2B", _SPREAD),
        _dist(_SLUGGER, "3B", _SPREAD),
        _dist(_SLUGGER, "R", _SPREAD),
        _dist(_RUNNER, "SB", _SPREAD),
        _dist(_SLUGGER, "HRR", _WIDE),
        # pitcher markets
        _dist(_STARTER, "K", _WIDE),
        _dist(_RELIEVER, "BB", _SPREAD),
        _dist(_STARTER, "ER", _SPREAD),
        _dist(_STARTER, "OUTS", _WIDE),
        _dist(_STARTER, "H_ALLOWED", _SPREAD),
    )
    prop_odds = {
        **_odds("hits", _SLUGGER, 1.5),  # 2 > 1.5
        **_odds("home_runs", _SLUGGER, 0.5),  # 1 > 0.5
        **_odds("total_bases", _SLUGGER, 5.5),  # 5 < 5.5
        **_odds("rbis", _SLUGGER, 1.5),  # 2 > 1.5
        **_odds("singles", _SLUGGER, 0.5),  # 1B = 2-0-0-1 = 1 > 0.5
        **_odds("doubles", _ROBLES, 0.5),  # 1 > 0.5
        **_odds("triples", _SLUGGER, 0.5),  # 0 < 0.5
        **_odds("runs", _SLUGGER, 0.5),  # 1 > 0.5
        **_odds("stolen_bases", _RUNNER, 1.5),  # 2 > 1.5
        **_odds("hits_runs_rbis", _SLUGGER, 4.5),  # 1+2+2 = 5 > 4.5
        **_odds("strikeouts", _STARTER, 7.5),  # 9 > 7.5
        **_odds("walks", _RELIEVER, 1.5),  # 1 < 1.5
        **_odds("earned_runs", _STARTER, 0.5),  # 0 < 0.5
        **_odds("outs_recorded", _STARTER, 17.5),  # 21 > 17.5
        **_odds("hits_allowed", _STARTER, 3.5),  # 2 < 3.5
    }
    expected_outcome = {
        "H": 1,
        "HR": 1,
        "TB": 0,
        "RBI": 1,
        "1B": 1,
        "2B": 1,
        "3B": 0,
        "R": 1,
        "SB": 1,
        "HRR": 1,
        "K": 1,
        "BB": 0,
        "ER": 0,
        "OUTS": 1,
        "H_ALLOWED": 0,
    }
    return pset, prop_odds, expected_outcome


def test_official_path_scores_every_priced_market():
    pset, prop_odds, expected_outcome = _every_market_case()
    batter, pitcher = real_props_from_boxscore_rows(_fixture_rows())
    recs = score_prop_accuracy(
        GAME_PK, pset, prop_odds, batter, pitcher, scorable_props=BOXSCORE_SCORED_PROPS
    )
    by_market = {r.market: r for r in recs}
    assert set(by_market) == set(PROP_VOCAB_MAP.values())
    assert len(recs) == 15
    for market, rec in by_market.items():
        assert rec.outcome == expected_outcome[market], market
        assert rec.market_type == "prop"
        assert rec.game_pk == GAME_PK
        assert 0.0 < rec.sim_prob < 1.0
        assert 0.0 < rec.market_prob < 1.0
        # SIM-540 needs the raw closing prices on every record.
        assert rec.market_side_price == pytest.approx(-110.0)
        assert rec.market_other_price == pytest.approx(-110.0)
    # The two props the platform priced but could not grade before SIM-545.
    assert by_market["RBI"].player_id == _SLUGGER
    assert by_market["ER"].player_id == _STARTER


def test_fallback_path_grades_only_the_five_event_label_props():
    """The SAME odds, distributions and actuals, gated by the fallback set:
    RBI, ER and every SIM-421 market drop out. The set is the gate, not the
    presence of an actual."""
    pset, prop_odds, _ = _every_market_case()
    batter, pitcher = real_props_from_boxscore_rows(_fixture_rows())
    recs = score_prop_accuracy(
        GAME_PK, pset, prop_odds, batter, pitcher, scorable_props=EVENT_LABEL_SCORED_PROPS
    )
    assert {r.market for r in recs} == {"H", "HR", "TB", "K", "BB"}


def test_official_path_skips_a_player_who_did_not_play():
    """The listed-but-did-not-play pitcher has an odds row and a model
    distribution but no box-score row, so no actual: the book voids the bet
    and the scorer skips him rather than grading against a guessed zero."""
    batter, pitcher = real_props_from_boxscore_rows(_fixture_rows())
    assert _DNP not in batter and _DNP not in pitcher
    pset = _pset(_dist(_DNP, "K", _WIDE), _dist(_DNP, "OUTS", _WIDE))
    prop_odds = {**_odds("strikeouts", _DNP, 4.5), **_odds("outs_recorded", _DNP, 11.5)}
    recs = score_prop_accuracy(
        GAME_PK, pset, prop_odds, batter, pitcher, scorable_props=BOXSCORE_SCORED_PROPS
    )
    assert recs == []


def test_batter_hits_market_on_a_pitcher_never_reads_his_hits_allowed():
    """``hits`` (287) is the batter market. A pitcher who did not bat has no
    batter actual, and his pitcher totals hold ``H_ALLOWED``, not ``H`` — so a
    hits row on him is skipped, never graded against the hits he gave up."""
    batter, pitcher = real_props_from_boxscore_rows(_fixture_rows())
    assert _STARTER not in batter
    assert pitcher[_STARTER]["H_ALLOWED"] == 2
    pset = _pset(_dist(_STARTER, "H", _SPREAD))
    recs = score_prop_accuracy(
        GAME_PK,
        pset,
        _odds("hits", _STARTER, 0.5),
        batter,
        pitcher,
        scorable_props=BOXSCORE_SCORED_PROPS,
    )
    assert recs == []


def test_push_is_skipped_on_the_new_markets_too():
    batter, pitcher = real_props_from_boxscore_rows(_fixture_rows())
    pset = _pset(_dist(_RUNNER, "SB", _SPREAD))
    recs = score_prop_accuracy(
        GAME_PK,
        pset,
        _odds("stolen_bases", _RUNNER, 2.0),  # actual 2 == line
        batter,
        pitcher,
        scorable_props=BOXSCORE_SCORED_PROPS,
    )
    assert recs == []


# ---------------------------------------------------------------------------
# (d) the per-source game counts: _tally, the worker payload, the report
# ---------------------------------------------------------------------------


def test_tally_counts_scored_games_per_source():
    c = clv_backtest._Counters()
    clv_backtest._tally(c, "scored", 1.0, GROUND_TRUTH_OFFICIAL)
    clv_backtest._tally(c, "scored", 1.0, GROUND_TRUTH_OFFICIAL)
    clv_backtest._tally(c, "scored", 1.0, GROUND_TRUTH_EVENT_LABEL)
    clv_backtest._tally(c, "scored", 1.0, None)  # game markets only — no prop source
    clv_backtest._tally(c, "no_odds", 1.0, None)
    clv_backtest._tally(c, "unresolved", 1.0, GROUND_TRUTH_OFFICIAL)  # never scored
    assert c.games_official_boxscore == 2
    assert c.games_event_label == 1
    assert c.games_scored == 4
    assert c.games_attempted == 6


def test_tally_keeps_its_old_call_shape():
    """The existing callers pass no source; both counters stay at zero."""
    c = clv_backtest._Counters()
    clv_backtest._tally(c, "scored", 1.18)
    assert c.games_scored == 1
    assert c.games_park_nonneutral == 1
    assert c.games_official_boxscore == 0
    assert c.games_event_label == 0


def test_worker_payload_carries_the_ground_truth_source(monkeypatch):
    """The parallel path folds the source into the SAME counters the serial
    path keeps, so it must cross the process boundary in the payload."""
    monkeypatch.setattr(clv_backtest, "_worker_lazy_init", lambda *_a, **_k: None)

    class _Loop:
        def run_until_complete(self, coro):
            coro.close()
            return [], "scored", 1.0, GROUND_TRUTH_OFFICIAL

    monkeypatch.setattr(clv_backtest, "_WORKER_LOOP", _Loop())
    params = {
        "do_game": False,
        "do_props": True,
        "iterations": 1,
        "base_seed": 0,
        "dsn": "postgresql://x/y",
        "duckdb": "/nope.duckdb",
    }
    payload = clv_backtest._process_one_game(777, params)
    assert payload["ground_truth_source"] == GROUND_TRUTH_OFFICIAL
    assert payload["status"] == "scored"


def test_console_header_shows_the_per_source_counts():
    comparison = clv_backtest.aggregate_accuracy_comparison([], n_bootstrap=50, seed=1)
    params = {
        "seasons": [2024],
        "iterations": 100,
        "markets": "props",
        "base_seed": 0,
        "calibration_applied": True,
    }
    with_counts = clv_backtest.format_accuracy_comparison(
        comparison,
        params=params,
        counters={"games_official_boxscore": 41, "games_event_label": 3},
    )
    assert "official box score=41 games" in with_counts
    assert "event-label fallback=3 games" in with_counts
    # Without a run behind it (a synthetic table), no source line at all.
    without = clv_backtest.format_accuracy_comparison(comparison, params=params)
    assert "official box score=" not in without


async def test_report_counters_carry_the_per_source_game_counts(monkeypatch, tmp_path):
    """The JSON report's ``counters`` block names both sources, even on a run
    that found no games (both zero) — a dashboard reading the JSON must always
    find the keys."""
    import simulation.sim_kwargs as sk

    class _Con:
        def execute(self, *_a, **_k):
            raise AssertionError("no park lookup on an empty slate")

        def close(self):
            return None

    monkeypatch.setattr(sk, "open_sim_duckdb", lambda *_a, **_k: _Con())

    async def _no_games(*_a, **_k):
        return []

    monkeypatch.setattr(clv_backtest, "_fetch_final_games", _no_games)
    out = tmp_path / "clv.json"
    args = clv_backtest.parse_args(
        [
            "--seasons",
            "2024",
            "--output",
            str(out),
            "--workers",
            "1",
            "--calibration-path",
            str(tmp_path / "no_calibration.json"),
        ]
    )
    rc = await clv_backtest.run(args)
    assert rc == clv_backtest.EXIT_NOTHING_SCORED
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["counters"]["games_official_boxscore"] == 0
    assert report["counters"]["games_event_label"] == 0
    assert report["counters"]["games_scored"] == 0


# ---------------------------------------------------------------------------
# (e) _score_one_game: the per-game source selection, end to end
# ---------------------------------------------------------------------------


def _wire_score_one_game(monkeypatch, pset: PropDistributionSet, prop_odds: dict):
    """Stub every seam around the ground-truth choice so ``_score_one_game``
    runs with no lineup, no replay and no DuckDB.

    ``_score_one_game`` lazy-imports one name from ``api.routes.games``; a
    fake module in ``sys.modules`` supplies it, which keeps this test
    independent of the web framework the real module boots.
    """
    import simulation.prop_distributions as pd_mod
    import simulation.results as results_mod
    import simulation.sim_kwargs as sk
    import simulation.win_probability as wp_mod

    fake_state = types.SimpleNamespace(asof_ymd=None)

    async def _resolve_state(pool, game_pk):
        return fake_state

    fake_games = types.ModuleType("api.routes.games")
    fake_games._resolve_state_or_error = _resolve_state  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "api.routes.games", fake_games)

    async def _park(state, pool, duck, game_pk):
        return 1.0

    async def _asof(pool, game_pk):
        return "2024-08-14"

    async def _prop_odds(pool, game_pk):
        return prop_odds

    monkeypatch.setattr(sk, "resolve_park_factor_onto_state", _park)
    monkeypatch.setattr(sk, "resolve_asof_ymd", _asof)
    monkeypatch.setattr(clv_backtest, "_fetch_prop_odds", _prop_odds)
    # SIM-421 (2026-09-12): the replay returns the results AND the per-iteration
    # inning grids; no grids here, so the segment-market scorer stays idle.
    monkeypatch.setattr(clv_backtest, "_replay_game", lambda *_a, **_k: ([object()], []))
    monkeypatch.setattr(
        results_mod.GameSimSummary, "from_results", classmethod(lambda cls, r: object())
    )
    monkeypatch.setattr(wp_mod, "win_probability", lambda summary, calibration_map=None: object())
    monkeypatch.setattr(
        pd_mod.PropDistributionSet, "from_results", classmethod(lambda cls, r: pset)
    )


async def test_score_one_game_grades_on_the_official_box_score(monkeypatch):
    pset, prop_odds, _ = _every_market_case()
    _wire_score_one_game(monkeypatch, pset, prop_odds)
    pool = _FakePool(boxscore_rows=_fixture_rows())
    recs, status, park_factor, source = await clv_backtest._score_one_game(
        pool, GAME_PK, duck=object(), do_game=False, do_props=True, iterations=1, base_seed=0
    )
    assert status == "scored"
    assert source == GROUND_TRUTH_OFFICIAL
    assert {r.market for r in recs} == set(PROP_VOCAB_MAP.values())
    assert "raw.pitches" not in pool.tables_read


async def test_score_one_game_falls_back_per_game(monkeypatch):
    pset, prop_odds, _ = _every_market_case()
    _wire_score_one_game(monkeypatch, pset, prop_odds)
    pool = _FakePool(
        boxscore_rows=[],
        pa_events=[
            {"batter": _SLUGGER, "pitcher": _STARTER, "events": "home_run"},
            {"batter": _SLUGGER, "pitcher": _STARTER, "events": "single"},
            {"batter": _SLUGGER, "pitcher": _RELIEVER, "events": "walk"},
            {"batter": _SLUGGER, "pitcher": _STARTER, "events": "strikeout"},
        ],
    )
    recs, status, _pf, source = await clv_backtest._score_one_game(
        pool, GAME_PK, duck=object(), do_game=False, do_props=True, iterations=1, base_seed=0
    )
    assert status == "scored"
    assert source == GROUND_TRUTH_EVENT_LABEL
    assert {r.market for r in recs} <= EVENT_LABEL_SCORED_PROPS
    assert "RBI" not in {r.market for r in recs}
    assert pool.tables_read == ["raw.game_player_stats", "raw.pitches"]


async def test_score_one_game_has_no_source_when_props_are_off(monkeypatch):
    pset, prop_odds, _ = _every_market_case()
    _wire_score_one_game(monkeypatch, pset, prop_odds)

    async def _game_odds(pool, game_pk):
        return {"moneyline": {"closing": {"home_ml": -120, "away_ml": 100}}}

    async def _final_score(pool, game_pk):
        return None  # no real score → no game records, but the game still scored

    monkeypatch.setattr(clv_backtest, "_fetch_game_odds", _game_odds)
    monkeypatch.setattr(clv_backtest, "_fetch_final_score", _final_score)
    pool = _FakePool(boxscore_rows=_fixture_rows())
    recs, status, _pf, source = await clv_backtest._score_one_game(
        pool, GAME_PK, duck=object(), do_game=True, do_props=False, iterations=1, base_seed=0
    )
    assert status == "scored"
    assert source is None
    assert recs == []
    assert pool.tables_read == []


# ---------------------------------------------------------------------------
# (f) scripts/validate_props.py: the same source choice, pairing every market
# ---------------------------------------------------------------------------


async def test_validate_props_pairs_every_market_on_the_official_box_score():
    pset, _odds_unused, _ = _every_market_case()
    pool = _FakePool(boxscore_rows=_fixture_rows())
    pairs: dict[tuple[str, float], list] = {}
    added, source = await validate_props._pair_game_props(
        pool, GAME_PK, pset, pairs, official_boxscore=True
    )
    assert source == validate_props.GROUND_TRUTH_OFFICIAL
    assert added == 15
    assert {prop for prop, _line in pairs} == set(ALL_PROPS)
    # Every pair sits on the prop's default line, with the box's own total.
    assert pairs[("RBI", DEFAULT_PROP_LINES["RBI"])][0][1] == 2
    assert pairs[("OUTS", DEFAULT_PROP_LINES["OUTS"])][0][1] == 21
    assert pairs[("HRR", DEFAULT_PROP_LINES["HRR"])][0][1] == 5
    assert pool.tables_read == ["raw.game_player_stats"]


async def test_validate_props_falls_back_per_game_to_the_event_label():
    pset, _odds_unused, _ = _every_market_case()
    pool = _FakePool(
        boxscore_rows=[],
        pa_events=[{"batter": _SLUGGER, "pitcher": _STARTER, "events": "home_run"}],
    )
    pairs: dict[tuple[str, float], list] = {}
    added, source = await validate_props._pair_game_props(
        pool, GAME_PK, pset, pairs, official_boxscore=True
    )
    assert source == validate_props.GROUND_TRUTH_EVENT_LABEL
    assert {prop for prop, _line in pairs} <= {"H", "HR", "TB", "K", "BB"}
    assert added == len([p for bucket in pairs.values() for p in bucket])
    assert pool.tables_read == ["raw.game_player_stats", "raw.pitches"]


async def test_validate_props_flag_off_never_reads_the_box_score():
    """``--no-official-boxscore`` pairs every game on the event label alone."""
    pset, _odds_unused, _ = _every_market_case()
    pool = _FakePool(
        boxscore_rows=_fixture_rows(),
        pa_events=[{"batter": _SLUGGER, "pitcher": _STARTER, "events": "single"}],
    )
    pairs: dict[tuple[str, float], list] = {}
    _added, source = await validate_props._pair_game_props(
        pool, GAME_PK, pset, pairs, official_boxscore=False
    )
    assert source == validate_props.GROUND_TRUTH_EVENT_LABEL
    assert pool.tables_read == ["raw.pitches"]
    assert "RBI" not in {prop for prop, _line in pairs}


def test_validate_props_flag_defaults_on():
    on = validate_props.parse_args(["--seasons", "2024"])
    off = validate_props.parse_args(["--seasons", "2024", "--no-official-boxscore"])
    assert on.official_boxscore is True
    assert off.official_boxscore is False


def test_validate_props_summary_prints_the_per_source_counts():
    from simulation.prop_validation import PropValidationReport

    text = validate_props._summary_text(
        PropValidationReport(),
        ground_truth_counts={
            validate_props.GROUND_TRUTH_OFFICIAL: 7,
            validate_props.GROUND_TRUTH_EVENT_LABEL: 2,
        },
    )
    assert "official box score=7 games" in text
    assert "event-label fallback=2 games" in text
    # The old call shape still renders.
    assert "official box score=" not in validate_props._summary_text(PropValidationReport())
