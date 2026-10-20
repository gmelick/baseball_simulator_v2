"""The pitcher engine's shape contract: ``_score_pair`` returns three scores
(composite, arsenal, command), ``SimilarityResult`` carries no release-point
score (the release point lives inside the GMM components), and
``ArsenalCache.finite_distances`` documents the real calibration API.

The behaviour itself is tested in ``tests/unit/test_pitcher_similarity.py``;
these checks read the source, so they also run where scipy is missing.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]

_SCIPY_AVAILABLE = True
try:
    import scipy  # noqa: F401
except ImportError:
    _SCIPY_AVAILABLE = False


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


class TestPitcherEngineContract:
    """Source-level checks, plus the dataclass and docstring checks when scipy
    is installed (CI installs it; a sandbox may not)."""

    def test_score_pair_returns_three_elements(self) -> None:
        """A 3-tuple, not a 5-tuple: the docstring advertises the shape."""
        src = _read(_ROOT / "similarity" / "engines" / "pitcher_similarity.py")
        # 3-tuple guard: _score_pair's docstring advertises a 3-element return.
        assert "(composite, arsenal, command)" in src, (
            "SIM-148: _score_pair docstring must advertise a 3-tuple return."
        )

    def test_no_release_score_in_source(self) -> None:
        """Source-level guard: release_score must not appear as a dataclass
        field annotation.  Catches the regression even when scipy is missing."""
        src = _read(_ROOT / "similarity" / "engines" / "pitcher_similarity.py")
        # Allow `release_score` to appear in comments; reject it as a
        # field annotation like ``release_score: float`` inside the
        # SimilarityResult class.
        # Confine the search to between "class SimilarityResult" and the
        # next blank line after a closing of that block.
        m = re.search(r"class SimilarityResult.*?(?=\n\n[A-Z@])", src, re.DOTALL)
        body = m.group(0) if m else src
        assert "release_score" not in body, (
            "SIM-148/SIM-067: SimilarityResult must NOT declare release_score "
            "(release-point info is already inside the GMM components)."
        )

    @pytest.mark.skipif(not _SCIPY_AVAILABLE, reason="scipy not installed")
    def test_no_release_score_field_on_similarity_result(self) -> None:
        from dataclasses import fields

        from similarity.engines.pitcher_similarity import SimilarityResult

        names = {f.name for f in fields(SimilarityResult)}
        assert "release_score" not in names

    @pytest.mark.skipif(not _SCIPY_AVAILABLE, reason="scipy not installed")
    def test_finite_distances_docstring_uses_real_calibration_api(self) -> None:
        from similarity.engines.pitcher_similarity import ArsenalCache

        doc = ArsenalCache.finite_distances.__doc__ or ""
        # The docstring must reference the REAL calibration API:
        # calibrate_arsenal_gamma → arsenal_scale_from_gamma.
        assert "calibrate_arsenal_gamma" in doc, (
            "SIM-148 AC #4: finite_distances() docstring must reference the "
            "real calibrate_arsenal_gamma API."
        )
        assert "arsenal_scale_from_gamma" in doc, (
            "SIM-148 AC #4: finite_distances() docstring must reference "
            "arsenal_scale_from_gamma (gamma → linear ARSENAL_SCALE)."
        )
        # The phantom calibrate_arsenal_scale name (which would raise
        # ImportError) must NOT appear.
        assert "calibrate_arsenal_scale" not in doc, (
            "SIM-148 AC #4: phantom calibrate_arsenal_scale (no such function) "
            "still in finite_distances() docstring."
        )
