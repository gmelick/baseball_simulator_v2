"""
SimilarityEngineRegistry (SIM-048)
==================================

A single, unified place to discover and construct any of the project's 11
similarity engines by a canonical name, together with per-engine metadata.

Why a registry
--------------
The 11 engines live in ``similarity/engines/*.py`` and each exposes a main
engine class with a slightly different API. Callers that want to operate over
engines generically (orchestration, diagnostics, calibration, CLIs) should not
have to memorize 11 module paths and class names. The registry gives them:

* a canonical name per engine (``list_engines()``),
* class resolution (``get_class(name)``) and construction (``create(name, ...)``),
* metadata via :class:`EngineSpec` — ``family`` and ``score_type`` in
  particular encode the project's score-discipline contract.

Score-discipline contract
--------------------------
The RBF and GMM families emit bounded **similarity** scores in ``[0, 1]``.
The geometric KDTree and FAISS families emit **distance** values (lower means
more similar, unbounded above). The registry surfaces this as
``EngineSpec.score_type`` ("similarity" | "distance") so generic consumers can
treat scores correctly without inspecting engine internals.

Optional dependency guarding
----------------------------
The FAISS engines (pitch-to-pitch, batted-ball) require ``faiss`` and the
pitcher GMM-W2 engine optionally uses POT (``ot``). To keep the registry
importable even when those optional deps are missing, engine classes are
imported **lazily**: each spec stores a module path + class name and the class
is imported on demand inside :meth:`get_class`. Importing this module therefore
never imports faiss/ot — exactly mirroring how the engine modules themselves
guard ``_FAISS_AVAILABLE`` / ``_POT_AVAILABLE``. A missing optional dep only
surfaces when you actually resolve/create that specific engine.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Literal

__all__ = ["EngineSpec", "SimilarityEngineRegistry"]

Family = Literal["rbf", "gmm", "kdtree", "faiss"]
ScoreType = Literal["similarity", "distance"]


@dataclass(frozen=True)
class EngineSpec:
    """Immutable metadata describing a single similarity engine.

    Attributes
    ----------
    name:
        Canonical registry name (stable identifier used by callers).
    module:
        Dotted import path of the module that defines ``class_name``.
    class_name:
        Name of the main engine class within ``module``.
    family:
        Algorithm family — one of ``"rbf"``, ``"gmm"``, ``"kdtree"``,
        ``"faiss"``.
    score_type:
        ``"similarity"`` for the bounded ``[0, 1]`` RBF/GMM engines,
        ``"distance"`` for the geometric KDTree/FAISS engines.
    description:
        One-line human-readable summary.
    """

    name: str
    module: str
    class_name: str
    family: Family
    score_type: ScoreType
    description: str

    @property
    def engine_class(self) -> type:
        """Resolve and return the engine class (lazy import).

        Importing the underlying module may raise if an optional dependency
        (faiss/ot) is unavailable — that is intentional and only happens when
        this specific engine is resolved, never at registry import time.
        """
        mod = importlib.import_module(self.module)
        return getattr(mod, self.class_name)


# ---------------------------------------------------------------------------
# Canonical engine catalogue
# ---------------------------------------------------------------------------
# Ordering is the conventional pipeline order; ``list_engines()`` preserves it.
_SPECS: tuple[EngineSpec, ...] = (
    EngineSpec(
        name="pitcher",
        module="similarity.engines.pitcher_similarity",
        class_name="PitcherSimilarityEngine",
        family="gmm",
        score_type="similarity",
        description="Pitcher-to-pitcher similarity via GMM arsenal Wasserstein-2 "
        "distance fused with RBF command similarity.",
    ),
    EngineSpec(
        name="batter",
        module="similarity.engines.batter_similarity",
        class_name="BatterSimilarityEngine",
        family="rbf",
        score_type="similarity",
        description="Batter-to-batter platoon-aware weighted RBF similarity over "
        "discipline, batted-ball, and power profiles.",
    ),
    EngineSpec(
        name="fielder",
        module="similarity.engines.fielder_similarity",
        class_name="FielderSimilarityEngine",
        family="rbf",
        score_type="similarity",
        description="Position-partitioned fielder-to-fielder weighted RBF similarity.",
    ),
    EngineSpec(
        name="baserunner",
        module="similarity.engines.baserunner_similarity",
        class_name="BaserunnerSimilarityEngine",
        family="rbf",
        score_type="similarity",
        description="Baserunner extra-base advancement weighted RBF similarity.",
    ),
    EngineSpec(
        name="baserunner_steal",
        module="similarity.engines.baserunner_steal_similarity",
        class_name="BaserunnerStealSimilarityEngine",
        family="rbf",
        score_type="similarity",
        description="Baserunner stolen-base tendency weighted RBF similarity.",
    ),
    EngineSpec(
        name="catcher",
        module="similarity.engines.catcher_similarity",
        class_name="CatcherSimilarityEngine",
        family="rbf",
        score_type="similarity",
        description="Catcher defense (framing/blocking/throwing) weighted RBF similarity.",
    ),
    EngineSpec(
        name="pitcher_steal",
        module="similarity.engines.pitcher_steal_similarity",
        class_name="PitcherStealSimilarityEngine",
        family="rbf",
        score_type="similarity",
        description="Pitcher stolen-base control (hold/pickoff) weighted RBF similarity.",
    ),
    EngineSpec(
        name="manager",
        module="similarity.engines.manager_similarity",
        class_name="ManagerSimilarityEngine",
        family="rbf",
        score_type="similarity",
        description="Manager in-game tendency weighted RBF similarity.",
    ),
    EngineSpec(
        name="situation",
        module="similarity.engines.situation_similarity",
        class_name="SituationSimilarityEngine",
        family="kdtree",
        score_type="distance",
        description="Game-state situation nearest-neighbour search via KD-tree "
        "(geometric distance).",
    ),
    EngineSpec(
        name="pitch_to_pitch",
        module="similarity.engines.pitch_pitch_similarity",
        class_name="PitchPitchSimilarityEngine",
        family="faiss",
        score_type="distance",
        description="Pitch-to-pitch nearest-neighbour search via FAISS (geometric L2 distance).",
    ),
    EngineSpec(
        name="batted_ball",
        module="similarity.engines.batted_ball_similarity",
        class_name="BattedBallSimilarityEngine",
        family="faiss",
        score_type="distance",
        description="Batted-ball nearest-neighbour search via FAISS (geometric L2 distance).",
    ),
)

_SPEC_BY_NAME: dict[str, EngineSpec] = {spec.name: spec for spec in _SPECS}


class SimilarityEngineRegistry:
    """Unified discovery/construction interface over all 11 similarity engines.

    All methods are class methods — the registry is a stateless namespace over
    the static :data:`_SPECS` catalogue. Engine classes are imported lazily, so
    importing this module never pulls in optional deps (faiss/ot).

    Examples
    --------
    >>> SimilarityEngineRegistry.list_engines()  # doctest: +ELLIPSIS
    ['pitcher', 'batter', ...]
    >>> spec = SimilarityEngineRegistry.get_spec("batter")
    >>> spec.family, spec.score_type
    ('rbf', 'similarity')
    >>> cls = SimilarityEngineRegistry.get_class("batter")
    >>> engine = SimilarityEngineRegistry.create("batter", duckdb_path=":memory:")
    """

    @classmethod
    def list_engines(cls) -> list[str]:
        """Return all 11 canonical engine names in conventional order."""
        return [spec.name for spec in _SPECS]

    @classmethod
    def get_spec(cls, name: str) -> EngineSpec:
        """Return the :class:`EngineSpec` metadata for ``name``.

        Raises
        ------
        KeyError
            If ``name`` is not a registered engine. The message lists the
            valid canonical names.
        """
        try:
            return _SPEC_BY_NAME[name]
        except KeyError:
            raise KeyError(
                f"unknown similarity engine: {name!r}. Valid names: {sorted(_SPEC_BY_NAME)}"
            ) from None

    @classmethod
    def get_class(cls, name: str) -> type:
        """Resolve ``name`` to its engine class (lazy import).

        Raises
        ------
        KeyError
            If ``name`` is unknown.
        ImportError / RuntimeError
            If the engine's optional dependency (faiss/ot) is unavailable.
            This only fires for the specific engine being resolved.
        """
        return cls.get_spec(name).engine_class

    @classmethod
    def create(cls, name: str, **kwargs: object) -> object:
        """Instantiate the engine ``name``, passing ``**kwargs`` to its
        constructor.

        Most engines require a ``duckdb_path`` argument; construction itself
        does not touch the database (the DB is only read during ``build()``),
        so ``duckdb_path=":memory:"`` is sufficient to construct an engine.

        Raises
        ------
        KeyError
            If ``name`` is unknown.
        """
        engine_cls = cls.get_class(name)
        return engine_cls(**kwargs)


# Convenience module-level aliases mirroring the class API.
list_engines = SimilarityEngineRegistry.list_engines
get_spec = SimilarityEngineRegistry.get_spec
get_class = SimilarityEngineRegistry.get_class
create = SimilarityEngineRegistry.create
