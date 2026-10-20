"""
simulation/full_pool_sampler.py
===============================
The full-pool similarity sampler: every draw of the simulator.

Each draw scores a hard-filtered pool of real plays by the applicable
similarity engines and takes one row by that weight. The pitch draw's weight
factorizes so its cost amortizes::

    w_i = f_pitcher[pid:season_i] * f_batter[bid:season_i]
          * f_situation(state, sit_i) * recency_i

The pitcher factor is fixed for a half-inning; the batter factor and the
situation kernel for a plate appearance. Each pitch is then one ``searchsorted``
draw from the live count's CDF. A missing artifact or key leaves its factor
neutral (1.0), so the sampler runs on a partial bundle.

:class:`FullPoolSampler` keeps its constructor here and takes its methods from
the mixins in :mod:`simulation.sampler`, one per draw.
"""

from __future__ import annotations

import numpy as np

from pipeline.batch.engine_artifacts import EngineArtifacts
from simulation.sampler.advancement import AdvancementMixin
from simulation.sampler.fence import FenceMixin
from simulation.sampler.fielding import FieldingMixin
from simulation.sampler.manager import ManagerMixin
from simulation.sampler.pitch import PitchMixin
from simulation.sampler.pitch_result import PitchResultMixin
from simulation.sampler.steal import StealMixin


class FullPoolSampler(
    PitchMixin,
    PitchResultMixin,
    FieldingMixin,
    FenceMixin,
    StealMixin,
    AdvancementMixin,
    ManagerMixin,
):
    """The similarity-weighted draws over one engine-artifact bundle.

    Every switch, power and bandwidth defaults to the value that makes its
    factor a no-op (a power of 1.0, a sigma of 0.0, a weight of 1.0), except
    ``steal_pitch_class``, which defaults to on. The production factory sets
    them from the ``SIM_*`` flags.
    """

    def __init__(
        self,
        artifacts: EngineArtifacts,
        rng: np.random.Generator | None = None,
        *,
        sit_sigma: float = 2.0,
        platoon_off_weight: float = 0.6,
        home_off_weight: float = 1.0,
    ) -> None:
        self.a = artifacts
        self.rng = rng if rng is not None else np.random.default_rng()
        #: The situation kernel's bandwidth (``SIM_SIT_SIGMA``).
        self.sit_sigma = float(sit_sigma)
        self._init_base()
        self._init_pitch()
        self._init_pitch_result()
        self._init_fielding(platoon_off_weight, home_off_weight)
        self._init_fence()
        self._init_steal()
        self._init_advancement()
        self._init_manager()
