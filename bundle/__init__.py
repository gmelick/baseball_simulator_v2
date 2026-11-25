"""Package the pools and the score tables into the versioned bundle, and load it in each worker.

The bundle (the engine-artifact bundle) is a folder of .npy, .npz, .parquet and .json files
behind a versioned link (``versions``). Each night ``python -m bundle.build_bundle --what all``
builds a new version and publishes it. Each ``export_*`` module writes one artifact and
holds its loader; ``load_bundle.EngineArtifacts`` reads them all in a worker.
``synthetic_bundle`` builds a small in-memory bundle for the tests.

The steps before are ``pools/`` and ``similarity/``; the step after is ``simulation/``.
"""
