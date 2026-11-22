"""The betting layer: the odds from BettingPros, their checks and load, and the pricing.

- :mod:`betting.markets`: our market names and book labels.
- :mod:`betting.bettingpros.match_games`: find BettingPros' event for an MLB game.
- :mod:`betting.bettingpros.fetch_odds`: get the odds and build one row per book.
- :mod:`betting.validate`: the checks a row must pass before it is stored.
- :mod:`betting.load`: write the rows, and stamp the closing line.
- :mod:`betting.history`: load the odds of finished games (the nightly pass, backfills).
- :mod:`betting.pricing`: no-vig probabilities, the edge, the expected value, the signal.
"""
