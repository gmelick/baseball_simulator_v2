"""The calibration of the similarity engines, one module per part.

- ``fitting.py``: the population fits (the kernel sigma, the arsenal anchor,
  the empirical-Bayes prior, the reliability weights).
- ``report.py``: ``CalibrationReport``, the fitted values and their JSON form.
- ``base.py``: ``CalibratorBase``, the helpers every per-engine mixin shares.
- ``batter.py``, ``pitcher.py``, ``fielder.py``, ``baserunner.py``,
  ``catcher.py``, ``baserunner_steal.py``, ``pitcher_steal.py``,
  ``manager.py``: one mixin per engine, each reading that engine's profiles.

``similarity.similarity_calibration.SimilarityCalibrator`` combines the mixins
and runs the whole fit.
"""
