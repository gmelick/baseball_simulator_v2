"""The calibration report: every fitted engine constant, its text summary and
its lossless JSON form.

The app loads the JSON at boot (``/data/calibration.json``) and each engine's
``apply_calibration`` reads its own fields. A field left at its default (0.0
or None) keeps the engine's module default.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field, fields
from typing import Any

import numpy as np
from numpy.typing import NDArray

# This top-level import also runs the fielder engine's import-time
# ``logging.basicConfig``, so importing the report configures the root logger.
from similarity.engines.fielder_similarity import IF_PIVOT_FEATURES  # noqa: F401

#: The reliability-weight blocks the summary prints, in order: (the label, the
#: report field, the engine module under ``similarity.engines``, the feature
#: list the weights follow). A module is imported only when its field is set.
_RELIABILITY_BLOCKS: tuple[tuple[str, str, str, str], ...] = (
    ("discipline", "reliability_weights_discipline", "batter_similarity", "DISCIPLINE_FEATURES"),
    (
        "batted ball",
        "reliability_weights_batted_ball",
        "batter_similarity",
        "BATTED_BALL_FEATURES",
    ),
    ("power", "reliability_weights_power", "batter_similarity", "POWER_FEATURES"),
    ("platoon", "reliability_weights_platoon", "batter_similarity", "PLATOON_FEATURES"),
    ("IF Range", "reliability_weights_if_range", "fielder_similarity", "IF_RANGE_FEATURES"),
    ("IF DP", "reliability_weights_if_dp", "fielder_similarity", "IF_DP_FEATURES"),
    ("IF Pivot", "reliability_weights_if_pivot", "fielder_similarity", "IF_PIVOT_FEATURES"),
    ("IF Error", "reliability_weights_if_error", "fielder_similarity", "IF_ERROR_FEATURES"),
    (
        "IF Specialty",
        "reliability_weights_if_specialty",
        "fielder_similarity",
        "IF_SPECIALTY_FEATURES",
    ),
    ("OF Range", "reliability_weights_of_range", "fielder_similarity", "OF_RANGE_FEATURES"),
    ("OF Arm", "reliability_weights_of_arm", "fielder_similarity", "OF_ARM_FEATURES"),
    ("OF Star", "reliability_weights_of_star", "fielder_similarity", "OF_STAR_FEATURES"),
    ("OF Error", "reliability_weights_of_error", "fielder_similarity", "OF_ERROR_FEATURES"),
    (
        "Baserunner Speed",
        "reliability_weights_baserunner_speed",
        "baserunner_similarity",
        "SPEED_FEATURES",
    ),
    (
        "Baserunner Aggression",
        "reliability_weights_baserunner_aggression",
        "baserunner_similarity",
        "AGGRESSION_FEATURES",
    ),
    (
        "Baserunner Success",
        "reliability_weights_baserunner_success",
        "baserunner_similarity",
        "SUCCESS_FEATURES",
    ),
)


@dataclass
class CalibrationReport:
    """Every calibrated engine constant, with the context it was fitted in."""

    # Batter and pitcher kernels.
    sigma_discipline: float = 0.0
    sigma_batted_ball: float = 0.0
    sigma_platoon: float = 0.0
    sigma_power: float = 0.0
    sigma_physical: float = 0.0  # batter swing and stance (SIM-529)
    sigma_command: float = 0.0  # pitcher
    sigma_results: float = 0.0  # pitcher; never fitted (the engine has no results sub-score)
    arsenal_gamma: float = 0.0
    # The median finite W₂ the arsenal_gamma anchor was solved on. The engine
    # converts gamma to its linear ARSENAL_SCALE exactly with it:
    # scale = 1 / (gamma * median) (the arsenal calibration, SIM-346).
    arsenal_median_w2: float = 0.0
    eb_n_prior_batter: float = 0.0
    eb_n_prior_pitcher: float = 0.0
    reliability_weights_discipline: NDArray | None = None
    reliability_weights_batted_ball: NDArray | None = None
    reliability_weights_power: NDArray | None = None
    reliability_weights_platoon: NDArray | None = None
    reliability_weights_physical: NDArray | None = None
    # Fielder and baserunner kernels.
    sigma_if_range: float = 0.0
    sigma_if_dp: float = 0.0
    sigma_if_errors: float = 0.0
    sigma_if_specialty: float = 0.0
    sigma_of_range: float = 0.0
    sigma_of_arm: float = 0.0
    sigma_of_stars: float = 0.0
    sigma_of_errors: float = 0.0
    sigma_baserunner_speed: float = 0.0
    sigma_baserunner_aggression: float = 0.0
    sigma_baserunner_success: float = 0.0
    eb_n_prior_baserunner: float = 0.0
    reliability_weights_if_range: NDArray | None = None
    reliability_weights_if_dp: NDArray | None = None
    reliability_weights_if_pivot: NDArray | None = None
    reliability_weights_if_error: NDArray | None = None
    reliability_weights_if_specialty: NDArray | None = None
    reliability_weights_of_range: NDArray | None = None
    reliability_weights_of_arm: NDArray | None = None
    reliability_weights_of_star: NDArray | None = None
    reliability_weights_of_error: NDArray | None = None
    reliability_weights_baserunner_speed: NDArray | None = None
    reliability_weights_baserunner_aggression: NDArray | None = None
    reliability_weights_baserunner_success: NDArray | None = None

    # The catcher, steal and manager engines: only the sigmas are fitted;
    # their reliability weights stay the engines' stabilization-research
    # priors. A sigma stays 0.0 when the engine's metrics table is absent.
    sigma_catcher_framing: float = 0.0
    sigma_catcher_blocking: float = 0.0
    sigma_catcher_throwing: float = 0.0
    sigma_catcher_deterrence: float = 0.0
    sigma_baserunner_steal_tendency: float = 0.0
    sigma_baserunner_steal_success: float = 0.0
    sigma_pitcher_steal_outcome: float = 0.0
    # The lead groups: the runner's lead and jump, and the lead and jump the
    # pitcher allows, fitted over the rows that carry a measurement (SIM-531).
    sigma_baserunner_steal_lead: float = 0.0
    sigma_pitcher_steal_hold: float = 0.0
    sigma_manager_usage: float = 0.0
    sigma_manager_aggression: float = 0.0
    sigma_manager_platoon: float = 0.0

    # Never filled: no fit writes these three. They stay round-trippable
    # fields for a future sub-score weight fit and bats-penalty measurement.
    batter_subscores: NDArray | None = None
    pitcher_subscores: NDArray | None = None
    bats_penalty_lr: float = 0.0

    # The win-probability reliability curve: ``(predicted_p, observed_p)``
    # anchor points, sorted by predicted_p, each in [0, 1].
    # ``simulation.win_probability.CalibrationMap.from_report`` turns them into
    # a monotone piecewise-linear map. None or empty keeps the identity map.
    # ``simulation.prop_validation.write_reliability_curve_to_calibration_report``
    # writes it (``scripts/validate_props.py --write-calibration``).
    reliability_curve: list | None = None

    # Metadata
    n_profiles_used: int = 0
    seasons_used: list = field(default_factory=list)
    target_median_score: float = 0.50
    calibration_time_sec: float = 0.0

    def summary(self) -> str:
        """The report as text, for a person to read."""
        lines = [
            "=" * 70,
            "SIMILARITY ENGINE CALIBRATION REPORT",
            "=" * 70,
            f"Profiles used: {self.n_profiles_used}",
            f"Seasons: {self.seasons_used}",
            f"Target median score: {self.target_median_score}",
            f"Calibration time: {self.calibration_time_sec:.1f}s",
            "",
            "--- Tier 1: Population-Derived Parameters ---",
            "",
            "  RBF Sigma (batter):",
            f"    Discipline:  {self.sigma_discipline:.4f}",
            f"    Batted Ball: {self.sigma_batted_ball:.4f}",
            f"    Platoon:     {self.sigma_platoon:.4f}",
            f"    Power:       {self.sigma_power:.4f}",
            f"    Physical:    {self.sigma_physical:.4f}",
            "",
            "  RBF Sigma (pitcher):",
            f"    Command:     {self.sigma_command:.4f}",
            f"    Results:     {self.sigma_results:.4f}",
            "",
            "  RBF Sigma (infield):",
            f"    Range:       {self.sigma_if_range}",
            f"    DP:          {self.sigma_if_dp}",
            f"    Errors:      {self.sigma_if_errors}",
            f"    Specialty:   {self.sigma_if_specialty}",
            "",
            "  RBF Sigma (outfield):",
            f"    Range:       {self.sigma_of_range}",
            f"    Arm:         {self.sigma_of_arm}",
            f"    Stars:       {self.sigma_of_stars}",
            f"    Errors:      {self.sigma_of_errors}",
            "",
            "  RBF Sigma (baserunner):",
            f"    Speed:       {self.sigma_baserunner_speed:.4f}",
            f"    Aggression:  {self.sigma_baserunner_aggression:.4f}",
            f"    Success:     {self.sigma_baserunner_success:.4f}",
            "",
            "  RBF Sigma (catcher):",
            f"    Framing:     {self.sigma_catcher_framing:.4f}",
            f"    Blocking:    {self.sigma_catcher_blocking:.4f}",
            f"    Throwing:    {self.sigma_catcher_throwing:.4f}",
            f"    Deterrence:  {self.sigma_catcher_deterrence:.4f}",
            "",
            "  RBF Sigma (steal / manager):",
            f"    BR-steal tendency: {self.sigma_baserunner_steal_tendency:.4f}",
            f"    BR-steal lead:     {self.sigma_baserunner_steal_lead:.4f}",
            f"    BR-steal success:  {self.sigma_baserunner_steal_success:.4f}",
            f"    P-steal outcome:   {self.sigma_pitcher_steal_outcome:.4f}",
            f"    P-steal hold:      {self.sigma_pitcher_steal_hold:.4f}",
            f"    Mgr usage:         {self.sigma_manager_usage:.4f}",
            f"    Mgr aggression:    {self.sigma_manager_aggression:.4f}",
            f"    Mgr platoon:       {self.sigma_manager_platoon:.4f}",
            "",
            f"  Arsenal Gamma:           {self.arsenal_gamma:.4f}",
            f"  EB N_PRIOR (batter):     {self.eb_n_prior_batter:.1f}",
            f"  EB N_PRIOR (pitcher):    {self.eb_n_prior_pitcher:.1f}",
            f"  EB N_PRIOR (baserunner): {self.eb_n_prior_baserunner:.1f}",
            "",
        ]

        for label, attr, module, features_name in _RELIABILITY_BLOCKS:
            weights = getattr(self, attr)
            if weights is None:
                continue
            features = getattr(
                importlib.import_module(f"similarity.engines.{module}"), features_name
            )
            lines.append(f"  Reliability Weights ({label}):")
            for (name, _), w in zip(features, weights, strict=False):
                lines.append(f"    {name:<25} {w:.3f}")
            lines.append("")

        if self.batter_subscores is not None:
            lines.extend(
                [
                    "--- Tier 2: Optimized Sub-Score Weights ---",
                    "",
                    "  Batter: [discipline, batted_ball, platoon, power]",
                    f"    {self.batter_subscores}",
                    "",
                ]
            )

        if self.bats_penalty_lr > 0:
            lines.extend(
                [
                    "--- Tier 3: Design Choice Measurements ---",
                    "",
                    f"  Bats penalty (L↔R): {self.bats_penalty_lr:.4f}",
                    "",
                ]
            )

        lines.append("=" * 70)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # The lossless JSON form. ``to_dict`` walks EVERY dataclass field, so a
    # new field persists with no extra code; it tags numpy arrays so
    # ``from_dict`` restores them as ``np.ndarray``, and turns numpy scalars
    # into Python ones for ``json.dumps``. The round trip is exact:
    # ``from_json(r.to_json())`` equals ``r`` under :meth:`equals`.
    # ------------------------------------------------------------------

    #: Bumped if the persisted layout changes incompatibly.
    SCHEMA_VERSION = 1

    @staticmethod
    def _encode_value(v: Any) -> Any:
        """Convert one field value into a JSON-safe, round-trippable form."""
        if v is None:
            return None
        if isinstance(v, np.ndarray):
            # Tag arrays so from_dict can restore the NDArray type exactly.
            return {"__ndarray__": True, "data": v.tolist()}
        if isinstance(v, np.generic):  # numpy scalar (e.g. np.float64)
            return v.item()
        if isinstance(v, list | tuple):
            return [CalibrationReport._encode_value(x) for x in v]
        # Plain Python scalars (int/float/str/bool) pass through.
        return v

    @staticmethod
    def _decode_value(v: Any) -> Any:
        """Inverse of :meth:`_encode_value`."""
        if isinstance(v, dict) and v.get("__ndarray__"):
            return np.asarray(v["data"], dtype=np.float64)
        if isinstance(v, list):
            return [CalibrationReport._decode_value(x) for x in v]
        return v

    def to_dict(self) -> dict:
        """A lossless, round-trippable dict of EVERY field."""
        out: dict[str, Any] = {"__schema_version__": self.SCHEMA_VERSION}
        for f in fields(self):
            out[f.name] = self._encode_value(getattr(self, f.name))
        return out

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialize to a JSON string (round-trips via :meth:`from_json`)."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> CalibrationReport:
        """Rebuild a CalibrationReport from a :meth:`to_dict` payload.

        An unknown key (``__schema_version__``, or a field of a newer report)
        is ignored, so an older reader never fails on a newer file; a missing
        key takes the dataclass default.
        """
        valid = {f.name for f in fields(cls)}
        kwargs = {k: cls._decode_value(v) for k, v in d.items() if k in valid}
        return cls(**kwargs)

    @classmethod
    def from_json(cls, s: str) -> CalibrationReport:
        """Rebuild a CalibrationReport from a :meth:`to_json` string."""
        return cls.from_dict(json.loads(s))

    def equals(self, other: object) -> bool:
        """Compare field by field, the NDArray fields included.

        The generated ``__eq__`` of ``@dataclass`` fails on an ndarray field
        (``a == b`` is elementwise, and its truth value is ambiguous), so this
        method is the way to assert a lossless round trip.
        """
        if not isinstance(other, CalibrationReport):
            return NotImplemented
        for f in fields(self):
            a = getattr(self, f.name)
            b = getattr(other, f.name)
            if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
                if a is None or b is None:
                    if a is not b:
                        return False
                    continue
                if not np.array_equal(np.asarray(a), np.asarray(b)):
                    return False
            else:
                if a != b:
                    return False
        return True
