"""The betting layer: it compares the simulator's probabilities with the market's prices.

The package has one module, :mod:`betting.bet_signal`. It turns a book's
American odds into implied and no-vig probabilities, compares them with the
simulator's probabilities (the edge and the expected value), and gates, stakes
and ranks the sides worth a bet. Import each name from that module.
"""
