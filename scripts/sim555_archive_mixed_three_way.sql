-- SIM-555 (2026-09-29): move the stored three-way moneyline rows the load guard now refuses
-- into raw.game_odds_archive, in one transaction. The rules (pipeline/odds_row_guard.py):
--   three_way_two_way_team_prices  the two team prices alone add to 1.00 or more
--                                  (THREE_WAY_TEAM_SUM_MAX): a tie-refunded two-way pair
--                                  listed beside a separate tie price;
--   three_way_sum_above_max        the three prices add above 1.18 (THREE_WAY_SUM_MAX).
-- The rows were written before the rules landed (2023, 2024 - almost all the vendor's blend -
-- 2025 and 2026). The archive keeps them; its readers take only 'consensus' rows, so these
-- stay out of every read. Run from the repo root:
--   docker compose exec -T db psql -U baseball_user -d baseball_sim -v ON_ERROR_STOP=1 < scripts/sim555_archive_mixed_three_way.sql

BEGIN;

CREATE TEMP TABLE sim555_mixed_three_way ON COMMIT DROP AS
SELECT o.id
FROM raw.game_odds o
CROSS JOIN LATERAL (
  SELECT CASE WHEN o.home_ml < 0 THEN -o.home_ml::float8 / (-o.home_ml + 100)
              ELSE 100.0 / (o.home_ml + 100) END AS p_home,
         CASE WHEN o.away_ml < 0 THEN -o.away_ml::float8 / (-o.away_ml + 100)
              ELSE 100.0 / (o.away_ml + 100) END AS p_away,
         CASE WHEN o.draw_ml < 0 THEN -o.draw_ml::float8 / (-o.draw_ml + 100)
              ELSE 100.0 / (o.draw_ml + 100) END AS p_draw
) p
WHERE o.book LIKE 'bp:%'
  AND o.market_type IN ('f1_moneyline', 'f5_moneyline')
  AND o.home_ml IS NOT NULL AND o.away_ml IS NOT NULL AND o.draw_ml IS NOT NULL
  AND (p.p_home + p.p_away >= 1.0 OR p.p_home + p.p_away + p.p_draw > 1.18);

SELECT count(*) AS rows_to_move FROM sim555_mixed_three_way;

INSERT INTO raw.game_odds_archive
SELECT o.* FROM raw.game_odds o JOIN sim555_mixed_three_way t USING (id);

DELETE FROM raw.game_odds o USING sim555_mixed_three_way t WHERE o.id = t.id;

SELECT count(*) AS archive_rows FROM raw.game_odds_archive;

COMMIT;
