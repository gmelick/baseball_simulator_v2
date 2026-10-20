"""
api.routes
==========

The API's routers.

``api.main.create_app`` includes each router, so a unit test can import one
router alone without booting the app. ``games`` and ``betting`` are aggregate
routers that include sub-routers of their own (``games_slate``, ``games_feed``,
``games_runs``; ``betting_lines``, ``betting_card``); ``_common`` holds the
helpers several routers share, and ``sql_safety`` the one read-only SQL path.
"""
