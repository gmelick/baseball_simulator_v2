"""Build each player's season profile in DuckDB ``derived.*``: the second step of the program.

``python -m profiles.build_profiles`` runs the nightly build. ``PlayerProfileComputor``
(``profiles/build_profiles.py``) is one class split across the modules here, one mixin per
group: the park factors, the pitchers (with the arsenal fit), the batters, the baserunners,
the managers, the catchers, the fielders and the league averages. The pool builders it also
mixes in live in ``pools/``.

The step before is ``ingest/``; the steps after are ``pools/``, ``similarity/`` and ``bundle/``.
"""
