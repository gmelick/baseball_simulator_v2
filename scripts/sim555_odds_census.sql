-- scripts/sim555_odds_census.sql
-- =============================================================================
-- SIM-555 (2026-09-28): the census of the one-row-per-book odds rows of one season.
--
-- WHAT IT CHECKS
--   Every stored odds row since SIM-555 holds ONE book's prices and names the
--   book as 'bp:<id>'. This census reads those rows (book LIKE 'bp:%') of one
--   season and runs the load guard's rules on them (pipeline/odds_row_guard.py),
--   per book and market, plus the scope note's Appendix A, a mixed-row check, the
--   dated first-five exclusion, a duplicate-row check and an opening-book check.
--   The healthy read of every section marked "Healthy: zero rows" is zero rows.
--   The sections marked "Info" are counts to read against the probe's counts
--   (scripts/sim555_book_probe.py) and the graded row's coverage.
--
-- WHAT IT CANNOT CHECK FROM THE STORE
--   * The guard's "over and under at different lines" rule: a stored row keeps
--     one total line, so the store cannot show two.
--   * The guard's closing-stamp rule exactly: the store keeps the game's local
--     date, not its scheduled start. Section 7 uses a loose bound (the stamp
--     more than 36 hours after the start of the game's local date), which only
--     catches a stamp far past any game.
--   * The guard's postponement rule: the store keeps no postponed original start.
--
-- READ-ONLY
--   The whole script runs inside BEGIN READ ONLY ... ROLLBACK. It writes nothing.
--
-- HOW TO RUN (the file is not in the db container, so pipe it in):
--   docker compose exec -T db psql -U baseball_user -d baseball_sim -v season=2024 -f - < scripts/sim555_odds_census.sql
--   Without -v season=..., the script reads 2024.
--
-- Three literals below must match the book vocabulary in pipeline/odds_provider.py:
-- the listed sportsbooks (bettable_labels(): a list of what may be graded, so a
-- book the vocabulary does not list is never graded), the graded-book preference
-- (graded_book_labels()) and the three-way markets (the first-inning and
-- first-five moneylines). tests/unit/test_sim555_scripts.py checks them, the
-- dated first-five exclusion against pipeline/bettingpros_odds_provider.py, and
-- the limits of sections 4b and 5b against pipeline/odds_row_guard.py.
-- =============================================================================

\if :{?season}
\else
\set season 2024
\endif
\pset footer on

BEGIN READ ONLY;

\echo ''
\echo '== SIM-555 odds census, season' :season '=='

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 1. Info: game-odds rows per market, line type and book =='
SELECT o.market_type, o.line_type, o.book,
       count(*) AS n_rows, count(DISTINCT o.game_pk) AS n_games
FROM raw.game_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = :season AND o.book LIKE 'bp:%'
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 2. Healthy: zero rows. Equal non-zero spreads priced like a pair (1.00-1.10; Appendix A, per book) =='
WITH r AS (
  SELECT o.market_type, o.line_type, o.book,
         o.home_spread AS hs, o.away_spread AS asp,
         CASE WHEN o.home_spread_ml < 0 THEN -o.home_spread_ml::float8 / (-o.home_spread_ml + 100)
              ELSE 100.0 / (o.home_spread_ml + 100) END AS ph,
         CASE WHEN o.away_spread_ml < 0 THEN -o.away_spread_ml::float8 / (-o.away_spread_ml + 100)
              ELSE 100.0 / (o.away_spread_ml + 100) END AS pa
  FROM raw.game_odds o
  JOIN raw.games g ON g.game_pk = o.game_pk
  WHERE g.season = :season AND o.book LIKE 'bp:%'
    AND o.market_type LIKE '%runline'
    AND abs(o.home_spread_ml) >= 100 AND abs(o.away_spread_ml) >= 100
)
SELECT market_type, line_type, book, hs, asp, count(*) AS n_rows
FROM r
WHERE hs = asp AND hs <> 0 AND ph + pa BETWEEN 1.00 AND 1.10
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, 2, 3;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 3. Healthy: zero rows. Run lines whose two spreads differ in size =='
SELECT o.market_type, o.line_type, o.book, o.home_spread, o.away_spread, count(*) AS n_rows
FROM raw.game_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = :season AND o.book LIKE 'bp:%'
  AND o.market_type LIKE '%runline'
  AND o.home_spread IS NOT NULL AND o.away_spread IS NOT NULL
  AND abs(o.home_spread) <> abs(o.away_spread)
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, 2, 3;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 4. Healthy: zero rows. First-five "+0.5 / +0.5" rows whose prices add above 1.40 (a first-inning tie) =='
WITH r AS (
  SELECT o.line_type, o.book,
         CASE WHEN o.home_spread_ml < 0 THEN -o.home_spread_ml::float8 / (-o.home_spread_ml + 100)
              ELSE 100.0 / (o.home_spread_ml + 100) END AS ph,
         CASE WHEN o.away_spread_ml < 0 THEN -o.away_spread_ml::float8 / (-o.away_spread_ml + 100)
              ELSE 100.0 / (o.away_spread_ml + 100) END AS pa
  FROM raw.game_odds o
  JOIN raw.games g ON g.game_pk = o.game_pk
  WHERE g.season = :season AND o.book LIKE 'bp:%'
    AND o.market_type = 'f5_runline'
    AND o.home_spread = 0.5 AND o.away_spread = 0.5
    AND abs(o.home_spread_ml) >= 100 AND abs(o.away_spread_ml) >= 100
)
SELECT line_type, book, count(*) AS n_rows, round(avg(ph + pa)::numeric, 3) AS mean_sum
FROM r
WHERE ph + pa > 1.40
GROUP BY 1, 2
ORDER BY 1, 2;

-- -----------------------------------------------------------------------------
-- The guard's f5_total_first_inning_line rule (F5_TOTAL_LINE_MAX = 1.5). The
-- first-five TEAM totals are left out: one team's first-five total sits low.
\echo ''
\echo '== 4b. Healthy: zero rows. First-five totals of both teams at a line of 1.5 or below (a first-inning total) =='
SELECT o.line_type, o.book, o.total_line, count(*) AS n_rows,
       min(g.game_date) AS first_date, max(g.game_date) AS last_date
FROM raw.game_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = :season AND o.book LIKE 'bp:%'
  AND o.market_type = 'f5_total'
  AND o.total_line <= 1.5
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 5. Healthy: zero rows. First-five ties priced above 0.35 implied (a first-inning tie) =='
WITH r AS (
  SELECT o.line_type, o.book,
         CASE WHEN o.draw_ml < 0 THEN -o.draw_ml::float8 / (-o.draw_ml + 100)
              ELSE 100.0 / (o.draw_ml + 100) END AS p_draw
  FROM raw.game_odds o
  JOIN raw.games g ON g.game_pk = o.game_pk
  WHERE g.season = :season AND o.book LIKE 'bp:%'
    AND o.market_type = 'f5_moneyline'
    AND o.draw_ml IS NOT NULL AND abs(o.draw_ml) >= 100
)
SELECT line_type, book, count(*) AS n_rows, round(avg(p_draw)::numeric, 3) AS mean_p_draw
FROM r
WHERE p_draw > 0.35
GROUP BY 1, 2
ORDER BY 1, 2;

-- -----------------------------------------------------------------------------
-- The guard's three three-way rules: three_way_sum_below_one (THREE_WAY_SUM_MIN =
-- 0.98: no book sells every outcome for less than the stake), and since the
-- book-order read of 2026-09-29 three_way_two_way_team_prices (the two team prices
-- alone at THREE_WAY_TEAM_SUM_MAX = 1.00 or more: a tie-refunded pair beside a tie)
-- and three_way_sum_above_max (the three add above THREE_WAY_SUM_MAX = 1.18).
\echo ''
\echo '== 5b. Healthy: zero rows. Three-way rows that cannot be one bet: the three prices add below 0.98 or above 1.18, or the two team prices alone reach 1.00 =='
WITH r AS (
  SELECT o.market_type, o.line_type, o.book,
         CASE WHEN o.home_ml < 0 THEN -o.home_ml::float8 / (-o.home_ml + 100)
              ELSE 100.0 / (o.home_ml + 100) END
       + CASE WHEN o.away_ml < 0 THEN -o.away_ml::float8 / (-o.away_ml + 100)
              ELSE 100.0 / (o.away_ml + 100) END
       + CASE WHEN o.draw_ml < 0 THEN -o.draw_ml::float8 / (-o.draw_ml + 100)
              ELSE 100.0 / (o.draw_ml + 100) END AS p_sum,
         CASE WHEN o.home_ml < 0 THEN -o.home_ml::float8 / (-o.home_ml + 100)
              ELSE 100.0 / (o.home_ml + 100) END
       + CASE WHEN o.away_ml < 0 THEN -o.away_ml::float8 / (-o.away_ml + 100)
              ELSE 100.0 / (o.away_ml + 100) END AS p_teams
  FROM raw.game_odds o
  JOIN raw.games g ON g.game_pk = o.game_pk
  WHERE g.season = :season AND o.book LIKE 'bp:%'
    AND o.market_type IN ('f1_moneyline', 'f5_moneyline')
    AND abs(o.home_ml) >= 100 AND abs(o.away_ml) >= 100 AND abs(o.draw_ml) >= 100
)
SELECT market_type, line_type, book, count(*) AS n_rows,
       round(min(p_sum)::numeric, 3) AS min_sum, round(max(p_sum)::numeric, 3) AS max_sum
FROM r
WHERE p_sum < 0.98 OR p_sum > 1.18 OR p_teams >= 1.0
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 6. Healthy: zero rows. Game-odds rows with a side (or the total line) missing =='
SELECT o.market_type, o.line_type, o.book, count(*) AS n_rows
FROM raw.game_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = :season AND o.book LIKE 'bp:%'
  AND (
       (o.market_type LIKE '%runline'
        AND (o.home_spread IS NULL OR o.away_spread IS NULL
             OR o.home_spread_ml IS NULL OR o.away_spread_ml IS NULL))
    OR (o.market_type IN ('moneyline', 'first_to_score', 'f1_moneyline', 'f5_moneyline')
        AND (o.home_ml IS NULL OR o.away_ml IS NULL))
    OR ((o.market_type LIKE '%total%' OR o.market_type = 'first_inning_run')
        AND (o.over_ml IS NULL OR o.under_ml IS NULL OR o.total_line IS NULL))
  )
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 6b. Healthy: zero rows. Full-game team-total rows at a line of 1 or below =='
-- The old rows of 2022-2024 held team totals at a line of 1 with an over price
-- no such line carries: Caesars' entries flagged is_off, which the old provider
-- read (traced on game 716358). The provider now skips every is_off line, but the
-- vendor's OPENER field carries no such flag: Caesars opened every 2024 team total
-- at 1 (876 rows). The load guard's team_total_placeholder_line refuses them.
SELECT o.market_type, o.line_type, o.book, o.total_line, count(*) AS n_rows
FROM raw.game_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = :season AND o.book LIKE 'bp:%'
  AND o.market_type IN ('team_total_home', 'team_total_away')
  AND o.total_line <= 1
GROUP BY 1, 2, 3, 4
ORDER BY 1, 2, 3, 4;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 7. Healthy: zero rows. Closing rows stamped more than 36 hours after the start of the game date (loose bound) =='
SELECT o.market_type, o.book, count(*) AS n_rows,
       max(o.book_line_at - (g.game_date::timestamp AT TIME ZONE 'UTC')) AS latest_after_date_start
FROM raw.game_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = :season AND o.book LIKE 'bp:%'
  AND o.line_type = 'closing'
  AND o.book_line_at > (g.game_date::timestamp AT TIME ZONE 'UTC') + interval '36 hours'
GROUP BY 1, 2
ORDER BY 1, 2;

\echo ''
\echo '== 7b. Info: bp: rows without a stamp (the provider stamps every row the vendor stamps) =='
SELECT o.line_type, o.book, count(*) AS n_rows
FROM raw.game_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = :season AND o.book LIKE 'bp:%' AND o.book_line_at IS NULL
GROUP BY 1, 2
ORDER BY 1, 2;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 8. Healthy: zero rows. Mixed rows: a row that fills another market kind''s columns =='
SELECT o.market_type, o.line_type, o.book, count(*) AS n_rows
FROM raw.game_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = :season AND o.book LIKE 'bp:%'
  AND (
       (o.market_type LIKE '%runline'
        AND (o.home_ml IS NOT NULL OR o.away_ml IS NOT NULL OR o.draw_ml IS NOT NULL
             OR o.total_line IS NOT NULL OR o.over_ml IS NOT NULL OR o.under_ml IS NOT NULL))
    OR (o.market_type IN ('moneyline', 'first_to_score')
        AND (o.draw_ml IS NOT NULL
             OR o.home_spread IS NOT NULL OR o.home_spread_ml IS NOT NULL
             OR o.away_spread IS NOT NULL OR o.away_spread_ml IS NOT NULL
             OR o.total_line IS NOT NULL OR o.over_ml IS NOT NULL OR o.under_ml IS NOT NULL))
    OR (o.market_type IN ('f1_moneyline', 'f5_moneyline')
        AND (o.home_spread IS NOT NULL OR o.home_spread_ml IS NOT NULL
             OR o.away_spread IS NOT NULL OR o.away_spread_ml IS NOT NULL
             OR o.total_line IS NOT NULL OR o.over_ml IS NOT NULL OR o.under_ml IS NOT NULL))
    OR ((o.market_type LIKE '%total%' OR o.market_type = 'first_inning_run')
        AND (o.home_ml IS NOT NULL OR o.away_ml IS NOT NULL OR o.draw_ml IS NOT NULL
             OR o.home_spread IS NOT NULL OR o.home_spread_ml IS NOT NULL
             OR o.away_spread IS NOT NULL OR o.away_spread_ml IS NOT NULL))
  )
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- -----------------------------------------------------------------------------
-- The dated first-five exclusion: pipeline/bettingpros_odds_provider.F5_EXCLUDED_BOOKS
-- on F5_EXCLUDED_MARKETS (DraftKings, bp:12, from 2025-03-01, on the first-five
-- run line and total only: its first-five moneyline is kept, and the guard
-- refuses its bad rows). Keep this literal in step with both.
\echo ''
\echo '== 9. Healthy: zero rows. First-five rows of an excluded book on or after its date, on its excluded markets =='
SELECT o.market_type, o.line_type, o.book, count(*) AS n_rows,
       min(g.game_date) AS first_date, max(g.game_date) AS last_date
FROM raw.game_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = :season
  AND (o.book = 'bp:12' AND g.game_date >= DATE '2025-03-01' AND o.market_type IN ('f5_runline', 'f5_total'))
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 10. Healthy: zero rows. Duplicate game-odds rows: more than one opening or closing row per (game, market, line type, book) =='
\echo '   The live cycle writes one current row per book per price change by design, so current rows are left out.'
SELECT d.market_type, d.line_type, d.book, count(*) AS n_groups, sum(d.n) AS n_rows
FROM (
  SELECT o.game_pk, o.market_type, o.line_type, o.book, count(*) AS n
  FROM raw.game_odds o
  JOIN raw.games g ON g.game_pk = o.game_pk
  WHERE g.season = :season AND o.book LIKE 'bp:%'
    AND o.line_type IN ('opening', 'closing')
  GROUP BY 1, 2, 3, 4
  HAVING count(*) > 1
) d
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 11. Healthy: zero rows. More than one opening book for one (game, market) =='
SELECT d.market_type, count(*) AS n_games, max(d.n_books) AS max_books
FROM (
  SELECT o.game_pk, o.market_type, count(DISTINCT o.book) AS n_books
  FROM raw.game_odds o
  JOIN raw.games g ON g.game_pk = o.game_pk
  WHERE g.season = :season AND o.book LIKE 'bp:%' AND o.line_type = 'opening'
  GROUP BY 1, 2
  HAVING count(DISTINCT o.book) > 1
) d
GROUP BY 1
ORDER BY 1;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 12. Healthy: zero rows. bp: labels that are not bp:<digits> =='
SELECT 'game_odds' AS tbl, o.book, count(*) AS n_rows
FROM raw.game_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = :season AND o.book LIKE 'bp:%' AND o.book !~ '^bp:[0-9]+$'
GROUP BY 1, 2
UNION ALL
SELECT 'prop_odds', p.book, count(*)
FROM raw.prop_odds p
JOIN raw.games g ON g.game_pk = p.game_pk
WHERE g.season = :season AND p.book LIKE 'bp:%' AND p.book !~ '^bp:[0-9]+$'
GROUP BY 1, 2
ORDER BY 1, 2;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 13. Info: the graded row''s coverage per market (closing), against the consensus rows =='
\echo '   A market that loses more than 3% of its consensus games is a finding (plan section 9).'
\echo '   A three-way game counts as graded only when a sportsbook row lists the tie (the scorer needs it).'
WITH c AS (
  SELECT DISTINCT o.game_pk, o.market_type
  FROM raw.game_odds o
  JOIN raw.games g ON g.game_pk = o.game_pk
  WHERE g.season = :season AND o.book = 'consensus' AND o.line_type = 'closing'
), b AS (
  SELECT DISTINCT o.game_pk, o.market_type
  FROM raw.game_odds o
  JOIN raw.games g ON g.game_pk = o.game_pk
  WHERE g.season = :season AND o.book LIKE 'bp:%' AND o.line_type = 'closing'
    AND o.book IN ('bp:10', 'bp:12', 'bp:13', 'bp:14', 'bp:15', 'bp:18', 'bp:19', 'bp:24', 'bp:27', 'bp:33', 'bp:49')
    AND NOT (o.draw_ml IS NULL AND o.market_type IN ('f1_moneyline', 'f5_moneyline'))
)
SELECT coalesce(c.market_type, b.market_type) AS market_type,
       count(c.game_pk) AS consensus_games,
       count(b.game_pk) AS graded_games,
       count(*) FILTER (WHERE c.game_pk IS NOT NULL AND b.game_pk IS NULL) AS lost_games,
       round(100.0 * count(*) FILTER (WHERE c.game_pk IS NOT NULL AND b.game_pk IS NULL)
             / NULLIF(count(c.game_pk), 0), 2) AS lost_pct
FROM c
FULL JOIN b ON b.game_pk = c.game_pk AND b.market_type = c.market_type
GROUP BY 1
ORDER BY 1;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 14. Info: which book the preference list grades, per market (closing) =='
\echo '   The backtest''s order: a three-way row with no tie after every row with one, then the list.'
WITH ranked AS (
  SELECT DISTINCT ON (o.game_pk, o.market_type) o.game_pk, o.market_type, o.book
  FROM raw.game_odds o
  JOIN raw.games g ON g.game_pk = o.game_pk
  WHERE g.season = :season AND o.book LIKE 'bp:%' AND o.line_type = 'closing'
    AND o.book IN ('bp:10', 'bp:12', 'bp:13', 'bp:14', 'bp:15', 'bp:18', 'bp:19', 'bp:24', 'bp:27', 'bp:33', 'bp:49')
  ORDER BY o.game_pk, o.market_type,
           (o.draw_ml IS NULL AND o.market_type IN ('f1_moneyline', 'f5_moneyline')),
           array_position(ARRAY['bp:12', 'bp:19', 'bp:10', 'bp:33', 'bp:18', 'bp:24', 'bp:13', 'bp:49', 'bp:14', 'bp:15', 'bp:27']::varchar[], o.book) NULLS LAST,
           o.fetched_at DESC
)
SELECT market_type, book AS graded_book, count(*) AS n_games
FROM ranked
GROUP BY 1, 2
ORDER BY 1, 3 DESC;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 15. Info: prop rows per market, line type and book =='
SELECT p.prop_stat, p.line_type, p.book, count(*) AS n_rows, count(DISTINCT p.game_pk) AS n_games
FROM raw.prop_odds p
JOIN raw.games g ON g.game_pk = p.game_pk
WHERE g.season = :season AND p.book LIKE 'bp:%'
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

\echo ''
\echo '== 16. Healthy: zero rows. Prop rows with a side (or the line) missing =='
SELECT p.prop_stat, p.line_type, p.book, count(*) AS n_rows
FROM raw.prop_odds p
JOIN raw.games g ON g.game_pk = p.game_pk
WHERE g.season = :season AND p.book LIKE 'bp:%'
  AND (p.over_ml IS NULL OR p.under_ml IS NULL OR p.line IS NULL)
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

\echo ''
\echo '== 17. Healthy: zero rows. Closing prop rows stamped more than 36 hours after the start of the game date (loose bound) =='
SELECT p.prop_stat, p.book, count(*) AS n_rows
FROM raw.prop_odds p
JOIN raw.games g ON g.game_pk = p.game_pk
WHERE g.season = :season AND p.book LIKE 'bp:%'
  AND p.line_type = 'closing'
  AND p.book_line_at > (g.game_date::timestamp AT TIME ZONE 'UTC') + interval '36 hours'
GROUP BY 1, 2
ORDER BY 1, 2;

\echo ''
\echo '== 18. Healthy: zero rows. Duplicate prop rows: more than one opening or closing row per (game, player, market, line type, book) =='
\echo '   Current rows are left out, as in section 10.'
SELECT d.prop_stat, d.line_type, d.book, count(*) AS n_groups, sum(d.n) AS n_rows
FROM (
  SELECT p.game_pk, p.player_id, p.prop_stat, p.line_type, p.book, count(*) AS n
  FROM raw.prop_odds p
  JOIN raw.games g ON g.game_pk = p.game_pk
  WHERE g.season = :season AND p.book LIKE 'bp:%'
    AND p.line_type IN ('opening', 'closing')
  GROUP BY 1, 2, 3, 4, 5
  HAVING count(*) > 1
) d
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

\echo ''
\echo '== 19. Healthy: zero rows. More than one opening book for one (game, player, market) =='
SELECT d.prop_stat, count(*) AS n_offers, max(d.n_books) AS max_books
FROM (
  SELECT p.game_pk, p.player_id, p.prop_stat, count(DISTINCT p.book) AS n_books
  FROM raw.prop_odds p
  JOIN raw.games g ON g.game_pk = p.game_pk
  WHERE g.season = :season AND p.book LIKE 'bp:%' AND p.line_type = 'opening'
  GROUP BY 1, 2, 3
  HAVING count(DISTINCT p.book) > 1
) d
GROUP BY 1
ORDER BY 1;

-- -----------------------------------------------------------------------------
\echo ''
\echo '== 20. Info: the consensus rows the retirement script archives for this season =='
SELECT 'game_odds' AS tbl, count(*) AS consensus_rows
FROM raw.game_odds o
JOIN raw.games g ON g.game_pk = o.game_pk
WHERE g.season = :season AND o.book = 'consensus'
UNION ALL
SELECT 'prop_odds', count(*)
FROM raw.prop_odds p
JOIN raw.games g ON g.game_pk = p.game_pk
WHERE g.season = :season AND p.book = 'consensus';

ROLLBACK;
