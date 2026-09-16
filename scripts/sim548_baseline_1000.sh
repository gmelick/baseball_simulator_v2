#!/bin/sh
# SIM-548 — the 1,000-game accuracy baseline of production (the split ON at
# 16 / 16 / 8 and the fatigue weight at 0.5 since 2026-09-14): the simulator's
# Brier score against the closing line's, per market, on the first 1,000 Final
# games of 2024 by game id at 100 iterations. Runs INSIDE the app container on
# the compose environment. The first 250 games are already on record under the
# identical configuration (scripts/sim548_accuracy_split.json — the same seeds
# and bundle), so this script runs games 251-1000 as three 250-game chunks,
# each writing its own report (a crash loses one chunk, not the run):
#   scripts/sim548_baseline_2024_chunk{2,3,4}.json
# The skill table (scripts/sim548_market_skill.py) merges the four reports.
#
#   MSYS_NO_PATHCONV=1 docker compose run -d --rm --name sim548_baseline \
#       -v "$PWD/scripts:/app/scripts" app sh scripts/sim548_baseline_1000.sh
set -u
ITERS="${SIM427_ITERS:-100}"
WORKERS="${SIM427_WORKERS:-5}"
cd /app || exit 1
LOG=/app/scripts/sim548_baseline_1000.log
echo "baseline start $(date -u +%FT%TZ) iters=$ITERS workers=$WORKERS split=${SIM_PITCH_RESULT_SPLIT:-unset} fatigue_tto=${SIM_FATIGUE_TTO_SIGMA:-unset}" > "$LOG"
for CH in 2 3 4; do
  echo "chunk $CH start $(date -u +%FT%TZ)" >> "$LOG"
  python scripts/clv_backtest.py --seasons 2024 --iterations "$ITERS" --workers "$WORKERS" \
    --game-pks-file "/app/scripts/sim548_games_2024_chunk$CH.txt" \
    --output "/app/scripts/sim548_baseline_2024_chunk$CH.json" > "/app/scripts/sim548_baseline_2024_chunk$CH.run.log" 2>&1
  echo "chunk $CH exit $? $(date -u +%FT%TZ)" >> "$LOG"
done
echo "baseline complete $(date -u +%FT%TZ)" >> "$LOG"
