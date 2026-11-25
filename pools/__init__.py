"""Build the play pools in DuckDB ``sim.*`` that the draws sample, and check their labels.

A play pool holds the real plays one draw samples. Each ``build_*`` module is one mixin
of ``profiles.build_profiles.PlayerProfileComputor``, which holds the build order. The
mixins build the pitch pool, the batted-ball pool, the two steal pools, the advancement
pool, the intentional-walk rates and the at-bat situations. ``pool_metadata`` holds the
recency weights, the incremental-rebuild gate and the build records.
``check_pool_labels`` checks the pitch pool's count chain against real plate appearances.

The step before is ``profiles/``. The steps after are ``similarity/`` and ``bundle/``.
"""
