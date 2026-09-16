#!/bin/sh
# SIM-548 Part A4 — the pitch / pitch-result split as one paired accuracy arm.
# Runs INSIDE the app container on production as the compose file sets it
# (the manager draw, the real pen, the fatigue weight at 0.5 since 2026-09-14):
# the split ON at its 2026-09-09 fit — the pitcher at power 16 in the pitch
# draw and the result draw, the batter at power 8 in the result draw, the
# pitch-to-pitch bandwidth 1.0 with the density correction — on the SAME 250
# games, seeds and bundle as the baseline. The baseline is production with the
# fatigue weight at 0.5: scripts/sim518_accuracy_tto05.json. Pair with
# scripts/sim518_pair_accuracy.py (baseline first, this arm second).
#
#   MSYS_NO_PATHCONV=1 docker compose run -d --rm --name sim548_split \
#       -e SIM427_MAX_GAMES=250 -v "$PWD/scripts:/app/scripts" app sh scripts/sim548_accuracy_split.sh
set -u
ITERS="${SIM427_ITERS:-100}"
WORKERS="${SIM427_WORKERS:-5}"
MAXG="${SIM427_MAX_GAMES:-250}"
cd /app || exit 1
LOG=/app/scripts/sim548_accuracy_split.log
echo "split arm start $(date -u +%FT%TZ) iters=$ITERS workers=$WORKERS max_games=$MAXG fatigue_tto=${SIM_FATIGUE_TTO_SIGMA:-unset}" > "$LOG"
SIM_PITCH_RESULT_SPLIT=1 SIM_PITCH_PITCHER_POWER=16 SIM_RESULT_PITCHER_POWER=16 SIM_RESULT_BATTER_POWER=8 \
  python scripts/clv_backtest.py --seasons 2024 --iterations "$ITERS" --workers "$WORKERS" --max-games "$MAXG" \
    --output /app/scripts/sim548_accuracy_split.json > /app/scripts/sim548_accuracy_split.run.log 2>&1
echo "split arm exit $? $(date -u +%FT%TZ)" >> "$LOG"
