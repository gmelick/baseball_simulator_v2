"""Get the real data into the Postgres ``raw.*`` tables: the first step of the program.

- :mod:`ingest.mlb`: the MLB Stats API client, the schedule and the per-game feed.
- :mod:`ingest.games`: load each finished game's pitches, plays, lineups and venue.
- :mod:`ingest.load_boxscores`: load the official box score of each game.
- :mod:`ingest.load_bullpen_availability`: record which pitchers each team can use in each game.
- :mod:`ingest.venues`: load the park dimensions and fill the missing venues.
- :mod:`ingest.savant`: load the Baseball Savant leaderboards, pitch tracking and sprint speed.
- :mod:`ingest.statcast_events`: the Statcast event and pitch-result code sets.

The next steps are the profiles (``profiles/``), the pools (``pools/``), the similarity
engines, the bundle, the simulation and the outputs.
"""
