/**
 * starterQueries.ts — SIM-439
 * Curated, ready-to-run example queries (from the Baseball Analyst's spec) that
 * seed the SQL console. Each models a safe pattern: bounded by an indexed
 * predicate, data_quality_flag = FALSE, HAVING sample floors, and a LIMIT.
 */
export interface StarterQuery {
  name: string
  sql: string
}

export const STARTER_QUERIES: StarterQuery[] = [
  {
    name: 'Pitcher arsenal & stuff (one pitcher-season)',
    sql: `SELECT pitch_type,
       COUNT(*)                                           AS pitches,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS usage_pct,
       ROUND(AVG(release_speed)::numeric, 1)             AS avg_velo,
       ROUND(AVG(break_vertical_induced)::numeric, 1)    AS avg_ivb,
       ROUND(AVG(break_horizontal)::numeric, 1)          AS avg_hb,
       ROUND(AVG(release_spin_rate)::numeric, 0)         AS avg_spin
FROM raw.pitches
WHERE pitcher = 543037            -- Gerrit Cole; swap the id
  AND season  = 2024
  AND data_quality_flag = FALSE
GROUP BY pitch_type
HAVING COUNT(*) >= 25
ORDER BY pitches DESC;`,
  },
  {
    name: 'League whiff-rate leaders (min swing sample)',
    sql: `SELECT p.pitcher,
       pl.full_name AS pitcher_name,
       COUNT(*) FILTER (WHERE p.description LIKE '%swinging_strike%')                        AS whiffs,
       COUNT(*) FILTER (WHERE p.description LIKE '%swinging_strike%'
                          OR p.description IN ('foul','hit_into_play','foul_tip'))            AS swings,
       ROUND(100.0 * COUNT(*) FILTER (WHERE p.description LIKE '%swinging_strike%')
             / NULLIF(COUNT(*) FILTER (WHERE p.description LIKE '%swinging_strike%'
                          OR p.description IN ('foul','hit_into_play','foul_tip')), 0), 1)    AS whiff_pct
FROM raw.pitches p
JOIN raw.players pl ON pl.player_id = p.pitcher
WHERE p.season = 2024 AND p.data_quality_flag = FALSE
GROUP BY p.pitcher, pl.full_name
HAVING COUNT(*) FILTER (WHERE p.description LIKE '%swinging_strike%'
                          OR p.description IN ('foul','hit_into_play','foul_tip')) >= 500
ORDER BY whiff_pct DESC
LIMIT 25;`,
  },
  {
    name: 'Batter batted-ball quality (EV / hard-hit / sweet-spot)',
    sql: `SELECT p.batter,
       pl.full_name AS batter_name,
       COUNT(*)                                                                              AS batted_balls,
       ROUND(AVG(p.launch_speed)::numeric, 1)                                                AS avg_ev,
       MAX(p.launch_speed)                                                                   AS max_ev,
       ROUND(100.0 * COUNT(*) FILTER (WHERE p.launch_speed >= 95) / COUNT(*), 1)             AS hard_hit_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE p.launch_angle BETWEEN 8 AND 32) / COUNT(*), 1)  AS sweet_spot_pct
FROM raw.pitches p
JOIN raw.players pl ON pl.player_id = p.batter
WHERE p.season = 2024 AND p.type = 'X' AND p.launch_speed IS NOT NULL AND p.data_quality_flag = FALSE
GROUP BY p.batter, pl.full_name
HAVING COUNT(*) >= 150
ORDER BY hard_hit_pct DESC
LIMIT 25;`,
  },
  {
    name: 'Chase rate — out-of-zone swing%',
    sql: `SELECT p.pitcher,
       pl.full_name AS pitcher_name,
       COUNT(*) FILTER (WHERE p.zone IN (11,12,13,14))                                       AS ooz_pitches,
       ROUND(100.0 * COUNT(*) FILTER (WHERE p.zone IN (11,12,13,14)
                        AND (p.description LIKE '%swinging_strike%'
                             OR p.description IN ('foul','hit_into_play','foul_tip')))
             / NULLIF(COUNT(*) FILTER (WHERE p.zone IN (11,12,13,14)), 0), 1)                AS chase_pct
FROM raw.pitches p
JOIN raw.players pl ON pl.player_id = p.pitcher
WHERE p.season = 2024 AND p.data_quality_flag = FALSE AND p.zone IS NOT NULL
GROUP BY p.pitcher, pl.full_name
HAVING COUNT(*) FILTER (WHERE p.zone IN (11,12,13,14)) >= 400
ORDER BY chase_pct DESC
LIMIT 25;`,
  },
  {
    name: 'Count-state behavior (swing & whiff by count)',
    sql: `SELECT balls, strikes,
       COUNT(*)                                                                              AS pitches,
       ROUND(100.0 * COUNT(*) FILTER (WHERE description LIKE '%swinging_strike%'
                          OR description IN ('foul','hit_into_play','foul_tip')) / COUNT(*), 1) AS swing_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE description LIKE '%swinging_strike%')
             / NULLIF(COUNT(*) FILTER (WHERE description LIKE '%swinging_strike%'
                          OR description IN ('foul','hit_into_play','foul_tip')), 0), 1)      AS whiff_pct
FROM raw.pitches
WHERE season = 2024 AND data_quality_flag = FALSE
  AND balls BETWEEN 0 AND 3 AND strikes BETWEEN 0 AND 2
GROUP BY balls, strikes
ORDER BY balls, strikes;`,
  },
  {
    name: 'Batter platoon split vs LHP / RHP',
    sql: `SELECT p.p_throws AS vs_hand,
       COUNT(*) FILTER (WHERE p.events IS NOT NULL)                                          AS pa,
       ROUND(100.0 * COUNT(*) FILTER (WHERE p.events IN ('strikeout','strikeout_double_play'))
             / NULLIF(COUNT(*) FILTER (WHERE p.events IS NOT NULL), 0), 1)                   AS k_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE p.events = 'walk')
             / NULLIF(COUNT(*) FILTER (WHERE p.events IS NOT NULL), 0), 1)                   AS bb_pct,
       COUNT(*) FILTER (WHERE p.events = 'home_run')                                         AS hr
FROM raw.pitches p
WHERE p.batter = 665742            -- swap the id
  AND p.season = 2024 AND p.data_quality_flag = FALSE
GROUP BY p.p_throws
ORDER BY p.p_throws;`,
  },
  {
    name: 'Catcher stolen-base defense (attempts & CS%)',
    sql: `SELECT p.fielder_2 AS catcher_id,
       pl.full_name AS catcher_name,
       COUNT(*) FILTER (WHERE p.sb_attempt_2b OR p.sb_attempt_3b OR p.sb_attempt_home)       AS sb_attempts,
       COUNT(*) FILTER (WHERE p.sb_success_2b OR p.sb_success_3b OR p.sb_success_home)        AS sb_success,
       ROUND(100.0 * (COUNT(*) FILTER (WHERE p.sb_attempt_2b OR p.sb_attempt_3b OR p.sb_attempt_home)
                    - COUNT(*) FILTER (WHERE p.sb_success_2b OR p.sb_success_3b OR p.sb_success_home))
             / NULLIF(COUNT(*) FILTER (WHERE p.sb_attempt_2b OR p.sb_attempt_3b OR p.sb_attempt_home), 0), 1) AS cs_pct
FROM raw.pitches p
JOIN raw.players pl ON pl.player_id = p.fielder_2
WHERE p.season = 2024 AND (p.sb_attempt_2b OR p.sb_attempt_3b OR p.sb_attempt_home)
GROUP BY p.fielder_2, pl.full_name
HAVING COUNT(*) FILTER (WHERE p.sb_attempt_2b OR p.sb_attempt_3b OR p.sb_attempt_home) >= 20
ORDER BY cs_pct DESC
LIMIT 25;`,
  },
  {
    name: 'Velocity trend by month (fatigue check)',
    sql: `SELECT date_trunc('month', game_date)::date AS month,
       COUNT(*)                                  AS fastballs,
       ROUND(AVG(release_speed)::numeric, 1)     AS avg_velo,
       ROUND(MAX(release_speed)::numeric, 1)     AS peak_velo
FROM raw.pitches
WHERE pitcher = 543037
  AND season  = 2024
  AND pitch_type IN ('FF','SI','FC')
  AND data_quality_flag = FALSE
GROUP BY 1
ORDER BY 1;`,
  },
  {
    name: 'Safe exploration template (bounded + LIMIT)',
    sql: `SELECT game_date, pitcher, batter, pitch_type, release_speed,
       break_vertical_induced, break_horizontal, plate_x, plate_z,
       description, events
FROM raw.pitches
WHERE pitcher = 543037
  AND season  = 2024
  AND data_quality_flag = FALSE
ORDER BY game_date, at_bat_number, pitch_number
LIMIT 200;`,
  },
]
