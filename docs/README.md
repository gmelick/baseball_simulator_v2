# Documentation map

The repository holds two kinds of document. A **living document** describes the project
as it is today, and the team keeps it current. A **record** describes one moment: a
plan, a run book, a measurement or a decision. Nobody edits a record after its work is
done, so a record can disagree with today's code. When the two disagree, the living
document and the code win.

## Living documents

| Document | What it is for |
|---|---|
| [README.md](../README.md) | The front door: what the project is, how it works, how to start it. |
| [CLAUDE.md](../CLAUDE.md) | The project guide: the owner's standing rulings, the current state, the conventions and the operating traps. Claude Code reads it first. |
| [WORKFLOW.md](../WORKFLOW.md) | The operator's manual: bring-up, the scheduled jobs, health checks, data runs, backups, troubleshooting. |
| [agent_team.md](../agent_team.md) | The nine roles of the agent team, what each owns, and who to call. |
| [technical/](technical/README.md) | The technical reference: the architecture, the code review path, the file index, the loop cheat sheet and one page per area. |
| [similarity_visualization_spec.md](similarity_visualization_spec.md) | The design of the pitcher similarity explorer. The API code cites its sections. |
| [deploy/README.md](../deploy/README.md) | The reverse proxy, the ports, the secrets and the login, monitoring, the scheduler, the environment tiers and TLS. |
| [db/roles/README.md](../db/roles/README.md) | The three Postgres roles and their grants. |
| [db/migrations/README.md](../db/migrations/README.md) | The two migration systems (Alembic for Postgres, numbered SQL for DuckDB) and their rules. |
| [deploy/monitoring/secrets/README.md](../deploy/monitoring/secrets/README.md) | The API key file Prometheus needs to scrape the app. |
| [BACKLOG.xlsx](../BACKLOG.xlsx) | The open tickets, ranked, each with a description, a definition of done and a proposed solution. |

## Records

| Location | What it holds |
|---|---|
| [CHANGES.md](../CHANGES.md) | The change record: every closed ticket and every data run, newest first. |
| [architecture/](architecture/) | Decision records. `2026-09-02-adr-frontend-framework.md` records the choice of React + Vite + TypeScript, which still holds. The current design is [technical/architecture.md](technical/architecture.md). |
| [audit/](audit/) | The plans of the open tickets, the run books, and the closed plans that code or tests cite. Each file name starts with its date. |
| [audit/records/](audit/records/) | The outputs of runs that a plan or `CHANGES.md` cites. |
| [data_quality/](data_quality/) and [perf/](perf/) | Dated measurements that a migration, a schema comment or a probe cites. |
| [archive/](archive/) | Frozen history: the closed-ticket narrative of the old `BACKLOG.md`, the dated status logs that `CLAUDE.md` carried, and three early design documents (the DuckDB schema and a benchmark still cite two of them). |

## Where to start

- **To understand the system:** [technical/architecture.md](technical/architecture.md).
- **To review the code in order:** [technical/code-review-path.md](technical/code-review-path.md).
- **To find a file:** [technical/file-index.md](technical/file-index.md).
- **To run the stack:** [WORKFLOW.md](../WORKFLOW.md).
- **To see the open work:** [BACKLOG.xlsx](../BACKLOG.xlsx).
