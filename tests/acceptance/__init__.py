"""SIM-450 — the production-configuration acceptance-band lane.

This package holds ONE lane. The lane runs the simulator in the PRODUCTION flag
configuration and asserts each box-score channel against a band around the real
MLB rate. Every other test tree in this repo pins the production flags OFF, so
this package is the only place production and test run the same simulator.

Read :mod:`tests.acceptance.bands` first — it owns the reference rates, the band
rule and the contract that governs changing a band.
"""
