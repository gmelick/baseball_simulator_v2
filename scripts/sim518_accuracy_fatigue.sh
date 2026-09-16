#!/bin/sh
# SIM-518 — the fatigue factor's paired accuracy run (the fatigue plan's step 3).
# Runs INSIDE the app container on the flipped production (the manager draw, the
# real pen — the compose env): the fatigue arm at a times-through-the-order
# bandwidth of 0.5, then 0.7, on the SAME 250 games, seeds and bundle as the
# manager flip's ON arm (scripts/sim427_accuracy_on.json), which is this run's
# baseline (fatigue OFF). Pair each with scripts/sim518_pair_accuracy.py.
#
#   MSYS_NO_PATHCONV=1 docker compose run -d --rm --name sim518_accuracy \
#       -e SIM427_MAX_GAMES=250 -v "$PWD/scripts:/app/scripts" app sh scripts/sim518_accuracy_fatigue.sh
set -u
ITERS="${SIM427_ITERS:-100}"
WORKERS="${SIM427_WORKERS:-5}"
MAXG="${SIM427_MAX_GAMES:-250}"
cd /app || exit 1
echo "fatigue arms start $(date -u +%FT%TZ) iters=$ITERS workers=$WORKERS max_games=$MAXG" > /app/scripts/sim518_accuracy_fatigue.log
for SIGMA in 0.5 0.7; do
  TAG=$(echo "$SIGMA" | tr -d .)
  echo "tto $SIGMA start $(date -u +%FT%TZ)" >> /app/scripts/sim518_accuracy_fatigue.log
  SIM_FATIGUE_PC_SIGMA=0 SIM_FATIGUE_TTO_SIGMA="$SIGMA" \
    python scripts/clv_backtest.py --seasons 2024 --iterations "$ITERS" --workers "$WORKERS" --max-games "$MAXG" \
      --output "/app/scripts/sim518_accuracy_tto$TAG.json" > "/app/scripts/sim518_accuracy_tto$TAG.log" 2>&1
  echo "tto $SIGMA exit $? $(date -u +%FT%TZ)" >> /app/scripts/sim518_accuracy_fatigue.log
done
echo "fatigue arms complete $(date -u +%FT%TZ)" >> /app/scripts/sim518_accuracy_fatigue.log
