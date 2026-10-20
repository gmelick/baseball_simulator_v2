"""simulation — the full-pool sampler and the pitch-by-pitch game loop.

``full_pool_sampler.py`` holds the sampler, which draws every play from an
engine-artifact bundle; its draws live in ``sampler/``. ``sim_loop.py`` holds
the state machine's core and the full-game driver; its other steps live in
``loop/``. ``synthetic_bundle.py`` builds the in-memory bundle the no-DB paths
use.
"""
