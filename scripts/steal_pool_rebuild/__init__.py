"""The steps of the steal-pool rebuild (``scripts/rebuild_steal_pool.py``).

The command line runs them in order: :mod:`steal_pool_rebuild.preflight` (step 0),
:mod:`steal_pool_rebuild.pickoffs` (step 1), the rebuild itself in the command
line (step 2), :mod:`steal_pool_rebuild.checks` (step 3),
:mod:`steal_pool_rebuild.bundle` (step 4) and :mod:`steal_pool_rebuild.export`
(steps 5 to 7). :mod:`steal_pool_rebuild.migration` applies migration 0031 inside
step 2; :mod:`steal_pool_rebuild.common` holds the shared constants and helpers.
"""
