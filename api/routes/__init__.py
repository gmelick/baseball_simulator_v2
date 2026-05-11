"""
api.routes
==========

FastAPI routers for the MLB Baseball Simulation Platform.

Each module in this package owns a single router. Routers are registered
in ``api.main.create_app`` so module imports stay lazy and unit tests can
import a single router in isolation without booting the whole app.

Current routers
---------------
- ``similarity`` — Similarity Score Explorer endpoints (v1 covers the
  Pitcher → Pitcher engine; spec in ``docs/similarity_visualization_spec.md``).
"""
