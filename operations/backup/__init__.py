"""operations/backup — the backups of the data stores and their off-site copy.

One file per step:

- ``backup_status`` reads which backup sets exist and how old they are (the API
  imports it, so it stays pure stdlib).
- ``back_up_data_stores`` backs up, verifies and restores the DuckDB analytics
  file and the engine-artifact bundle.
- ``copy_offsite`` copies the newest complete sets into the off-site folder, the
  owner's OneDrive folder (pure stdlib, SIM-568).
- ``backup_drill`` restores the newest sets into a scratch cluster and checks them
  (run on the host).
- ``common`` holds the helpers the backup modules share (pure stdlib).
- ``postgres/`` holds the two shell scripts the database container runs:
  ``pg_backup.sh`` and ``pg_restore.sh``.
"""
