#!/bin/sh
# SIM-548 Phase 3 — the designed experiment on the full simulator (the review's §4 design):
# four factors as a full 2^4 — the pitch draw's pitcher power 16 / 4, the result draw's
# pitcher power 16 / 4, the result draw's batter power 8 / 2, the fatigue bandwidth 0.5 / 0 —
# three baseline repeats, two centre arms at (8, 8, 4, 0.5), then two supplementary
# split-OFF arms outside the factorial (pitcher power 16 and 4). Every arm: the fresh 250
# games of 2024 (games 1001-1250 by game id), 100 iterations, the same bundle; the runner
# skips an arm whose report exists (resumable). Runs INSIDE the app container with the
# app service STOPPED (ten workers need the VM's memory).
#
#   MSYS_NO_PATHCONV=1 docker compose run -d --rm --name sim548_design \
#       -v "$PWD/scripts:/app/scripts" app sh scripts/sim548_design_run.sh
set -u
OUT=/app/scripts/sim548_design_20260915
GAMES=/app/scripts/sim548_games_2024_design.txt
WORKERS="${SIM548_WORKERS:-10}"
ITERS="${SIM548_ITERS:-100}"
mkdir -p "$OUT"
LOG="$OUT/run.log"
echo "design start $(date -u +%FT%TZ) workers=$WORKERS iters=$ITERS" >> "$LOG"
cd /app || exit 1
python scripts/sim548_design.py \
  --factors '{"SIM_PITCH_PITCHER_POWER": ["16", "4", "8"], "SIM_RESULT_PITCHER_POWER": ["16", "4", "8"], "SIM_RESULT_BATTER_POWER": ["8", "2", "4"], "SIM_FATIGUE_TTO_SIGMA": ["0.5", "0", "0.5"]}' \
  --baseline-repeats 3 --centre-arms 2 --base-seed 0 \
  --game-pks-file "$GAMES" --seasons 2024 --iterations "$ITERS" --workers "$WORKERS" \
  --out-dir "$OUT" >> "$LOG" 2>&1
echo "design exit $? $(date -u +%FT%TZ)" >> "$LOG"
for P in 16 4; do
  if [ ! -f "$OUT/splitoff_p$P.json" ]; then
    echo "splitoff p$P start $(date -u +%FT%TZ)" >> "$LOG"
    SIM_PITCH_RESULT_SPLIT=0 SIM_PITCH_PITCHER_POWER=$P \
      python scripts/clv_backtest.py --seasons 2024 --iterations "$ITERS" --workers "$WORKERS" --base-seed 0 \
        --game-pks-file "$GAMES" --output "$OUT/splitoff_p$P.json" > "$OUT/splitoff_p$P.run.log" 2>&1
    echo "splitoff p$P exit $? $(date -u +%FT%TZ)" >> "$LOG"
  fi
done
echo "design complete $(date -u +%FT%TZ)" >> "$LOG"
