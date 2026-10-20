"""The steps of the pitch-pool rebuild (``scripts/rebuild_pitch_pool.py``).

The command line runs them in order: :mod:`pitch_pool_rebuild.preflight` (step 0),
the snapshot and the rebuild itself in the command line (steps 1 and 2),
:mod:`pitch_pool_rebuild.checks` (step 3) and :mod:`pitch_pool_rebuild.export`
(steps 4 to 6). :mod:`pitch_pool_rebuild.common` holds the shared constants and
helpers.
"""
