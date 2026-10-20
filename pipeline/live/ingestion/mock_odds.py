"""The mock odds source: deterministic lines for tests and for a stack with no
odds vendor key.
"""

from __future__ import annotations

import random
from typing import Any

from pipeline.odds_provider import (
    GAME_MARKET_KIND,
    GAME_MARKET_TYPES,
    GAME_ODDS_FIELDS,
    LEGACY_GAME_MARKET_TYPES,
)

# ---------------------------------------------------------------------------
# Mock Odds API
# ---------------------------------------------------------------------------


class MockOddsAPI:
    """
    Deterministic mock that generates plausible MLB betting lines seeded on
    game_pk.  All values are consistent within a session (same game_pk always
    returns the same lines) so the frontend never sees odds flicker.

    It is the deterministic default behind the provider seam
    (get_odds_provider()); ODDS_PROVIDER=bettingpros switches the live pipeline
    to the real BettingProsOddsProvider. It serves local development and the
    tests.

    American odds encoding:
      Favourite:  odds = -(prob / (1-prob)) * 100  (negative integer)
      Underdog:   odds = ((1-prob) / prob) * 100   (positive integer)

    ``get_prop_odds()`` serves all fifteen prop markets.
    """

    # ------------------------------------------------------------------
    # Prop line configuration
    # Betting Analyst (Agent 8) approved centers and vig ranges.
    # Format: (line_center, half_spread, over_vig_range, under_vig_range)
    #   line_center:  base prop line (snapped to nearest 0.5 by get_prop_odds)
    #   half_spread:  ± RNG window added to line_center before snapping
    #   over/under vig: (min, max) tuple for American odds juice
    #
    # Design notes:
    #   - Strikeouts: widest window; pitcher quality variance is large.
    #   - Home runs / RBIs set at 0.5; these are binary-feel props.
    #   - Juice asymmetry on HR (books shade the under) reflects sharp-book
    #     behaviour Betting Analyst observed in real markets.
    #   - Doubles, triples and stolen bases sit at 0.5 with a
    #     plus-money over (a rare event); outs recorded centres on a 5.2-inning
    #     start (16.5); hits allowed on 4.5; hits+runs+RBIs on 1.5.
    # The keys are exactly PROP_STATS (a unit test enforces it).
    # ------------------------------------------------------------------
    _PROP_CONFIG: dict[str, tuple[float, float, tuple[int, int], tuple[int, int]]] = {
        #  prop_stat       center  ±spread  over_vig        under_vig
        "strikeouts": (5.5, 1.0, (-125, -105), (-125, -105)),
        "hits": (0.5, 0.5, (-115, -105), (-115, -105)),
        "home_runs": (0.5, 0.0, (-130, -110), (+100, +110)),
        "earned_runs": (3.5, 1.0, (-115, -105), (-115, -105)),
        "walks": (2.5, 0.5, (-115, -105), (-115, -105)),
        "total_bases": (1.5, 0.5, (-120, -105), (-115, -105)),
        "rbis": (0.5, 0.5, (-120, -110), (-110, -100)),
        # The eight markets the book already offers.
        "singles": (0.5, 0.5, (-120, -105), (-115, -100)),
        "doubles": (0.5, 0.0, (+150, +200), (-250, -190)),
        "triples": (0.5, 0.0, (+600, +900), (-1400, -1000)),
        "runs": (0.5, 0.5, (-125, -105), (-120, -100)),
        "stolen_bases": (0.5, 0.0, (+250, +450), (-650, -350)),
        "hits_runs_rbis": (1.5, 1.0, (-120, -105), (-120, -105)),
        "outs_recorded": (16.5, 1.5, (-120, -105), (-120, -105)),
        "hits_allowed": (4.5, 1.0, (-120, -105), (-120, -105)),
    }

    @staticmethod
    def _prob_to_american(prob: float) -> int:
        prob = max(0.01, min(0.99, prob))
        if prob >= 0.5:
            return round(-(prob / (1.0 - prob)) * 100)
        else:
            return round(((1.0 - prob) / prob) * 100)

    @staticmethod
    def get_odds(
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
        book: str = "consensus",
        is_sharp_book: bool = False,
    ) -> dict[str, Any]:
        """
        Returns mock betting lines for a game.

        Added book, line_type, market_type, is_sharp_book fields so all
        raw.game_odds inserts populate the four CLV-enabling columns.

        Parameters
        ----------
        game_pk       : MLB game identifier (seeds the RNG for determinism)
        line_type     : 'opening' | 'current' | 'closing' | 'bet_placement'
        market_type   : any value of ``GAME_MARKET_TYPES`` (``run_line`` is the
                        legacy alias of ``runline``); an unknown value raises
                        ``ValueError`` like the real provider does
        book          : book identifier (e.g. 'consensus', 'pinnacle', 'draftkings')
        is_sharp_book : TRUE for sharp books used as CLV reference (e.g. Pinnacle, Circa)

        The three full-game markets share one row shape (it carries the
        moneyline, the run line and the total). A
        segment or team market fills only its own fields, the way the real
        provider does — a first-inning moneyline gets a ``draw_ml`` (a segment
        can end tied), a team total a lower line, a yes / no market an over /
        under pair at 0.5.
        """
        canonical = "runline" if market_type == "run_line" else market_type
        if canonical not in GAME_MARKET_KIND:
            known = ", ".join(GAME_MARKET_TYPES)
            raise ValueError(f"Unknown market_type '{market_type}'. Known values: {known}")
        rng = random.Random(game_pk)

        # Home win probability: slight home-field edge built in
        home_win_prob = rng.uniform(0.38, 0.64)
        away_win_prob = 1.0 - home_win_prob

        # Apply realistic book vig so mock lines don't produce zero-
        # overround prices. Without vig, the implied probabilities sum to
        # exactly 1.0, which no real book does, and every edge, calibration
        # target and display built on the mock would read 3-8 points larger
        # than on real lines.
        #
        # vig is the total overround, split evenly between home and away:
        #   home_inflated = home_win_prob * (1 + vig/2)
        #   away_inflated = away_win_prob * (1 + vig/2)
        #   sum of inflated probs = 1 + vig/2  ∈ [1.03, 1.05]
        #
        # Range chosen so (home_implied + away_implied) > 1.03 for every
        # game_pk — the acceptance-criteria assertion in test_live_pipeline.py.
        # Real sharp-book MLB overround is ~3–5 %; soft-book ~6–8 %.
        vig = rng.uniform(0.06, 0.10)
        home_ml = MockOddsAPI._prob_to_american(home_win_prob * (1 + vig / 2))
        away_ml = MockOddsAPI._prob_to_american(away_win_prob * (1 + vig / 2))

        # Run line: home -1.5 if heavy fav, else pick randomly
        if home_win_prob > 0.58:
            home_spread, away_spread = -1.5, +1.5
        elif home_win_prob < 0.42:
            home_spread, away_spread = +1.5, -1.5
        else:
            home_spread = rng.choice([-1.5, +1.5])
            away_spread = -home_spread

        # Standard -110 juice on spread with slight variance
        spread_juice = rng.randint(-115, -105)

        # Total: center around 8.5, range 7.5–10.5
        total_line = round(rng.uniform(7.5, 10.5) * 2) / 2  # nearest 0.5
        total_juice = rng.randint(-115, -105)

        result: dict[str, Any] = {
            "game_pk": game_pk,
            "source": "mock",
            "is_mock": True,
            # The closing-line-value columns
            "book": book,
            "line_type": line_type,
            "market_type": market_type,
            "is_sharp_book": is_sharp_book,
        }
        for field in GAME_ODDS_FIELDS:
            result[field] = None
        if canonical in LEGACY_GAME_MARKET_TYPES:
            result.update(
                {
                    "home_ml": home_ml,
                    "away_ml": away_ml,
                    "home_spread": home_spread,
                    "home_spread_ml": spread_juice,
                    "away_spread": away_spread,
                    "away_spread_ml": spread_juice,
                    "total_line": total_line,
                    "over_ml": total_juice,
                    "under_ml": total_juice,
                }
            )
            return result

        # A segment / team market: its own deterministic draw, seeded on the
        # game AND the market so two markets of one game never share numbers.
        mrng = random.Random(game_pk * 1_000 + (GAME_MARKET_TYPES.index(canonical) + 1))
        kind = GAME_MARKET_KIND[canonical]
        if kind == "three_way":
            # A first-inning / first-five moneyline: the tie is a real outcome,
            # so the three prices carry the vig between them.
            tie_prob = mrng.uniform(0.18, 0.30)
            side_prob = (1.0 - tie_prob) / 2.0
            edge = mrng.uniform(-0.06, 0.06)
            result["home_ml"] = MockOddsAPI._prob_to_american((side_prob + edge) * (1 + vig / 3))
            result["away_ml"] = MockOddsAPI._prob_to_american((side_prob - edge) * (1 + vig / 3))
            result["draw_ml"] = MockOddsAPI._prob_to_american(tie_prob * (1 + vig / 3))
        elif kind == "moneyline":
            # First team to score: near a coin flip with the book's margin.
            p = mrng.uniform(0.44, 0.56)
            result["home_ml"] = MockOddsAPI._prob_to_american(p * (1 + vig / 2))
            result["away_ml"] = MockOddsAPI._prob_to_american((1.0 - p) * (1 + vig / 2))
        elif kind == "runline":
            spread = mrng.choice([-0.5, 0.5])
            result["home_spread"], result["away_spread"] = spread, -spread
            # Every mock price is a valid American price. A raw
            # integer draw put about half of them inside (-100, 100) and
            # some at 0, which the de-vig refuses for both sides.
            p_home = mrng.uniform(0.40, 0.60)
            result["home_spread_ml"] = MockOddsAPI._prob_to_american(p_home * (1 + vig / 2))
            result["away_spread_ml"] = MockOddsAPI._prob_to_american((1.0 - p_home) * (1 + vig / 2))
        elif kind == "total":
            # The line scales with the slice of the game and the side.
            if canonical.startswith("f1_"):
                line = mrng.choice([0.5, 1.5])
            elif canonical.startswith("f5_team"):
                line = mrng.choice([1.5, 2.0, 2.5])
            elif canonical.startswith("f5_"):
                line = mrng.choice([4.0, 4.5, 5.0])
            else:  # a full-game team total
                line = mrng.choice([3.5, 4.0, 4.5, 5.0])
            result["total_line"] = line
            p_over = mrng.uniform(0.40, 0.60)
            result["over_ml"] = MockOddsAPI._prob_to_american(p_over * (1 + vig / 2))
            result["under_ml"] = MockOddsAPI._prob_to_american((1.0 - p_over) * (1 + vig / 2))
        elif kind == "yes_no":
            result["total_line"] = 0.5
            p_yes = mrng.uniform(0.40, 0.60)
            result["over_ml"] = MockOddsAPI._prob_to_american(p_yes * (1 + vig / 2))
            result["under_ml"] = MockOddsAPI._prob_to_american((1.0 - p_yes) * (1 + vig / 2))
        return result

    @staticmethod
    def get_prop_odds(
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
        book: str = "consensus",
        is_sharp_book: bool = False,
    ) -> dict[str, Any]:
        """
        Returns a deterministic mock prop line for a player.

        The RNG is seeded on (game_pk, player_id, prop_stat) so the same
        combination always returns the same line — frontend and tests never
        see flicker.

        Parameters
        ----------
        game_pk       : MLB game identifier
        player_id     : MLB player identifier
        prop_stat     : one of the 15 markets in PROP_STATS (pipeline/odds_provider.py):
                        the pitcher markets 'strikeouts' | 'earned_runs' | 'walks'
                        | 'outs_recorded' | 'hits_allowed' and the batter markets
                        'hits' | 'home_runs' | 'total_bases' | 'rbis' | 'singles'
                        | 'doubles' | 'triples' | 'runs' | 'stolen_bases'
                        | 'hits_runs_rbis'
        line_type     : 'opening' | 'current' | 'closing' | 'bet_placement'
        book          : book identifier (default 'consensus')
        is_sharp_book : True for sharp reference books (Pinnacle, Circa)

        Returns
        -------
        dict with all columns required by a raw.prop_odds INSERT:
          game_pk, player_id, prop_stat, line,
          over_ml, under_ml, book, line_type, is_sharp_book,
          source, is_mock

        Raises
        ------
        ValueError
            If prop_stat is not in the 15 known markets.  Prevents silent
            schema violations before the DB CHECK constraint catches them.
        """
        if prop_stat not in MockOddsAPI._PROP_CONFIG:
            known = ", ".join(sorted(MockOddsAPI._PROP_CONFIG))
            raise ValueError(f"Unknown prop_stat '{prop_stat}'. Known values: {known}")

        center, half_spread, over_vig_range, under_vig_range = MockOddsAPI._PROP_CONFIG[prop_stat]

        # Deterministic RNG: same game + player + prop always → same line
        rng = random.Random(game_pk * 1_000_000 + player_id + hash(prop_stat))

        # Line: add jitter to center then snap to nearest 0.5
        raw_line = center + rng.uniform(-half_spread, half_spread)
        line = round(raw_line * 2) / 2  # snap to nearest 0.5

        # Vig: independent random draws within Betting-Analyst-approved ranges
        over_ml = rng.randint(*over_vig_range)
        under_ml = rng.randint(*under_vig_range)

        return {
            "game_pk": game_pk,
            "player_id": player_id,
            "prop_stat": prop_stat,
            "line": line,
            "over_ml": over_ml,
            "under_ml": under_ml,
            "book": book,
            "line_type": line_type,
            "is_sharp_book": is_sharp_book,
            "source": "mock",
            "is_mock": True,
        }

    def get_odds_by_book(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
    ) -> list[dict[str, Any]]:
        """The by-book protocol method. The mock has one book: its one row.

        The row keeps the mock's ``consensus`` label and, for the three
        full-game markets, its all-three-markets shape. The method reads
        ``self.get_odds``, so a subclass that overrides the one-row method (a
        test fake that raises on one market) answers here too. Call it on an
        instance, as the pipeline does.
        """
        return [self.get_odds(game_pk, line_type=line_type, market_type=market_type)]

    def get_prop_odds_by_book(
        self,
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
    ) -> list[dict[str, Any]]:
        """The by-book protocol method. The mock has one book: its one row.

        Reads ``self.get_prop_odds``, as :meth:`get_odds_by_book` reads
        ``self.get_odds``.
        """
        return [self.get_prop_odds(game_pk, player_id, prop_stat, line_type=line_type)]
