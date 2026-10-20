"""Prove that a refactor leaves the simulator's output unchanged.

The tool runs seeded games, records every pitch of each game, and writes one
SHA-256 hash per game to a JSON record. Run it on the code before a change and
on the code after it, then compare the two records. Equal records mean the
change is behaviour-neutral: the same seeds give the same play stream, the same
box score and the same linescore, byte for byte.

The record is a comparison between two commits on the same day. It is not a
golden file, and no test reads it.

Modes
-----
``nodb``
    The no-database factory (``simulation.batch_runner:rng_driven_machine_factory``)
    over the in-memory league bundle, the path every unit test runs. No flags
    apply.
``synthetic``
    The production factory over the in-memory league bundle, once with every
    ``SIM_*`` flag unset (the code defaults) and once with the app service's
    flags from ``docker-compose.yml``. This exercises the flag wiring without a
    database.
``production``
    The production factory over a real bundle with the real lineups of the 45
    balanced games (``tests/acceptance/bands.py``), with the app service's flags.
    Needs Postgres and the sim DuckDB, so it runs in the app container. Pass
    ``--bundle-dir`` with a COPY of one bundle version: the nightly rebuild
    moves the published link, and pruning keeps only three versions. Pass
    ``--kwargs-cache``: the first run writes each game's resolved inputs there,
    and every later run reads them, so the nightly data jobs cannot move the
    record.
``bundle``
    Hash every array of a built bundle by content (``--bundle-dir``). Build two
    bundles with ``python -m pipeline.batch.engine_artifacts --out-dir ...``, one
    on each commit, and fingerprint both.
``compare``
    Diff two records and exit 1 when any hash differs.

Usage
-----
    python scripts/behaviour_fingerprint.py nodb --out results/sim493/fp_nodb.json
    python scripts/behaviour_fingerprint.py synthetic --out results/sim493/fp_synth.json
    python scripts/behaviour_fingerprint.py production --bundle-dir /data/sim493/bundle_pin \\
        --kwargs-cache results/sim493/production_kwargs.json --out results/sim493/fp_prod.json
    python scripts/behaviour_fingerprint.py bundle --bundle-dir /data/x --out fp_bundle.json
    python scripts/behaviour_fingerprint.py compare before.json after.json
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import enum
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

NODB_FACTORY = "simulation.batch_runner:rng_driven_machine_factory"
PRODUCTION_FACTORY = "simulation.production_factory:production_machine_factory"

#: The synthetic games' rosters: two nine-man lineups and two starters.
_AWAY_LINEUP = list(range(101, 110))
_HOME_LINEUP = list(range(201, 210))
_AWAY_STARTER = 301
_HOME_STARTER = 401


# ---------------------------------------------------------------------------
# The canonical form: every value the loop hands back, as plain JSON data
# ---------------------------------------------------------------------------


def canonical(obj: Any, _seen: frozenset[int] = frozenset()) -> Any:
    """Turn ``obj`` into plain JSON data with a fixed order.

    Floats keep every bit (``float.hex``). Dict keys and sets are sorted, so a
    change in insertion order alone does not read as a change in behaviour.
    Class names appear without their module, so a class that moves to another
    module keeps its fingerprint. A cycle back to an object already on the path
    reads as ``"<cycle>"``.
    """
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, enum.Enum):
        return f"{type(obj).__name__}.{obj.name}"
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return float.hex(obj)
    if isinstance(obj, np.generic):
        return canonical(obj.item(), _seen)
    if isinstance(obj, np.ndarray):
        return {
            "ndarray": str(obj.dtype),
            "shape": list(obj.shape),
            "sha256": hashlib.sha256(np.ascontiguousarray(obj).tobytes()).hexdigest(),
        }
    if isinstance(obj, np.random.Generator):
        return {"rng": canonical(obj.bit_generator.state, _seen)}
    if id(obj) in _seen:
        return "<cycle>"
    seen = _seen | {id(obj)}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        body = {f.name: canonical(getattr(obj, f.name), seen) for f in dataclasses.fields(obj)}
        return {"class": type(obj).__name__, **body}
    if isinstance(obj, Mapping):
        items = [[canonical(k, seen), canonical(v, seen)] for k, v in obj.items()]
        return {"map": sorted(items, key=lambda kv: json.dumps(kv[0], sort_keys=True))}
    if isinstance(obj, (set, frozenset)):
        return {"set": sorted((canonical(x, seen) for x in obj), key=json.dumps)}
    if isinstance(obj, (list, tuple)):
        return [canonical(x, seen) for x in obj]
    attrs = _object_attrs(obj)
    if attrs is not None:
        return {"class": type(obj).__name__, **{k: canonical(v, seen) for k, v in attrs}}
    return {"repr": type(obj).__name__}


def _object_attrs(obj: Any) -> list[tuple[str, Any]] | None:
    """The instance attributes of a plain object, sorted by name, or None."""
    names: set[str] = set()
    if hasattr(obj, "__dict__"):
        names.update(vars(obj))
    for cls in type(obj).__mro__:
        names.update(n for n in getattr(cls, "__slots__", ()) if hasattr(obj, n))
    if not names:
        return None
    return [(n, getattr(obj, n)) for n in sorted(names) if not n.startswith("__")]


def digest(obj: Any) -> str:
    """The SHA-256 of ``obj``'s canonical form."""
    text = json.dumps(canonical(obj), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def game_digest(recorded: Any) -> str:
    """One hash for a recorded game: the result, every play, every context."""
    return digest(
        {
            "result": recorded.result,
            "plays": recorded.plays,
            "contexts": recorded.contexts,
        }
    )


# ---------------------------------------------------------------------------
# Flags and the in-memory bundle
# ---------------------------------------------------------------------------


def compose_app_flags(compose_path: Path | None = None) -> dict[str, str]:
    """The ``SIM_*`` values of the app service in ``docker-compose.yml``."""
    import yaml

    path = compose_path or _ROOT / "docker-compose.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    env = doc["services"]["app"].get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env if "=" in item)
    return {k: str(v) for k, v in env.items() if k.startswith("SIM_")}


@contextmanager
def sim_flags(flags: Mapping[str, str]) -> Iterator[None]:
    """Set exactly ``flags`` as the ``SIM_*`` environment for the block."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("SIM_")}
    for key in saved:
        del os.environ[key]
    os.environ.update(flags)
    try:
        yield
    finally:
        for key in [k for k in os.environ if k.startswith("SIM_")]:
            del os.environ[key]
        os.environ.update(saved)


@contextmanager
def in_memory_bundle() -> Iterator[None]:
    """Make the production factory load the in-memory league bundle.

    The bundle carries the steal pools and the change pool, so the running game
    and the pitching change draw.
    """
    from pipeline.batch.engine_artifacts import EngineArtifacts
    from simulation import production_factory
    from simulation.synthetic_bundle import league_artifacts

    original = EngineArtifacts.__dict__["load"]

    def _load(cls: type, *_args: Any, **_kwargs: Any) -> Any:
        return league_artifacts(steal=(0.06, 0.78), manager=True)

    EngineArtifacts.load = classmethod(_load)  # type: ignore[method-assign,assignment]
    production_factory.reset_caches()
    try:
        yield
    finally:
        EngineArtifacts.load = original  # type: ignore[method-assign]
        production_factory.reset_caches()


def _synthetic_kwargs() -> dict[str, Any]:
    hands = {pid: ("L" if pid % 3 == 0 else "R") for pid in _AWAY_LINEUP + _HOME_LINEUP}
    throws = {_AWAY_STARTER: "R", _HOME_STARTER: "L"}
    return {
        "away_lineup": list(_AWAY_LINEUP),
        "home_lineup": list(_HOME_LINEUP),
        "away_pitcher_id": _AWAY_STARTER,
        "home_pitcher_id": _HOME_STARTER,
        "pitcher_id": _AWAY_STARTER,
        "bat_hands": hands,
        "throw_hands": throws,
        "season": 2024,
    }


def _run_games(
    label: str, factory_ref: str, kwargs: dict[str, Any], seeds: range
) -> dict[str, str]:
    from simulation.play_recorder import record_game

    out: dict[str, str] = {}
    for seed in seeds:
        recorded = record_game(factory_ref=factory_ref, seed=seed, sim_kwargs=dict(kwargs))
        out[f"{label}/seed{seed}"] = game_digest(recorded)
    return out


def run_nodb(games: int) -> dict[str, str]:
    with sim_flags({}):
        return _run_games("nodb", NODB_FACTORY, _synthetic_kwargs(), range(games))


#: The app flags the in-memory bundle cannot serve: its pitch pool has no
#: base-out cell structure, so the cell index refuses it. The production mode
#: covers these flags on a real bundle.
_FLAGS_THE_MEMORY_BUNDLE_CANNOT_SERVE = ("SIM_PITCH_CELL_INDEX",)


def run_synthetic(games: int) -> dict[str, str]:
    out: dict[str, str] = {}
    app_flags = {
        k: v
        for k, v in compose_app_flags().items()
        if k not in _FLAGS_THE_MEMORY_BUNDLE_CANNOT_SERVE
    }
    variants: list[tuple[str, dict[str, str]]] = [
        ("synthetic-defaults", {}),
        ("synthetic-production", app_flags),
    ]
    for label, flags in variants:
        with sim_flags(flags), in_memory_bundle():
            out.update(_run_games(label, PRODUCTION_FACTORY, _synthetic_kwargs(), range(games)))
    return out


# ---------------------------------------------------------------------------
# The production bundle with real lineups
# ---------------------------------------------------------------------------


def balanced_game_pks() -> tuple[int, ...]:
    from tests.acceptance.bands import BALANCED_GAME_ORDER

    return tuple(int(gp) for gp in BALANCED_GAME_ORDER)


async def _real_kwargs(game_pks: tuple[int, ...]) -> dict[int, dict[str, Any]]:
    import asyncpg

    from pipeline.db_dsn import require_dsn
    from simulation.lineup_resolver import resolve_game_state
    from simulation.sim_kwargs import build_sim_kwargs, open_sim_duckdb

    duck = open_sim_duckdb()
    conn = await asyncpg.connect(require_dsn())
    try:
        out: dict[int, dict[str, Any]] = {}
        for gp in game_pks:
            state = await resolve_game_state(conn, gp, seed=0)
            out[gp] = await build_sim_kwargs(state, pool=conn, con=duck, game_pk=gp)
        return out
    finally:
        await conn.close()
        if duck is not None:
            duck.close()


def _encode(value: Any) -> Any:
    """Encode a sim-kwargs value as JSON that keeps its types: int keys, tuples, float bits."""
    if isinstance(value, Mapping):
        return {"dict": [[_encode(k), _encode(v)] for k, v in value.items()]}
    if isinstance(value, tuple):
        return {"tuple": [_encode(v) for v in value]}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return {"set": [_encode(v) for v in sorted(value)]}
    if isinstance(value, np.generic):
        return _encode(value.item())
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return {"float": float.hex(value)}
    raise TypeError(f"cannot cache a sim-kwargs value of type {type(value).__name__}")


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        ((kind, body),) = value.items()
        if kind == "dict":
            return {_decode(k): _decode(v) for k, v in body}
        if kind == "tuple":
            return tuple(_decode(v) for v in body)
        if kind == "set":
            return {_decode(v) for v in body}
        if kind == "float":
            return float.fromhex(body)
        raise ValueError(f"unknown cached kind {kind!r}")
    return value


def production_kwargs(
    game_pks: tuple[int, ...], cache_path: str | None
) -> dict[int, dict[str, Any]]:
    """The games' sim-kwargs, from ``cache_path`` when it exists.

    The kwargs read Postgres and the sim DuckDB, which the nightly jobs change.
    A cache written on the first run makes every later run depend on the pinned
    bundle alone.
    """
    if cache_path and Path(cache_path).exists():
        cached = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        by_game = {int(gp): _decode(kw) for gp, kw in cached.items()}
        missing = [gp for gp in game_pks if gp not in by_game]
        if missing:
            raise SystemExit(f"{cache_path} lacks games {missing}; delete it to rebuild")
        return {gp: by_game[gp] for gp in game_pks}
    resolved = asyncio.run(_real_kwargs(game_pks))
    if cache_path:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(
            json.dumps({str(gp): _encode(kw) for gp, kw in resolved.items()}) + "\n",
            encoding="utf-8",
        )
    return resolved


def run_production(
    bundle_dir: str, seeds: int, game_pks: tuple[int, ...], kwargs_cache: str | None
) -> dict[str, str]:
    from simulation import production_factory

    flags = compose_app_flags()
    os.environ[production_factory.PINNED_ARTIFACT_DIR_ENV] = bundle_dir
    production_factory.reset_caches()
    with sim_flags(flags):
        kwargs_by_game = production_kwargs(game_pks, kwargs_cache)
        out: dict[str, str] = {}
        for gp in game_pks:
            out.update(
                _run_games(f"game{gp}", PRODUCTION_FACTORY, kwargs_by_game[gp], range(seeds))
            )
            print(f"  game {gp}: {seeds} seeds", flush=True)
    return out


# ---------------------------------------------------------------------------
# A built bundle, by content
# ---------------------------------------------------------------------------


def run_bundle(bundle_dir: str) -> dict[str, str]:
    """Hash every file of a bundle by content.

    An ``.npy`` file hashes its array (dtype, shape and bytes), an ``.npz`` file
    each member array, a JSON file its parsed value, and any other file its
    bytes. A build-time stamp in a JSON document reads as a difference; the
    compare report names the file, so a reader can tell a stamp from a change.
    """
    root = Path(bundle_dir)
    out: dict[str, str] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if path.suffix == ".npy":
            out[rel] = digest(np.load(path, allow_pickle=False))
        elif path.suffix == ".npz":
            with np.load(path, allow_pickle=False) as npz:
                for name in sorted(npz.files):
                    out[f"{rel}:{name}"] = digest(npz[name])
        elif path.suffix == ".json":
            out[rel] = digest(json.loads(path.read_text(encoding="utf-8")))
        else:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# The record and the comparison
# ---------------------------------------------------------------------------


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _environment() -> dict[str, str]:
    """The interpreter and numpy versions. Compare records from one environment only."""
    return {"python": sys.version.split()[0], "numpy": np.__version__}


def write_record(path: str, mode: str, hashes: dict[str, str], elapsed: float) -> None:
    record = {
        "mode": mode,
        "environment": _environment(),
        "git_head": _git_head(),
        "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(elapsed, 1),
        "count": len(hashes),
        "combined": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode("utf-8")).hexdigest(),
        "hashes": hashes,
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def compare(path_a: str, path_b: str) -> int:
    a = json.loads(Path(path_a).read_text(encoding="utf-8"))
    b = json.loads(Path(path_b).read_text(encoding="utf-8"))
    ha, hb = a["hashes"], b["hashes"]
    only_a = sorted(set(ha) - set(hb))
    only_b = sorted(set(hb) - set(ha))
    differ = sorted(k for k in set(ha) & set(hb) if ha[k] != hb[k])
    print(f"A: {path_a} ({a.get('mode')}, {a.get('git_head', '?')[:10]}, {len(ha)} hashes)")
    print(f"B: {path_b} ({b.get('mode')}, {b.get('git_head', '?')[:10]}, {len(hb)} hashes)")
    if a.get("environment") != b.get("environment"):
        print(
            f"WARNING: the records come from two environments ({a.get('environment')} vs "
            f"{b.get('environment')}); a float can differ for that reason alone."
        )
    for label, keys in (("only in A", only_a), ("only in B", only_b), ("differ", differ)):
        if keys:
            print(f"{label}: {len(keys)}")
            for key in keys[:20]:
                print(f"  {key}")
    if only_a or only_b or differ:
        print("FINGERPRINT DIFFERS")
        return 1
    print(f"IDENTICAL: {len(ha)} hashes")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=["nodb", "synthetic", "production", "bundle", "compare"])
    ap.add_argument("records", nargs="*", help="compare: the two record files")
    ap.add_argument("--out", help="the record file to write")
    ap.add_argument("--games", type=int, default=100, help="nodb / synthetic: seeds per variant")
    ap.add_argument("--seeds", type=int, default=4, help="production: seeds per game")
    ap.add_argument("--game-pks", type=int, nargs="*", help="production: games (default: the 45)")
    ap.add_argument("--bundle-dir", help="production / bundle: the bundle folder")
    ap.add_argument(
        "--kwargs-cache",
        help="production: the games' resolved inputs; written on the first run, read after",
    )
    args = ap.parse_args(argv)

    if args.mode == "compare":
        if len(args.records) != 2:
            ap.error("compare needs two record files")
        return compare(*args.records)
    if not args.out:
        ap.error("--out is required")
    runners: dict[str, Callable[[], dict[str, str]]] = {
        "nodb": lambda: run_nodb(args.games),
        "synthetic": lambda: run_synthetic(args.games),
        "production": lambda: run_production(
            _required(ap, args.bundle_dir, "--bundle-dir"),
            args.seeds,
            tuple(args.game_pks) if args.game_pks else balanced_game_pks(),
            args.kwargs_cache,
        ),
        "bundle": lambda: run_bundle(_required(ap, args.bundle_dir, "--bundle-dir")),
    }
    started = time.perf_counter()
    hashes = runners[args.mode]()
    elapsed = time.perf_counter() - started
    write_record(args.out, args.mode, hashes, elapsed)
    print(f"wrote {args.out}: {len(hashes)} hashes in {elapsed:.1f}s")
    return 0


def _required(ap: argparse.ArgumentParser, value: str | None, name: str) -> str:
    if not value:
        ap.error(f"{name} is required for this mode")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
