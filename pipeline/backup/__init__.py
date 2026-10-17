"""pipeline/backup — the backups of the data stores (SIM-525) and their off-site copy (SIM-568).

``status`` reads which backup sets exist (the API imports it, so it stays pure
stdlib). ``data_stores`` backs up, verifies and restores the DuckDB analytics
file and the engine-artifact bundle. ``common`` holds the helpers that both
backup modules share (pure stdlib). ``offsite`` copies the newest complete sets
into the off-site folder, the owner's OneDrive folder (pure stdlib, SIM-568).
"""
