"""pipeline/backup — the backups of the data stores (SIM-525).

``status`` reads which backup sets exist (the API imports it, so it stays pure
stdlib). ``data_stores`` backs up, verifies and restores the DuckDB analytics
file and the engine-artifact bundle.
"""
