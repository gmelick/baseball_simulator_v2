-- SIM-555 (2026-10-01): move the bp: odds rows of ten double-header game 2s that were
-- matched to game 1's vendor event into the archive, in one transaction.
--
-- MLB lists no real start for game 2 of a straight double-header: its gameDate is a
-- placeholder 5 minutes after game 1's start (status.startTimeTBD). The event matcher
-- took the event nearest that placeholder, which is game 1's, so these games' rows hold
-- game 1's prices. The matcher fix (pipeline/bettingpros_odds_provider.py,
-- _match_game_two) now takes the later of the day's two same-team events. A read-only
-- check of every postponed, suspended or double-header game with bp: rows (340 games;
-- the old and the new matcher's event compared) found exactly these ten changed:
--   566734 (2019), 630973 630984 631152 631426 (2020), 661233 662199 662459 (2022),
--   745310 745659 (2024).
-- After this move, the loader re-loads the ten games with the fixed matcher. The
-- archive keeps the rows; its readers take only 'consensus' rows, so these stay out of
-- every read. The live and archive tables share one column order (checked 2026-10-01).
-- Run from the repo root:
--   docker compose exec -T db psql -U baseball_user -d baseball_sim -v ON_ERROR_STOP=1 < scripts/sim555_archive_wrong_dh_game2.sql

BEGIN;

CREATE TEMP TABLE sim555_wrong_game2 (game_pk integer PRIMARY KEY) ON COMMIT DROP;
INSERT INTO sim555_wrong_game2 VALUES
  (566734), (630973), (630984), (631152), (631426),
  (661233), (662199), (662459), (745310), (745659);

SELECT
  (SELECT count(*) FROM raw.game_odds o JOIN sim555_wrong_game2 USING (game_pk) WHERE o.book LIKE 'bp:%') AS game_rows_to_move,
  (SELECT count(*) FROM raw.prop_odds p JOIN sim555_wrong_game2 USING (game_pk) WHERE p.book LIKE 'bp:%') AS prop_rows_to_move;

INSERT INTO raw.game_odds_archive
SELECT o.* FROM raw.game_odds o JOIN sim555_wrong_game2 USING (game_pk) WHERE o.book LIKE 'bp:%';
DELETE FROM raw.game_odds o USING sim555_wrong_game2 w WHERE o.game_pk = w.game_pk AND o.book LIKE 'bp:%';

INSERT INTO raw.prop_odds_archive
SELECT p.* FROM raw.prop_odds p JOIN sim555_wrong_game2 USING (game_pk) WHERE p.book LIKE 'bp:%';
DELETE FROM raw.prop_odds p USING sim555_wrong_game2 w WHERE p.game_pk = w.game_pk AND p.book LIKE 'bp:%';

SELECT
  (SELECT count(*) FROM raw.game_odds o JOIN sim555_wrong_game2 USING (game_pk) WHERE o.book LIKE 'bp:%') AS game_rows_left,
  (SELECT count(*) FROM raw.prop_odds p JOIN sim555_wrong_game2 USING (game_pk) WHERE p.book LIKE 'bp:%') AS prop_rows_left;

COMMIT;
