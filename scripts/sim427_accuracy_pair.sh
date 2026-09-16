#!/bin/sh
# SIM-427 — the paired accuracy run (the plan's §4f grade / §5 second flip condition).
# Runs INSIDE the app container: the ON arm (the fitted manager draw, the real pen,
# the reliever draw) and then the OFF arm (production today: the SIM-434 formula,
# the synthetic pen), the same 2024 games, the same seeds, the same frozen bundle.
# Pair the two reports with scripts/sim518_pair_accuracy.py.
#
#   MSYS_NO_PATHCONV=1 docker compose run -d --rm --name sim427_accuracy \
#       -e SIM427_MAX_GAMES=250 -v "$PWD/scripts:/app/scripts" app sh scripts/sim427_accuracy_pair.sh
#
# SIM427_ITERS (100) and SIM427_WORKERS (5) set the run; SIM427_MAX_GAMES caps it to
# the first N Final games of 2024 by game_pk — a game_pk prefix spans the whole season
# (the first 250 cover 150 distinct dates), the subset read the owner chose on
# 2026-09-13; unset = the full season.
set -u
ITERS="${SIM427_ITERS:-100}"
WORKERS="${SIM427_WORKERS:-5}"
MAXG="${SIM427_MAX_GAMES:-}"
CAP=""
if [ -n "$MAXG" ]; then CAP="--max-games $MAXG"; fi
cd /app || exit 1
echo "ON arm start $(date -u +%FT%TZ) iters=$ITERS workers=$WORKERS max_games=${MAXG:-all}" > /app/scripts/sim427_accuracy_pair.log
SIM_MANAGER_DRAW=1 SIM_BULLPEN_SOURCE=box SIM_ACTOR_POWER_MANAGER_USAGE=4 \
SIM_RELIEF_ROLE_SIGMA=0.1 SIM_RELIEF_REST_SIGMA=0.5 \
SIM_RELIEF_PITCHED2D_OFF_WEIGHT=0.25 SIM_RELIEF_PITCHES3D_SIGMA=10 \
  python scripts/clv_backtest.py --seasons 2024 --iterations "$ITERS" --workers "$WORKERS" $CAP \
    --output /app/scripts/sim427_accuracy_on.json > /app/scripts/sim427_accuracy_on.log 2>&1
echo "ON arm exit $? $(date -u +%FT%TZ)" >> /app/scripts/sim427_accuracy_pair.log
echo "OFF arm start $(date -u +%FT%TZ)" >> /app/scripts/sim427_accuracy_pair.log
python scripts/clv_backtest.py --seasons 2024 --iterations "$ITERS" --workers "$WORKERS" $CAP \
    --output /app/scripts/sim427_accuracy_off.json > /app/scripts/sim427_accuracy_off.log 2>&1
echo "OFF arm exit $? $(date -u +%FT%TZ)" >> /app/scripts/sim427_accuracy_pair.log
echo "pair complete $(date -u +%FT%TZ)" >> /app/scripts/sim427_accuracy_pair.log
