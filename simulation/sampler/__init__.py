"""The parts of the full-pool sampler, one module per draw.

:class:`simulation.full_pool_sampler.FullPoolSampler` is the sampler. It keeps
its constructor in ``simulation/full_pool_sampler.py`` and takes its methods
from the mixins here:

* :mod:`~simulation.sampler.kernels` -- the module-level helpers: the power and
  draw-neutral arithmetic, the pitch engine's feature weights, the row grouping.
* :mod:`~simulation.sampler.base` -- the shared machinery: the pool precompute,
  the as-of cutoff, the actor score matrices and the draw-neutral means.
* :mod:`~simulation.sampler.pitch` -- the pitch draw: the pitcher, batter,
  situation and fatigue factors, the cell index and the count-bucket draw.
* :mod:`~simulation.sampler.pitch_result` -- the pitch-result draw, the catcher
  receiving ratio and the readers of the last drawn pitch.
* :mod:`~simulation.sampler.fielding` -- the batted-ball (fielding) draw.
* :mod:`~simulation.sampler.fence` -- the fence stage, the carry and the
  wall-margin band.
* :mod:`~simulation.sampler.steal` -- the steal and pickoff draws.
* :mod:`~simulation.sampler.advancement` -- the advancement draws.
* :mod:`~simulation.sampler.manager` -- the pitching-change and reliever draws.

Every draw takes its random number from the sampler's one ``self.rng``.
"""
