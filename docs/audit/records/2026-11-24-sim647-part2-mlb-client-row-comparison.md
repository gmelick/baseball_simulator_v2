# The one MLB client: the row comparison (SIM-647 part 2, Step B), 2026-11-24

The old code (master 0f0fd61) and the new code (branch `sim647-part2-ingest`, d72c06d)
each read the same 320 Final games of 2025 and 2026 from the live MLB Stats API, in
parallel. The games are the first 320 of `raw.games` ordered by `md5(game_pk)`. For each
game, each side ran the game loader's `_fetch_game_pitches`, the box-score fetch and
`parse_boxscore`, and the bullpen loader's schedule lookup. Each side also read the venue
dimensions of 2025 and 2026 once.

| What | Compared | Differences |
|---|---|---|
| The game loader's pitch rows | 93,831 rows | 0 |
| The game feed (`liveData`) | 320 feeds | 0 |
| The box-score rows | 9,488 rows | 0 |
| The bullpen loader's schedule lookup (date, home, away), by value | 320 lookups | 0 |
| The venue dimensions | 59 + 59 venues | 0 |
| Errors | old 0, new 0 | — |

The harness and the comparer: `results/sim647_rowcmp/` (not committed).
