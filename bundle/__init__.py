"""Package the pools and the score tables into the versioned bundle, and load it in each worker.

``python -m bundle.build_bundle`` builds a bundle version and publishes it (``versions``).
Each ``export_*`` module writes one artifact; ``load_bundle.EngineArtifacts`` reads them
all in a worker. ``synthetic_bundle`` builds a small in-memory bundle for the tests.

The steps before are ``pools/`` and ``similarity/``; the step after is ``simulation/``.
"""
