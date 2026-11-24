"""operations/backup — the backups of the data stores and their off-site copy.

- ``backup_status`` lists the backup sets and their ages. The API imports it,
  so it is pure standard library.
- ``back_up_data_stores`` backs up, verifies and restores the DuckDB file and
  the engine-artifact bundle.
- ``copy_offsite`` copies the newest complete sets into the owner's OneDrive
  folder. Pure standard library.
- ``backup_drill`` restores the newest sets into a scratch cluster and checks
  them. It runs on the host.
- ``common`` holds the helpers the backup jobs share. Pure standard library.
- ``postgres/`` holds ``pg_backup.sh`` and ``pg_restore.sh``, which the
  database container runs.
"""
