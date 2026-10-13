-- =============================================================================
-- SIM-443b: the two least-privilege roles and their grants (idempotent).
--
-- Run by db/roles/setup_roles.sh, which passes these psql variables:
--   admin_role      the cluster owner (POSTGRES_USER, an identifier)
--   db_name         the database (POSTGRES_DB, an identifier)
--   app_password    baseball_app's password (a literal)
--   ro_password     baseball_ro's password (a literal)
--   rotate_admin    on / off: also set the owner's password
--   admin_password  the owner's new password (a literal; empty when off)
--
-- A new Postgres schema needs its lines here (the grants, the default
-- privileges); see db/roles/README.md.
-- =============================================================================

-- Quiet the "already exists, skipping" notices of a rerun.
SET client_min_messages = warning;

-- Migration 0001 uses CREATE SCHEMA IF NOT EXISTS, so creating the two
-- schemas here (owned by the owner) is safe before the first `make migrate`.
CREATE SCHEMA IF NOT EXISTS raw AUTHORIZATION :"admin_role";
CREATE SCHEMA IF NOT EXISTS sim AUTHORIZATION :"admin_role";

-- Create a missing role. The password is set below on every run.
SELECT 'CREATE ROLE baseball_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'baseball_app')
\gexec
SELECT 'CREATE ROLE baseball_ro'
WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'baseball_ro')
\gexec

ALTER ROLE baseball_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
    NOREPLICATION NOBYPASSRLS PASSWORD :'app_password';
ALTER ROLE baseball_ro WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
    NOREPLICATION NOBYPASSRLS PASSWORD :'ro_password';
-- Every transaction the console opens is read-only, whatever it sends.
ALTER ROLE baseball_ro SET default_transaction_read_only = on;

\if :rotate_admin
ALTER ROLE :"admin_role" WITH PASSWORD :'admin_password';
\endif

GRANT CONNECT ON DATABASE :"db_name" TO baseball_app, baseball_ro;

-- baseball_app: rows in raw and sim. Not granted: TRUNCATE, REFERENCES,
-- TRIGGER, CREATE. Serial ids need USAGE on the sequences.
GRANT USAGE ON SCHEMA raw, sim TO baseball_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA raw, sim TO baseball_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA raw, sim TO baseball_app;
-- The tables a later migration creates (as the owner) get the same grants.
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_role" IN SCHEMA raw, sim
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO baseball_app;
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_role" IN SCHEMA raw, sim
    GRANT USAGE, SELECT ON SEQUENCES TO baseball_app;

-- baseball_ro: read raw only.
GRANT USAGE ON SCHEMA raw TO baseball_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA raw TO baseball_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_role" IN SCHEMA raw
    GRANT SELECT ON TABLES TO baseball_ro;
-- sim holds the run records and the lineup state: never the console's.
REVOKE ALL ON SCHEMA sim FROM baseball_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA sim FROM baseball_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA sim FROM baseball_ro;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA raw FROM baseball_ro;

-- The summary: the roles and their flags. No secret is printed.
SELECT rolname, rolsuper, rolcanlogin, rolcreatedb, rolcreaterole, rolinherit
FROM pg_catalog.pg_roles
WHERE rolname IN ('baseball_app', 'baseball_ro', :'admin_role')
ORDER BY rolname;
