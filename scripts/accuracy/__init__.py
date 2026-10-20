"""The stages of the sim-vs-closing-line accuracy comparison.

``scripts/accuracy_comparison.py`` is the command line. It runs these stages:

* :mod:`accuracy.selection` — which games a run scores, and the frozen inputs it
  scores them with;
* :mod:`accuracy.replay` — each game's replay and scoring, in one process or over a
  pool of worker processes;
* :mod:`accuracy.pricing` — the closing prices: the graded book's row, the best
  price a bettor could take, and the margin that prices a one-sided bet;
* :mod:`accuracy.grading` — what really happened, and one accuracy record per
  priced market;
* :mod:`accuracy.report` — the accuracy table;
* :mod:`accuracy.hypothetical_return` — the opt-in dollar-return table.

:mod:`accuracy.records` holds the record the stages pass along and the market
vocabulary. :mod:`accuracy.confidence` holds the statistics both tables share.
Every module logs as ``"accuracy_comparison"``. The modules import each other
by relative import, so the package works however ``scripts/`` reached
``sys.path``.
"""
