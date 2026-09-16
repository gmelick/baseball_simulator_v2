#!/bin/sh
# SIM-427 part 4f: run the reliever-weight ladder arms, at most 3 probe containers at once.
# Usage: sh scripts/sim427_ladder.sh   (from the repo root, Git Bash; needs docker compose)
cd "$(dirname "$0")/.." || exit 1
run_arm() {
  spec="$1"; tag="$2"
  [ -f "scripts/sim427_probe_$tag.json" ] && return 0
  while [ "$(docker ps --format '{{.Names}}' | grep -c sim427_probe_)" -ge 3 ]; do sleep 20; done
  MSYS_NO_PATHCONV=1 docker compose run -d --rm --name "sim427_probe_$tag" -v "$PWD/scripts:/app/scripts" app \
    sh -c "python scripts/sim427_manager_probe.py run --arm '$spec' --iters 20 --json-out /app/scripts/sim427_probe_$tag.json > /app/scripts/sim427_probe_$tag.log 2>&1" >/dev/null 2>&1
  echo "started $tag ($spec) $(date +%H:%M:%S)"
  sleep 5
}
run_arm "draw=1,pen=box,mgr=2,role=0.05" r_role005
run_arm "draw=1,pen=box,mgr=2,role=0.2" r_role020
run_arm "draw=1,pen=box,mgr=2,rest=0.5" r_rest05
run_arm "draw=1,pen=box,mgr=2,rest=2" r_rest2
run_arm "draw=1,pen=box,mgr=2,hand=0.25" r_hand025
run_arm "draw=1,pen=box,mgr=2,p2d=0.5" r_p2d05
run_arm "draw=1,pen=box,mgr=2,p2d=0.25" r_p2d025
run_arm "draw=1,pen=box,mgr=2,p3d=10" r_p3d10
run_arm "draw=1,pen=box,mgr=2,p3d=20" r_p3d20
run_arm "draw=1,pen=box,mgr=2,p3d=40" r_p3d40
run_arm "draw=1,pen=box,mgr=2,stuff=1" r_stuff1
run_arm "draw=1,pen=box,mgr=2,stuff=2" r_stuff2
while [ "$(docker ps --format '{{.Names}}' | grep -c sim427_probe_)" -gt 0 ]; do sleep 30; done
echo "ladder complete $(date +%H:%M:%S)"
