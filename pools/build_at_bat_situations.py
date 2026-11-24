"""Build ``derived.at_bat_situations``, the plate-appearance situations the situation engine reads."""

from __future__ import annotations

import logging

from profiles.base import ComputorBase

log = logging.getLogger("player_profile_computor")


class AtBatSituationsMixin(ComputorBase):
    """Build ``derived.at_bat_situations``."""

    def _build_at_bat_situations(self, seasons: list[int]) -> None:
        """
        Materialize derived.at_bat_situations: one row per plate appearance,
        the game state before it, which the situation engine
        (similarity/engines/situation_similarity.py) indexes.

        One row per (game_pk, at_bat_number), taken at the FIRST pitch of the PA
        so outs / base-state / score reflect the situation the batter came up to.
        ``leverage_index`` replicates :func:`compute_leverage_index` as a SQL
        expression (inning × score-diff × runners × outs factors).  Idempotent
        via INSERT OR REPLACE on the (game_pk, at_bat_number) PK.
        """
        log.info("Building derived.at_bat_situations …")
        season_list = ", ".join(str(s) for s in seasons)

        self._conn.execute(f"""
            INSERT OR REPLACE INTO derived.at_bat_situations (
                play_id, game_pk, season, venue_id, at_bat_number,
                inning, top_or_bottom, outs_when_up,
                on_1b, on_2b, on_3b, home_score, away_score,
                leverage_index, pitcher_pitch_count, batter_pa_count
            )
            WITH clean AS (
                SELECT
                    game_pk, at_bat_number, pitch_number, season, venue_id,
                    inning, inning_topbot, outs,
                    on_1b, on_2b, on_3b,
                    home_score, away_score, bat_score, fld_score,
                    batter,
                    -- pitches thrown by this pitcher in this game up to & incl. this pitch
                    COUNT(*) OVER (
                        PARTITION BY game_pk, pitcher
                        ORDER BY at_bat_number, pitch_number
                        ROWS UNBOUNDED PRECEDING
                    ) AS pitcher_pitches_thru
                FROM pg.raw.pitches
                WHERE data_quality_flag = FALSE
                  AND season IN ({season_list})
            ),
            pa AS (
                SELECT
                    *,
                    -- batter's PA sequence number in the game (ties share a rank,
                    -- so every pitch of the same PA gets the same value)
                    DENSE_RANK() OVER (
                        PARTITION BY game_pk, batter ORDER BY at_bat_number
                    ) AS batter_pa_seq
                FROM clean
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY game_pk, at_bat_number ORDER BY pitch_number
                ) = 1
            )
            SELECT
                CAST(game_pk AS VARCHAR) || '-' || CAST(at_bat_number AS VARCHAR) AS play_id,
                game_pk,
                season,
                venue_id,
                at_bat_number,
                inning,
                CASE WHEN inning_topbot = 'Bot' THEN 1 ELSE 0 END                 AS top_or_bottom,
                outs                                                              AS outs_when_up,
                CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END                     AS on_1b,
                CASE WHEN on_2b IS NOT NULL THEN 1 ELSE 0 END                     AS on_2b,
                CASE WHEN on_3b IS NOT NULL THEN 1 ELSE 0 END                     AS on_3b,
                home_score,
                away_score,
                -- compute_leverage_index(inning, outs, runners_state, bat-fld) in SQL
                (CASE WHEN inning <= 3 THEN 0.6 WHEN inning <= 6 THEN 0.9
                      WHEN inning <= 8 THEN 1.3 ELSE 1.5 END)
                  * GREATEST(0.3, 1.5 - LEAST(ABS(bat_score - fld_score), 5) * 0.24)
                  * (1.0 + ((CASE WHEN on_1b IS NOT NULL THEN 1 ELSE 0 END)
                          + (CASE WHEN on_2b IS NOT NULL THEN 1 ELSE 0 END)
                          + (CASE WHEN on_3b IS NOT NULL THEN 1 ELSE 0 END)) * 0.15)
                  * (CASE WHEN outs < 2 THEN 1.0 ELSE 1.1 END)                    AS leverage_index,
                GREATEST(pitcher_pitches_thru - 1, 0)                             AS pitcher_pitch_count,
                batter_pa_seq                                                     AS batter_pa_count
            FROM pa
        """)
        log.info("  derived.at_bat_situations done.")
