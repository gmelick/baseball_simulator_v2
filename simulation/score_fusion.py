"""
score_fusion.py
===============
SIM-321 -- Cross-engine score fusion (the per-pitch shaping signal)
MLB Baseball Simulation Platform

Design: ``docs/architecture/2026-06-24-cross-engine-fusion.md``.

WHAT THIS IS
------------
The project has 11 similarity engines (``similarity/registry.py``) that answer
"how like X is candidate Y?" in two incompatible currencies:

  * RBF / GMM engines emit a bounded **similarity in [0, 1]** (1 = identical);
  * KDTree / FAISS engines emit a **distance** (>= 0, lower = more similar).

Nothing today combines them into ONE per-pitch decision.  This module is that
combiner: it takes per-engine signals, makes them comparable, and blends them
into a single ``[0, 1]`` *shaping scalar* per candidate that feeds SIM-317
(fingerprint derivation) and SIM-318 (outcome determination).

THE HARD BOUNDARY (locked architecture constraint)
--------------------------------------------------
The **sampler** (``simulation/play_pool_sampler.py``) is the ONE and ONLY place
a distance becomes a sampling weight (the reciprocal-distance transform
``w = 1 over (d + EPS)`` then normalized).  This module MUST NOT cross that
boundary.  Therefore fusion:

  * NEVER applies the reciprocal-distance weight transform and NEVER returns a
    normalized probability vector (no ``Sigma = 1`` across candidates) -- that
    is the sampler's job;
  * MAY map a distance to a bounded, monotone-decreasing ``[0, 1]`` *affinity*
    via ``exp(-distance / scale)``.  This is a *comparability* transform (it
    preserves distance ordering and is per-candidate, never normalized across
    candidates), not a *weighting* transform.  It reuses the exact exp-decay
    family the similarity engines already use internally (e.g. the pitcher
    engine maps its W2 arsenal distance to a score via ``exp(-W2 / scale)`` --
    see ``pitcher_similarity.ArsenalSimilarity.score``).

Fusion shapes which neighbourhood the draw should favour; the sampler still does
the FAISS k-NN and still owns distance->weight.  See the design doc sections 1
and 3 for the full statement.

PUBLIC API
----------
  * :func:`fuse_scores` -- one-shot functional entry point.
  * :class:`ScoreFusion` -- reusable object holding a profile (weights + rule).
  * :class:`FusionResult` -- the structured output (fused scalar + diagnostics).
  * :func:`distance_to_affinity` -- the comparability transform (NOT a weight).

Engines stay pure: this module accepts *injected* per-engine outputs (a number,
or an engine's result object) and a ``score_type`` per engine -- it never builds
or queries an engine and never needs a DB to unit-test.

CURRENTLY UNWIRED IN PRODUCTION (status note)
---------------------------------------------
This whole module is **dead-wired on the production path**.  ``ScoreFusion.fuse()``
is reached only through ``FingerprintDeriver``'s fusion-tilt branches, and those
never fire in production: every fingerprint call site in ``simulation/sim_loop.py``
calls the deriver with NO ``pitch_signals`` / ``battedball_signals``, and the
full-pool production path (``SIM_FULL_POOL=1``) sets ``deriver=None`` outright
(``simulation/production_factory.py`` line ~561).  So fusion runs only on the
per-tile FAISS fallback / unit-test path -- and even there only the tests inject
signals.  Kept (not deleted) pending an ML-owner decision to either thread fusion
signals into the deriver or retire the centroid-tilt approach now that full-pool
scoring is the production default.  Mirrors the note in ``simulation/fingerprints.py``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "ScoreType",
    "EngineSignal",
    "FusionResult",
    "ScoreFusion",
    "fuse_scores",
    "distance_to_affinity",
    "PITCH_DRAW_WEIGHTS",
    "BATTED_BALL_WEIGHTS",
    "FIELDING_WEIGHTS",
    "PROFILES",
    "DEFAULT_DISTANCE_SCALE",
    "GEOMETRIC_FLOOR",
]

# A string score-type tag, matching ``similarity.registry.EngineSpec.score_type``.
ScoreType = str  # "similarity" | "distance"

_SIMILARITY = "similarity"
_DISTANCE = "distance"

# ---------------------------------------------------------------------------
# Constants (documented in the design doc 4.3 / 3)
# ---------------------------------------------------------------------------

#: Default exp-decay scale for distance -> affinity (design doc 3).  The
#: situation engine's normalized + sqrt-weighted Euclidean distances sit in an
#: O(1) range, so a unit scale gives useful spread.  This is a calibration knob,
#: NOT a weighting step -- it never makes fusion own the distance->weight role.
DEFAULT_DISTANCE_SCALE = 1.0

#: Lower clamp for the geometric-mean log so ``log(affinity)`` stays finite when
#: an affinity is 0 (design doc 4.1).  Tiny enough that a near-zero affinity
#: still drags the product toward zero (AND-semantics) without producing -inf.
GEOMETRIC_FLOOR = 1e-6

#: Per-engine weights for the default per-pitch *pitch draw* (design doc 4.3).
#: ``pitcher`` dominates: arsenal geometry supplies the fingerprint centroid
#: (spec section 4.1) and is the per-pitch cost driver (perf budget).
PITCH_DRAW_WEIGHTS: dict[str, float] = {
    "pitcher": 0.50,
    "batter": 0.30,
    "situation": 0.20,
}

#: Weights for the on-contact batted-ball draw (spec step 5): the batter's
#: contact profile leads, situation + pitcher tilt it.
BATTED_BALL_WEIGHTS: dict[str, float] = {
    "batter": 0.55,
    "situation": 0.25,
    "pitcher": 0.20,
}

#: Weights for the resolution-only fielding step (spec step 6): NOT part of the
#: per-pitch draw, reused via a named profile (design doc 2.2 / 4.3).
FIELDING_WEIGHTS: dict[str, float] = {
    "fielder": 0.70,
    "catcher": 0.30,
}

#: Named profiles bundling {weights + rule}.  ``pitch_draw`` is the default.
PROFILES: dict[str, dict] = {
    "pitch_draw": {"weights": PITCH_DRAW_WEIGHTS, "rule": "geometric"},
    "batted_ball": {"weights": BATTED_BALL_WEIGHTS, "rule": "geometric"},
    "fielding": {"weights": FIELDING_WEIGHTS, "rule": "linear"},
}

_VALID_RULES = ("geometric", "linear")


# ---------------------------------------------------------------------------
# score_type resolution (respects the registry contract)
# ---------------------------------------------------------------------------


def _registry_score_type(engine_name: str) -> ScoreType | None:
    """Resolve ``engine_name``'s ``score_type`` via the registry, if available.

    The registry import is lazy + guarded so this module unit-tests without
    importing any engine (and without optional deps).  Returns ``None`` when the
    registry or the name is unavailable, leaving the caller to fall back to an
    explicit per-signal tag.
    """
    try:  # pragma: no cover - thin import shim
        from similarity.registry import SimilarityEngineRegistry

        return SimilarityEngineRegistry.get_spec(engine_name).score_type
    except Exception:  # pragma: no cover - registry absent / unknown name
        return None


def _coerce_score(raw: object) -> float:
    """Pull a numeric score out of a raw engine output.

    Accepts a plain number, or an object exposing a ``score`` / ``distance``
    attribute (the engines' result dataclasses -- ``SimilarityResult.score``,
    ``NearestSituation.distance``).  ``None`` -> NaN (treated as missing).
    """
    if raw is None:
        return float("nan")
    if isinstance(raw, int | float):
        return float(raw)
    # Prefer an explicit similarity ``score``; else a ``distance``.
    for attr in ("score", "distance"):
        if hasattr(raw, attr):
            val = getattr(raw, attr)
            if val is not None:
                return float(val)
    raise TypeError(
        f"cannot coerce engine output {raw!r} to a score: pass a number or an "
        "object with a .score / .distance attribute."
    )


# ---------------------------------------------------------------------------
# The comparability transform (NOT a sampling weight -- design doc 1 / 3)
# ---------------------------------------------------------------------------


def distance_to_affinity(distance: float, scale: float = DEFAULT_DISTANCE_SCALE) -> float:
    """Map a non-negative *distance* to a bounded ``[0, 1]`` **affinity**.

        affinity = exp(-distance / scale)

    This is the cross-engine comparability transform (design doc 3).  It is
    deliberately NOT the sampler's distance->weight conversion:

      * it is **monotone decreasing** in distance (ordering preserved);
      * it is **per-candidate** -- it never normalizes across candidates, so it
        is not a probability and cannot be mistaken for the sampler's normalized
        weight vector (the distance->weight boundary, design doc 1);
      * it reuses the engines' own exp-decay-of-distance family (e.g.
        ``pitcher_similarity.ArsenalSimilarity.score`` = ``exp(-W2 / scale)``).

    Degenerate inputs: ``+inf`` distance -> ``0.0``; ``NaN`` -> ``NaN`` (the
    caller treats NaN as a missing engine and drops it from the blend).  A
    negative distance is clamped to ``0`` (affinity ``1.0``) defensively.
    """
    if scale <= 0:
        raise ValueError(f"scale must be > 0; got {scale!r}")
    if math.isnan(distance):
        return float("nan")
    if math.isinf(distance):
        return 0.0
    d = max(0.0, float(distance))
    return math.exp(-d / scale)


def _to_affinity(raw: object, score_type: ScoreType, scale: float) -> float:
    """Reduce one engine's raw output to a comparable ``[0, 1]`` affinity,
    honouring its ``score_type`` (design doc 3).

    similarity -> clip(score, 0, 1) unchanged (already comparable);
    distance   -> ``distance_to_affinity`` (exp-decay comparability transform).
    """
    score = _coerce_score(raw)
    if math.isnan(score):
        return float("nan")  # missing -> dropped by the blend
    if score_type == _SIMILARITY:
        # Already in the comparable [0,1] space; clamp defensively.
        return min(1.0, max(0.0, score))
    if score_type == _DISTANCE:
        return distance_to_affinity(score, scale)
    raise ValueError(f"unknown score_type {score_type!r} (expected 'similarity' | 'distance')")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineSignal:
    """One engine's contribution to a fusion, before blending.

    ``raw`` is the engine's output (a number, or a result object with
    ``.score`` / ``.distance``).  ``score_type`` is "similarity" or "distance";
    if ``None``, it is resolved from the registry by ``name`` (design doc 3).
    ``scale`` overrides the per-engine exp-decay scale for distance signals.
    """

    name: str
    raw: object
    score_type: ScoreType | None = None
    scale: float = DEFAULT_DISTANCE_SCALE

    def resolved_score_type(self) -> ScoreType:
        if self.score_type is not None:
            return self.score_type
        st = _registry_score_type(self.name)
        if st is None:
            raise ValueError(
                f"score_type for engine {self.name!r} could not be resolved from "
                "the registry; pass score_type= explicitly."
            )
        return st

    def affinity(self) -> float:
        """This signal's comparable ``[0, 1]`` affinity (NaN if missing)."""
        return _to_affinity(self.raw, self.resolved_score_type(), self.scale)


@dataclass(frozen=True)
class FusionResult:
    """The structured output of a fusion (design doc 5).

    ``fused`` is the single ``[0, 1]`` shaping scalar SIM-317 / SIM-318 consume.
    ``affinities`` / ``weights`` are diagnostics (per-engine comparable affinity
    and the renormalized weight actually used after any missing-engine
    redistribution).  ``rule`` / ``profile`` are provenance.
    """

    fused: float
    affinities: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    rule: str = "geometric"
    profile: str | None = None


# ---------------------------------------------------------------------------
# Core blend
# ---------------------------------------------------------------------------


def _blend(
    affinities: Mapping[str, float],
    weights: Mapping[str, float],
    rule: str,
) -> tuple:
    """Blend per-engine ``affinities`` with ``weights`` under ``rule``.

    * Missing engines (NaN affinity) are dropped and their weight redistributed
      across the present engines (renormalized) -- mirrors the pitcher engine's
      "redistribute the missing sub-score's weight" behaviour.
    * Returns ``(fused, used_weights)`` where ``used_weights`` are the
      renormalized weights actually applied (sums to 1 over present engines).
    * The blend is a symmetric function of its (weight, affinity) pairs, so it
      is **order-invariant** (design doc 4.1).
    """
    if rule not in _VALID_RULES:
        raise ValueError(f"unknown rule {rule!r} (expected one of {_VALID_RULES})")

    # Keep only present engines (finite affinity) with a positive weight.
    present = []
    for name, a in affinities.items():
        w = float(weights.get(name, 0.0))
        if w <= 0.0:
            continue
        if a is None or math.isnan(a):
            continue  # missing engine -> drop, weight redistributed below
        present.append((name, a, w))

    if not present:
        # Degenerate: nothing usable -> neutral shaping (no tilt).
        return 0.0, {}

    total_w = sum(w for _, _, w in present)
    used_weights = {name: w / total_w for name, _, w in present}

    if rule == "linear":
        fused = sum(used_weights[name] * a for name, a, _ in present)
    else:  # geometric (weighted geometric mean via log-sum)
        log_sum = 0.0
        for name, a, _ in present:
            log_sum += used_weights[name] * math.log(max(a, GEOMETRIC_FLOOR))
        fused = math.exp(log_sum)

    # Bounded [0,1] (both rules of [0,1] inputs are), clamp float drift.
    fused = min(1.0, max(0.0, fused))
    return fused, used_weights


# ---------------------------------------------------------------------------
# ScoreFusion -- reusable object holding a profile
# ---------------------------------------------------------------------------


class ScoreFusion:
    """Reusable cross-engine fuser bound to a profile (weights + rule).

    Engines stay pure: construct with a profile name (or explicit weights/rule)
    and call :meth:`fuse` with injected per-engine signals.  No DB, no engine
    construction.

    NOTE (currently unwired in production): :meth:`fuse` never runs on the
    production path -- see the module docstring's "CURRENTLY UNWIRED IN
    PRODUCTION" note.  It is exercised only by the per-tile fallback / unit-test
    path (and only the tests inject signals).

    Examples
    --------
    >>> fz = ScoreFusion(profile="pitch_draw")
    >>> res = fz.fuse({
    ...     "pitcher":   (0.80, "similarity"),
    ...     "batter":    (0.60, "similarity"),
    ...     "situation": (0.50, "distance"),
    ... })
    >>> 0.0 <= res.fused <= 1.0
    True
    """

    def __init__(
        self,
        profile: str | None = "pitch_draw",
        *,
        weights: Mapping[str, float] | None = None,
        rule: str | None = None,
        distance_scale: float = DEFAULT_DISTANCE_SCALE,
    ) -> None:
        if weights is not None:
            self.weights: dict[str, float] = dict(weights)
            self.rule = rule or "geometric"
            self.profile = profile
        elif profile is not None:
            if profile not in PROFILES:
                raise KeyError(f"unknown profile {profile!r}; known: {sorted(PROFILES)}")
            spec = PROFILES[profile]
            self.weights = dict(spec["weights"])
            self.rule = rule or spec["rule"]
            self.profile = profile
        else:
            raise ValueError("provide a profile name or explicit weights.")

        if self.rule not in _VALID_RULES:
            raise ValueError(f"unknown rule {self.rule!r}")
        if distance_scale <= 0:
            raise ValueError("distance_scale must be > 0")
        self.distance_scale = float(distance_scale)

    def _normalize_signals(self, signals) -> list:
        """Coerce the various accepted signal shapes into ``EngineSignal``s.

        Accepts:
          * ``Iterable[EngineSignal]``;
          * ``Mapping[name -> raw]`` (score_type resolved via registry);
          * ``Mapping[name -> (raw, score_type)]`` or ``(raw, score_type, scale)``.
        """
        out = []
        if isinstance(signals, Mapping):
            for name, val in signals.items():
                if isinstance(val, EngineSignal):
                    out.append(val)
                elif isinstance(val, tuple):
                    raw = val[0]
                    st = val[1] if len(val) > 1 else None
                    scale = val[2] if len(val) > 2 else self.distance_scale
                    out.append(EngineSignal(name, raw, st, scale))
                else:
                    out.append(EngineSignal(name, val, None, self.distance_scale))
        else:
            for s in signals:
                if not isinstance(s, EngineSignal):
                    raise TypeError(
                        f"iterable signals must be EngineSignal instances; got {type(s).__name__}"
                    )
                out.append(s)
        return out

    def fuse(self, signals) -> FusionResult:
        """Fuse injected per-engine ``signals`` into a :class:`FusionResult`.

        Only engines that appear in BOTH the signals and this profile's weights
        contribute; an engine with no configured weight is ignored (its signal
        is diagnostic-only).
        """
        sigs = self._normalize_signals(signals)
        affinities = {s.name: s.affinity() for s in sigs}
        fused, used_weights = _blend(affinities, self.weights, self.rule)
        return FusionResult(
            fused=fused,
            affinities=affinities,
            weights=used_weights,
            rule=self.rule,
            profile=self.profile,
        )


# ---------------------------------------------------------------------------
# Functional entry point
# ---------------------------------------------------------------------------


def fuse_scores(
    signals,
    *,
    profile: str | None = "pitch_draw",
    weights: Mapping[str, float] | None = None,
    rule: str | None = None,
    distance_scale: float = DEFAULT_DISTANCE_SCALE,
) -> FusionResult:
    """One-shot cross-engine fusion (design doc 4 / 5).

    ``signals`` may be a mapping ``{name: raw}`` / ``{name: (raw, score_type)}``
    or an iterable of :class:`EngineSignal`.  See :class:`ScoreFusion`.

    Returns a :class:`FusionResult` whose ``fused`` is the ``[0, 1]`` shaping
    scalar for the candidate -- a sampler-independent signal that lives on the
    correct side of the distance->weight boundary (design doc 1).
    """
    fuser = ScoreFusion(
        profile=profile,
        weights=weights,
        rule=rule,
        distance_scale=distance_scale,
    )
    return fuser.fuse(signals)
