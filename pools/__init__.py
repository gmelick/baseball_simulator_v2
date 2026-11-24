"""Build the play pools in DuckDB ``sim.*`` that the simulator's draws sample, and check their labels.

Each ``build_*`` module is one mixin of ``profiles.build_profiles.PlayerProfileComputor``:
the pitch pool, the batted-ball pool, the steal pools, the advancement pool, the
intentional-walk rates and the at-bat situations. ``pool_metadata`` records each build;
``check_pool_labels`` checks the pool's count chain against real plate appearances.

The step before is ``profiles/``; the steps after are ``similarity/`` and ``bundle/``.
"""
