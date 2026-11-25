"""The per-play fielder metrics: outfield catch probability, infield outs
above average, double plays, bunt defense, first-base scooping and errors.

Outs above average (OAA) are the outs a fielder makes beyond what an average
fielder makes on the same chances. Each step reads ``raw.pitches``, scores
each play and writes one temp table: ``_tmp_of_plays``, ``_tmp_if_plays``,
``_tmp_dp_plays``, ``_tmp_bunt_defense``, ``_tmp_1b_scoop`` or ``_tmp_errors``.
``_aggregate_fielder_season_metrics`` (``profiles/fielder.py``) reads them. A
step writes no table when its sample is too small.
"""

from __future__ import annotations

import logging
import math
from datetime import date

import numpy as np
import pandas as pd

from ingest.statcast_events import (
    batter_retired,
    play_outs,
)
from profiles.base import ComputorBase
from profiles.defense import (
    AVG_FIELDER_POS,
    AVG_THROW_VELO,
    DEFAULT_SPRINT_SPEED_FPS,
    FIRST_BASE,
    IF_AWAY_FROM_BAG_PENALTY_S,
    INFIELD_EXCHANGE_TIME,
    OF_GOING_BACK_PENALTY_FPS,
    SECOND_BASE,
    _classify_direction_of,
    _estimate_gb_travel_time,
    _estimate_hang_time,
    _euclidean_hc,
    _fit_logistic_model,
    _hc_to_feet,
    _predict_proba,
    _quick_auc,
    _throw_time,
)
from profiles.leverage import RE24_APPROX
from profiles.sql import (
    SQL_BATTER_RETIRED,
    SQL_FIELDING_OUT,
    SQL_IN_PLAY,
    sql_seasons,
)

log = logging.getLogger("player_profile_computor")


def _nearest_position(
    hc_x: float, hc_y: float, stand: str, positions: tuple[str, ...]
) -> tuple[str | None, float]:
    """The position whose average start is nearest the ball, and that distance in hc units."""
    best_pos = None
    best_dist = float("inf")
    for pos in positions:
        fx, fy = AVG_FIELDER_POS[pos].get(stand, AVG_FIELDER_POS[pos]["R"])
        d = _euclidean_hc(hc_x, hc_y, fx, fy)
        if d < best_dist:
            best_dist = d
            best_pos = pos
    return best_pos, best_dist


def _sprint_speed_map(speeds: pd.DataFrame) -> dict[tuple[int, int], float]:
    """Each (player, season)'s sprint speed; a NULL speed reads the default."""
    speed_map = {}
    for _, r in speeds.iterrows():
        speed_map[(int(r["player_id"]), int(r["season"]))] = (
            r["sprint_speed"] if pd.notna(r["sprint_speed"]) else DEFAULT_SPRINT_SPEED_FPS
        )
    return speed_map


class FielderPlayMetricsMixin(ComputorBase):
    """Builds the per-play fielder temp tables the season aggregate reads."""

    # ------------------------------------------------------------------
    # The outfield catch probability and OAA
    # ------------------------------------------------------------------

    def _compute_outfield_catch_probability(
        self, seasons: list[int], asof: date | None = None
    ) -> None:
        """Fit the outfield catch-probability model and score each play's OAA.

        The steps follow Statcast's method:
          1. Estimate the hang time from the launch speed and angle.
          2. Measure the distance from the fielder's average start to the ball.
          3. Divide the distance by the opportunity time (the hang time + 0.4 s):
             the speed needed.
          4. Add 1 ft/s to the speed needed when the fielder goes back (Tango).
          5. Fit a logistic curve: P(catch) from the speed needed.
          6. Credit 1 - P for a catch and -P for a miss.

        Writes ``_tmp_of_plays``. ``asof`` keeps ``raw.pitches`` to
        ``game_date <= asof``.
        """
        log.info("Computing outfield catch probability …")
        season_list = sql_seasons(seasons)
        date_cutoff = f"AND game_date <= DATE '{asof.isoformat()}'" if asof is not None else ""

        df = self._conn.execute(f"""
            SELECT
                game_pk || '-' || at_bat_number || '-' || pitch_number AS pitch_id_str,
                game_pk,
                season,
                stand,
                fielder_7, fielder_8, fielder_9,
                fielded_by,
                hc_x, hc_y,
                launch_speed, launch_angle, spray_angle,
                hit_distance_sc,
                bb_type,
                events
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              {date_cutoff}
              -- SIM-457: every ball in play is a catch OPPORTUNITY. The old
              -- `type='X'` kept only the plays that became outs, so 44% of
              -- chances — the ones that fell for hits — were invisible and
              -- catch probability read near 100%.
              AND {SQL_IN_PLAY}
              AND events IS NOT NULL
              -- A home run leaves the field: no outfielder can catch it, so
              -- it is not a catch opportunity. `type='X'` used to exclude
              -- home runs implicitly (X = out, no run); the widened filter
              -- must exclude them explicitly or ~5.5k/season enter as
              -- "missed catches" charged to whoever plays nearest the wall.
              AND events <> 'home_run'
              AND hc_x IS NOT NULL AND hc_y IS NOT NULL
              AND launch_speed IS NOT NULL AND launch_angle IS NOT NULL
              -- Outfield plays: fly balls, line drives, and popups
              -- Exclude ground balls (handled by infield model)
              AND bb_type IN ('fly_ball', 'line_drive', 'popup')
              AND launch_angle > 10
              -- Ball reached outfield (hc_y < ~150 in hc coordinates)
              AND hc_y < 160
        """).fetchdf()

        if len(df) < 500:
            log.warning("  Too few outfield plays (%d) — skipping", len(df))
            return

        log.info("  Loaded %d outfield plays", len(df))

        rows = []
        for _, r in df.iterrows():
            # The play belongs to the outfielder whose average start is nearest the ball.
            stand = r["stand"] if r["stand"] in ("L", "R") else "R"
            best_pos, distance_hc = _nearest_position(
                r["hc_x"], r["hc_y"], stand, ("LF", "CF", "RF")
            )

            pos_to_col = {"LF": "fielder_7", "CF": "fielder_8", "RF": "fielder_9"}
            fielder_id = r[pos_to_col[best_pos]]
            if pd.isna(fielder_id):
                continue

            fx, fy = AVG_FIELDER_POS[best_pos].get(stand, AVG_FIELDER_POS[best_pos]["R"])
            distance_ft = _hc_to_feet(distance_hc)

            hang_time = _estimate_hang_time(r["launch_speed"], r["launch_angle"])
            if hang_time is None or hang_time < 1.0:
                continue

            # Opportunity time ≈ hang time + 0.4 s (pitch release to contact).
            opportunity_time = hang_time + 0.4

            if opportunity_time <= 0.5:
                continue
            speed_needed = distance_ft / opportunity_time

            direction_cat, going_back = _classify_direction_of(
                fx, fy, r["hc_x"], r["hc_y"], best_pos
            )
            if going_back:
                speed_needed += OF_GOING_BACK_PENALTY_FPS

            # The label: did the outfielder catch the ball? A catch retires the
            # batter. A force_out or fielders_choice_out is an out, not a catch:
            # the ball hit the ground. A field_error dropped; a hit fell in.
            caught = batter_retired(r["events"])

            rows.append(
                {
                    "pitch_id_str": r["pitch_id_str"],
                    "game_pk": r["game_pk"],
                    "season": r["season"],
                    "fielder_id": int(fielder_id),
                    "position": best_pos,
                    "hang_time": hang_time,
                    "distance_needed": distance_ft,
                    "speed_needed": speed_needed,
                    "direction_back": going_back,
                    "direction_cat": direction_cat,
                    "caught": caught,
                }
            )

        plays = pd.DataFrame(rows)
        if len(plays) < 200:
            log.warning("  Too few valid OF plays (%d) after processing", len(plays))
            return

        X = plays[["speed_needed"]].values
        y = plays["caught"].astype(int).values

        model = _fit_logistic_model(X, y)
        if model is None:
            log.error("  OF catch probability model fit failed")
            return

        plays["catch_probability"] = _predict_proba(model, X)

        # The clip keeps one play from carrying an extreme credit.
        plays["catch_probability"] = plays["catch_probability"].clip(0.01, 0.99)

        # Statcast's star ratings: 5 stars is the hardest catch.
        plays["star_rating"] = pd.cut(
            plays["catch_probability"],
            bins=[0, 0.25, 0.50, 0.75, 0.90, 1.01],
            labels=[5, 4, 3, 2, 1],
        ).astype(int)

        plays["oaa_credit"] = np.where(
            plays["caught"],
            1.0 - plays["catch_probability"],
            -plays["catch_probability"],
        )

        self._conn.register("of_plays", plays)
        self._conn.execute("""
            DROP TABLE IF EXISTS _tmp_of_plays;
            CREATE TABLE _tmp_of_plays AS SELECT * FROM of_plays;
        """)
        self._conn.unregister("of_plays")

        log.info(
            "  OF catch probability computed: %d plays, model AUC ~%.3f",
            len(plays),
            _quick_auc(y, plays["catch_probability"].values),
        )

    # ------------------------------------------------------------------
    # The infield OAA
    # ------------------------------------------------------------------

    def _compute_infield_oaa(self, seasons: list[int], asof: date | None = None) -> None:
        """Fit the infield out-probability model and score each play's OAA.

        The model reads one number per play, the time margin: the
        batter-runner's time to first base minus the defense's time. The
        defense's time is the ball's travel to the fielder, the exchange and
        the throw to first base. A fielder who moves away from first base
        loses time. The inputs follow Statcast's infield model: the fielder's
        distance to the ball, the ball's speed, the throw distance and the
        batter-runner's speed.

        Writes ``_tmp_if_plays``. ``asof`` keeps ``raw.pitches`` to
        ``game_date <= asof``. The sprint speed board carries no date, so the
        per-row lookup takes the Savant season shift in Python: the season
        that contains the cutoff reads the prior season's speed.
        """
        log.info("Computing infield OAA …")
        season_list = sql_seasons(seasons)
        asof_date = asof or date.today()
        date_cutoff = f"AND game_date <= DATE '{asof_date.isoformat()}'" if asof is not None else ""

        df = self._conn.execute(f"""
            SELECT
                game_pk || '-' || at_bat_number || '-' || pitch_number AS pitch_id_str,
                game_pk,
                season,
                stand,
                batter,
                fielder_3, fielder_4, fielder_5, fielder_6,
                fielded_by,
                hc_x, hc_y,
                launch_speed, launch_angle, spray_angle,
                bb_type,
                events,
                type
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              {date_cutoff}
              -- SIM-457: every ground ball is a conversion OPPORTUNITY. The
              -- old `type='X'` kept only the plays that became outs, so the
              -- model trained on a set with almost no failures.
              AND {SQL_IN_PLAY}
              AND events IS NOT NULL
              AND hc_x IS NOT NULL AND hc_y IS NOT NULL
              AND launch_speed IS NOT NULL
              -- Infield plays: ground balls and some line drives
              AND (bb_type = 'ground_ball' OR (bb_type = 'line_drive' AND hc_y > 150))
        """).fetchdf()

        if len(df) < 500:
            log.warning("  Too few infield plays (%d)", len(df))
            return

        log.info("  Loaded %d infield plays", len(df))

        # A cutoff build also loads the prior season, for the season shift below.
        lookup_seasons = set(seasons) | ({asof_date.year - 1} if asof is not None else set())
        batter_speeds = self._conn.execute(f"""
            SELECT player_id, season, sprint_speed
            FROM pg.raw.sprint_speed
            WHERE season IN ({sql_seasons(sorted(lookup_seasons))})
        """).fetchdf()
        speed_map = _sprint_speed_map(batter_speeds)

        def _speed_lookup_season(row_season: int) -> int:
            """The season whose sprint speed a row reads: the prior season for
            the season that contains the cutoff, whose own row is still growing."""
            if asof is not None and row_season == asof_date.year:
                return asof_date.year - 1
            return row_season

        rows = []
        for _, r in df.iterrows():
            stand = r["stand"] if r["stand"] in ("L", "R") else "R"

            # The play belongs to the infielder whose average start is nearest the ball.
            best_pos, fielder_dist_hc = _nearest_position(
                r["hc_x"], r["hc_y"], stand, ("1B", "2B", "3B", "SS")
            )

            pos_to_col = {
                "1B": "fielder_3",
                "2B": "fielder_4",
                "3B": "fielder_5",
                "SS": "fielder_6",
            }
            fielder_id = r[pos_to_col[best_pos]]
            if pd.isna(fielder_id):
                continue

            fx, fy = AVG_FIELDER_POS[best_pos].get(stand, AVG_FIELDER_POS[best_pos]["R"])
            fielder_dist_ft = _hc_to_feet(fielder_dist_hc)

            throw_dist_hc = _euclidean_hc(r["hc_x"], r["hc_y"], FIRST_BASE[0], FIRST_BASE[1])
            throw_dist_ft = _hc_to_feet(throw_dist_hc)

            ball_travel_time = _estimate_gb_travel_time(
                r["launch_speed"], r["launch_angle"], fielder_dist_ft
            )
            if ball_travel_time is None:
                continue

            throw_velo = AVG_THROW_VELO.get(best_pos, 80.0)
            throw_time = _throw_time(throw_dist_ft, throw_velo)
            defense_time = ball_travel_time + INFIELD_EXCHANGE_TIME + throw_time

            batter_speed = speed_map.get(
                (int(r["batter"]), _speed_lookup_season(int(r["season"]))),
                DEFAULT_SPRINT_SPEED_FPS,
            )
            runner_time = 90.0 / batter_speed  # seconds from home to first

            # A positive margin means the defense has time to spare.
            time_margin = runner_time - defense_time

            direction_cat, going_back = _classify_direction_of(
                fx, fy, r["hc_x"], r["hc_y"], best_pos
            )
            if going_back:
                time_margin -= IF_AWAY_FROM_BAG_PENALTY_S

            # A move away from the throw's target also costs time.
            fx_to_ball = (r["hc_x"] - fx, r["hc_y"] - fy)
            fx_to_base = (FIRST_BASE[0] - fx, FIRST_BASE[1] - fy)
            dot = fx_to_ball[0] * fx_to_base[0] + fx_to_ball[1] * fx_to_base[1]
            mag1 = math.sqrt(fx_to_ball[0] ** 2 + fx_to_ball[1] ** 2)
            mag2 = math.sqrt(fx_to_base[0] ** 2 + fx_to_base[1] ** 2)
            if mag1 > 0 and mag2 > 0:
                cos_angle = dot / (mag1 * mag2)
                if cos_angle < -0.3:  # moving substantially away from 1B
                    time_margin -= 0.10

            # The label: did the defense record at least one out? A force_out
            # or fielders_choice_out counts, though the batter reached. A clean
            # hit, an error or an all-safe fielders_choice counts zero.
            # play_outs also counts a runner thrown out on a hit typed X, as
            # the double-play model and the pools do.
            out_recorded = play_outs(r["events"], r["type"]) > 0

            rows.append(
                {
                    "pitch_id_str": r["pitch_id_str"],
                    "game_pk": r["game_pk"],
                    "season": r["season"],
                    "fielder_id": int(fielder_id),
                    "position": best_pos,
                    "fielder_distance": fielder_dist_ft,
                    "throw_distance": throw_dist_ft,
                    "time_margin": time_margin,
                    "runner_speed": batter_speed,
                    "direction_category": direction_cat,
                    "out_recorded": out_recorded,
                }
            )

        plays = pd.DataFrame(rows)
        if len(plays) < 200:
            log.warning("  Too few valid infield plays (%d) after processing", len(plays))
            return

        X = plays[["time_margin"]].values
        y = plays["out_recorded"].astype(int).values

        model = _fit_logistic_model(X, y)
        if model is None:
            log.error("  Infield OAA model fit failed")
            return

        plays["out_probability"] = _predict_proba(model, X)
        plays["out_probability"] = plays["out_probability"].clip(0.01, 0.99)

        plays["oaa_credit"] = np.where(
            plays["out_recorded"],
            1.0 - plays["out_probability"],
            -plays["out_probability"],
        )

        self._conn.register("if_plays", plays)
        self._conn.execute("""
            DROP TABLE IF EXISTS _tmp_if_plays;
            CREATE TABLE _tmp_if_plays AS SELECT * FROM if_plays;
        """)
        self._conn.unregister("if_plays")

        log.info(
            "  Infield OAA computed: %d plays, model AUC ~%.3f",
            len(plays),
            _quick_auc(y, plays["out_probability"].values),
        )

    # ------------------------------------------------------------------
    # The double plays
    # ------------------------------------------------------------------

    def _compute_dp_metrics(self, seasons: list[int], asof: date | None = None) -> None:
        """Fit the double-play model and score each chance (Tango's method).

        A chance is a ground ball with a runner on first only and fewer than
        two outs. The model reads the time margin: each runner's time to his
        base minus the defense's time to that base, the tighter of the two.
        The fielder who fields the ball (the initiator) and the fielder who
        turns the relay (the pivot) each get credit. ``dp_run_value`` is the
        drop in run expectancy over the play.

        Writes ``_tmp_dp_plays``. ``asof`` keeps ``raw.pitches`` to
        ``game_date <= asof``. The sprint speed join takes the Savant season
        shift. The run values read ``self._re_matrix``, which ``run()`` builds
        at the same cutoff before this step.
        """
        log.info("Computing double play metrics …")
        season_list = sql_seasons(seasons)
        asof_date = asof or date.today()
        date_cutoff = f"AND game_date <= DATE '{asof_date.isoformat()}'" if asof is not None else ""

        df = self._conn.execute(f"""
            SELECT
                game_pk || '-' || at_bat_number || '-' || pitch_number AS pitch_id_str,
                game_pk,
                at_bat_number,
                season,
                stand,
                batter,
                on_1b,
                outs,
                fielder_3, fielder_4, fielder_5, fielder_6,
                fielded_by,
                hc_x, hc_y,
                launch_speed, launch_angle,
                bb_type,
                events,
                type,
                field_assist_1, field_assist_2,
                field_putout_1, field_putout_2,
                home_score, away_score, inning, inning_topbot,
                -- runners state for RE24
                (CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END)
                + (CASE WHEN on_2b IS NOT NULL THEN 2 ELSE 0 END)
                + (CASE WHEN on_3b IS NOT NULL THEN 4 ELSE 0 END) AS runners_state
            FROM pg.raw.pitches
            WHERE season IN ({season_list})
              {date_cutoff}
              AND on_1b IS NOT NULL
              AND on_2b IS NULL        -- runner on 1B only (Tango scoping)
              AND on_3b IS NULL
              AND outs < 2
              AND bb_type = 'ground_ball'
              AND events IS NOT NULL
              AND hc_x IS NOT NULL AND hc_y IS NOT NULL
              AND launch_speed IS NOT NULL
        """).fetchdf()

        if len(df) < 200:
            log.warning("  Too few DP opportunities (%d)", len(df))
            return

        log.info("  Loaded %d DP opportunities", len(df))

        # The speeds of the players in derived.baserunner_season_metrics. The
        # SQL join takes the season shift, so the per-row lookup needs none. A
        # player with no row there reads the default speed.
        sprint_speed_season = self._savant_season_sql("bsm.season", asof)
        batter_speeds = self._conn.execute(f"""
            SELECT bsm.player_id, bsm.season,
                   COALESCE(ss.sprint_speed, {DEFAULT_SPRINT_SPEED_FPS}) AS sprint_speed
            FROM derived.baserunner_season_metrics bsm
            LEFT JOIN pg.raw.sprint_speed ss
                ON bsm.player_id = ss.player_id
                AND ss.season = {sprint_speed_season}
            WHERE bsm.season IN ({season_list})
        """).fetchdf()
        speed_map = _sprint_speed_map(batter_speeds)

        rows = []
        for _, r in df.iterrows():
            stand = r["stand"] if r["stand"] in ("L", "R") else "R"

            # The initiator is the fielder who fielded the ball.
            fielded_by = r["fielded_by"]
            if pd.isna(fielded_by):
                continue

            pos_to_col = {
                "1B": "fielder_3",
                "2B": "fielder_4",
                "3B": "fielder_5",
                "SS": "fielder_6",
            }
            initiator_pos = None
            for pos, col in pos_to_col.items():
                if r[col] == fielded_by:
                    initiator_pos = pos
                    break
            if initiator_pos is None:
                continue

            fx, fy = AVG_FIELDER_POS.get(initiator_pos, {}).get(stand, (125, 155))
            fielder_dist_ft = _hc_to_feet(_euclidean_hc(r["hc_x"], r["hc_y"], fx, fy))

            dist_to_2b_ft = _hc_to_feet(
                _euclidean_hc(r["hc_x"], r["hc_y"], SECOND_BASE[0], SECOND_BASE[1])
            )

            dist_2b_to_1b_ft = _hc_to_feet(
                _euclidean_hc(SECOND_BASE[0], SECOND_BASE[1], FIRST_BASE[0], FIRST_BASE[1])
            )

            ball_time = _estimate_gb_travel_time(
                r["launch_speed"], r["launch_angle"], fielder_dist_ft
            )
            if ball_time is None:
                continue

            # The defense's time: the field, the throw to second, the pivot's
            # exchange and the throw to first.
            throw_velo_to_2b = AVG_THROW_VELO.get(initiator_pos, 80.0)
            time_to_2b = (
                ball_time + INFIELD_EXCHANGE_TIME + _throw_time(dist_to_2b_ft, throw_velo_to_2b)
            )

            pivot_exchange = 0.60  # seconds: the pivot's exchange is quicker than the initiator's
            throw_to_1b_time = _throw_time(dist_2b_to_1b_ft, 78.0)  # the pivot's throw, ~78 mph
            total_defense_time = time_to_2b + pivot_exchange + throw_to_1b_time

            runner_1b_id = int(r["on_1b"]) if pd.notna(r["on_1b"]) else 0
            runner_speed = speed_map.get((runner_1b_id, int(r["season"])), DEFAULT_SPRINT_SPEED_FPS)
            runner_time_to_2b = 90.0 / runner_speed

            batter_speed = speed_map.get(
                (int(r["batter"]), int(r["season"])), DEFAULT_SPRINT_SPEED_FPS
            )
            batter_time_to_1b = 90.0 / batter_speed

            # A double play beats the runner at second AND the batter at first,
            # so the tighter margin counts.
            time_margin_2b = runner_time_to_2b - time_to_2b
            time_margin_1b = batter_time_to_1b - total_defense_time
            time_margin = min(time_margin_2b, time_margin_1b)

            # A corner infielder who moves away from second base loses time.
            toward_2b = r["hc_y"] < fy  # ball is closer to 2B than fielder start
            if not toward_2b and initiator_pos in ("3B", "1B"):
                time_margin -= 0.15

            # The label: did the defense turn two?
            dp_turned = r["events"] in ("grounded_into_double_play", "double_play")

            # The outs on the play derive from `events` (ingest/statcast_events.py).
            outs_recorded = play_outs(r["events"], r["type"])

            # The pivot is the second assist, when he is one of the four infielders.
            pivot_id = None
            pivot_pos = None
            if pd.notna(r.get("field_assist_2")):
                for pos, col in pos_to_col.items():
                    if r[col] == r["field_assist_2"]:
                        pivot_id = int(r["field_assist_2"])
                        pivot_pos = pos
                        break

            # The run value: the run expectancy before the play minus after it.
            pre_outs = int(r["outs"])
            pre_runners = int(r["runners_state"])
            re_start = self._re_matrix.get(
                (pre_outs, pre_runners), RE24_APPROX.get((pre_outs, pre_runners), 0.5)
            )

            # The state after the play derives from `events`. Every chance has a
            # runner on first only and fewer than two outs, so:
            #   3+ total outs: the inning is over; the run expectancy is 0.
            #   2 outs on the play (a ground-ball, lineout or bunt double
            #     play): both men are out, and the bases are empty.
            #   force_out / fielders_choice_out: the runner is out at second
            #     and the batter reaches; first base is occupied.
            #   batter retired (field_out, sac_bunt): the runner moves up;
            #     second base is occupied.
            #   a runner out on a reach event (a single or an error typed X,
            #     outs_recorded = 1): the runner is out advancing and the
            #     batter is aboard; first base is occupied.
            #   nobody out (a hit, an error, an all-safe fielders' choice):
            #     both men are aboard. A deep single can put the runner on
            #     third; the model reads first and second as the approximation.
            post_outs = pre_outs + outs_recorded
            if post_outs >= 3:
                post_runners = 0
                re_end = 0.0
            elif outs_recorded >= 2:
                post_runners = 0
                re_end = self._re_matrix.get((min(post_outs, 2), post_runners), 0.10)
            else:
                if r["events"] in ("force_out", "fielders_choice_out"):
                    post_runners = 0b001
                elif batter_retired(r["events"]):
                    post_runners = 0b010
                elif outs_recorded == 1:
                    post_runners = 0b001
                else:
                    post_runners = 0b011
                re_end = self._re_matrix.get((min(post_outs, 2), post_runners), 0.30)

            dp_run_value = re_start - re_end  # positive = runs prevented

            rows.append(
                {
                    "pitch_id_str": r["pitch_id_str"],
                    "game_pk": r["game_pk"],
                    "season": r["season"],
                    "initiator_id": int(fielded_by),
                    "initiator_position": initiator_pos,
                    "pivot_id": pivot_id,
                    "pivot_position": pivot_pos,
                    "time_margin": time_margin,
                    "runner_speed": runner_speed,
                    "batter_speed": batter_speed,
                    "fielder_toward_2b": toward_2b,
                    "dp_turned": dp_turned,
                    "outs_recorded": outs_recorded,
                    "re24_start": re_start,
                    "re24_end": re_end,
                    "dp_run_value": dp_run_value,
                }
            )

        dp_plays = pd.DataFrame(rows)
        if len(dp_plays) < 100:
            log.warning("  Too few valid DP plays (%d) after processing", len(dp_plays))
            return

        X = dp_plays[["time_margin"]].values
        y = dp_plays["dp_turned"].astype(int).values

        model = _fit_logistic_model(X, y)
        if model is not None:
            dp_plays["dp_probability"] = _predict_proba(model, X)
            dp_plays["dp_probability"] = dp_plays["dp_probability"].clip(0.01, 0.99)
        else:
            # With no fit, every chance reads the sample's double-play rate.
            league_dp_rate = y.mean()
            dp_plays["dp_probability"] = league_dp_rate

        self._conn.register("dp_plays", dp_plays)
        self._conn.execute("""
            DROP TABLE IF EXISTS _tmp_dp_plays;
            CREATE TABLE _tmp_dp_plays AS SELECT * FROM dp_plays;
        """)
        self._conn.unregister("dp_plays")

        log.info(
            "  DP metrics computed: %d opportunities, DP rate %.1f%%",
            len(dp_plays),
            dp_plays["dp_turned"].mean() * 100,
        )

    # ------------------------------------------------------------------
    # The bunt defense
    # ------------------------------------------------------------------

    def _compute_bunt_defense(self, seasons: list[int], asof: date | None = None) -> None:
        """Build the bunt fielding rates of the first and third basemen into
        ``_tmp_bunt_defense``.

        ``asof`` keeps ``raw.pitches`` to ``game_date <= asof``.
        """
        log.info("Computing bunt defense metrics …")
        season_list = sql_seasons(seasons)
        date_cutoff = f"AND game_date <= DATE '{asof.isoformat()}'" if asof is not None else ""

        self._conn.execute(f"""
            DROP TABLE IF EXISTS _tmp_bunt_defense;
            CREATE TABLE _tmp_bunt_defense AS

            WITH bunt_plays AS (
                SELECT
                    season,
                    fielded_by,
                    fielder_3, fielder_5,
                    events,
                    CASE
                        WHEN fielded_by = fielder_3 THEN '1B'
                        WHEN fielded_by = fielder_5 THEN '3B'
                        ELSE 'other'
                    END AS fielder_position
                FROM pg.raw.pitches
                WHERE season IN ({season_list})
                  {date_cutoff}
                  -- SIM-457: every bunt in play is an opportunity, not only
                  -- the ones that became outs.
                  AND {SQL_IN_PLAY}
                  AND events IS NOT NULL
                  -- Detect bunts: look for 'bunt' in description/events or very low EV + LA
                  AND (
                      events LIKE '%bunt%'
                      OR (launch_speed IS NOT NULL AND launch_speed < 50
                          AND launch_angle IS NOT NULL AND launch_angle BETWEEN -10 AND 30
                          AND bb_type = 'ground_ball'
                          AND hit_distance_sc IS NOT NULL AND hit_distance_sc < 60)
                  )
            )
            SELECT
                fielded_by AS fielder_id,
                fielder_position AS position,
                season,
                COUNT(*) AS bunt_opportunities,
                -- SIM-501a. Question: did the defense record at least one out
                -- on this bunt? A sacrifice counts (the batter is retired); a
                -- bunt single or an error counts zero.
                SUM(CASE WHEN {SQL_FIELDING_OUT} THEN 1 ELSE 0 END) AS bunt_outs_recorded,
                SUM(CASE WHEN {SQL_FIELDING_OUT} THEN 1 ELSE 0 END) * 1.0
                    / NULLIF(COUNT(*), 0) AS bunt_fielding_rate
            FROM bunt_plays
            WHERE fielder_position IN ('1B', '3B')
              AND fielded_by IS NOT NULL
            GROUP BY fielded_by, fielder_position, season
        """)

        log.info("  Bunt defense metrics computed.")

    # ------------------------------------------------------------------
    # The first-base scooping
    # ------------------------------------------------------------------

    def _compute_first_base_scooping(self, seasons: list[int], asof: date | None = None) -> None:
        """Build the first baseman's success rate on long throws into ``_tmp_1b_scoop``.

        A scoop chance is a ground ball fielded by the third baseman or the
        shortstop. ``asof`` keeps ``raw.pitches`` to ``game_date <= asof``.
        """
        log.info("Computing 1B scooping metrics …")
        season_list = sql_seasons(seasons)
        date_cutoff = f"AND game_date <= DATE '{asof.isoformat()}'" if asof is not None else ""

        self._conn.execute(f"""
            DROP TABLE IF EXISTS _tmp_1b_scoop;
            CREATE TABLE _tmp_1b_scoop AS

            WITH scoop_plays AS (
                SELECT
                    season,
                    fielder_3 AS first_baseman_id,
                    field_putout_1,
                    field_putout_2,
                    field_assist_1,
                    fielded_by,
                    events,
                    throwing_error_1,
                    -- A scoop opportunity: throw from deep infield to 1B
                    -- Identified by: putout at 1B on a throw from SS/3B (longer throws)
                    CASE WHEN (fielded_by = fielder_5 OR fielded_by = fielder_6)
                              AND hc_x IS NOT NULL
                              AND bb_type = 'ground_ball'
                         THEN TRUE ELSE FALSE END AS is_scoop_candidate
                FROM pg.raw.pitches
                WHERE season IN ({season_list})
                  {date_cutoff}
                  -- SIM-457: a throw the first baseman could not handle means
                  -- the batter REACHED, so the row is typed D or E — the old
                  -- `type='X'` filtered out the failures being measured.
                  AND {SQL_IN_PLAY}
                  AND events IS NOT NULL
                  AND fielder_3 IS NOT NULL
            )
            SELECT
                first_baseman_id AS fielder_id,
                '1B' AS position,
                season,
                SUM(CASE WHEN is_scoop_candidate THEN 1 ELSE 0 END) AS scoop_opportunities,
                -- SIM-501a. Question: was the BATTER retired at first base?
                -- force_out / fielders_choice_out mean the throw went to
                -- another bag, so they are not scoop successes.
                SUM(CASE WHEN is_scoop_candidate AND {SQL_BATTER_RETIRED} THEN 1 ELSE 0 END)
                    * 1.0 / NULLIF(SUM(CASE WHEN is_scoop_candidate THEN 1 ELSE 0 END), 0)
                    AS scoop_success_rate
            FROM scoop_plays
            WHERE first_baseman_id IS NOT NULL
            GROUP BY first_baseman_id, season
        """)

        log.info("  1B scooping metrics computed.")

    # ------------------------------------------------------------------
    # The errors
    # ------------------------------------------------------------------

    def _compute_error_decomposition(self, seasons: list[int], asof: date | None = None) -> None:
        """Build each fielder's fielding-error and throwing-error rates into ``_tmp_errors``.

        ``asof`` keeps ``raw.pitches`` to ``game_date <= asof``.
        """
        log.info("Computing error decomposition …")
        season_list = sql_seasons(seasons)
        date_cutoff = f"AND game_date <= DATE '{asof.isoformat()}'" if asof is not None else ""

        self._conn.execute(f"""
            DROP TABLE IF EXISTS _tmp_errors;
            CREATE TABLE _tmp_errors AS

            WITH fielder_errors AS (
                SELECT
                    season,
                    fielded_by AS fielder_id,
                    CASE
                        WHEN fielded_by = fielder_3 THEN '1B'
                        WHEN fielded_by = fielder_4 THEN '2B'
                        WHEN fielded_by = fielder_5 THEN '3B'
                        WHEN fielded_by = fielder_6 THEN 'SS'
                        WHEN fielded_by = fielder_7 THEN 'LF'
                        WHEN fielded_by = fielder_8 THEN 'CF'
                        WHEN fielded_by = fielder_9 THEN 'RF'
                        ELSE 'unknown'
                    END AS position,
                    -- Total plays fielded
                    1 AS play,
                    -- Fielding error: misplay on the ball itself
                    CASE WHEN fielding_error = fielded_by THEN 1 ELSE 0 END AS fielding_err,
                    -- Throwing error: clean field but bad throw
                    CASE WHEN throwing_error_1 = fielded_by
                           OR throwing_error_2 = fielded_by THEN 1 ELSE 0 END AS throwing_err
                FROM pg.raw.pitches
                WHERE season IN ({season_list})
                  {date_cutoff}
                  -- SIM-457: an error means the batter REACHED, so error rows
                  -- are typed D or E and never X — the old filter excluded
                  -- 99.5% of the very events this query counts.
                  AND {SQL_IN_PLAY}
                  AND events IS NOT NULL
                  AND fielded_by IS NOT NULL
            )
            SELECT
                fielder_id,
                position,
                season,
                SUM(play)           AS total_plays_fielded,
                SUM(fielding_err)   AS fielding_error_count,
                SUM(throwing_err)   AS throwing_error_count,
                SUM(fielding_err) * 1.0 / NULLIF(SUM(play), 0) AS fielding_error_rate,
                SUM(throwing_err) * 1.0 / NULLIF(SUM(play), 0) AS throwing_error_rate,
                (SUM(fielding_err) + SUM(throwing_err)) * 1.0 / NULLIF(SUM(play), 0) AS error_rate
            FROM fielder_errors
            WHERE position != 'unknown'
            GROUP BY fielder_id, position, season
        """)

        log.info("  Error decomposition computed.")
