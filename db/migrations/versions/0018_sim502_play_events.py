"""sim502_play_events

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-11

SIM-502: `raw.play_events` — the non-pitch events the database cannot currently
represent at all.

WHY A NEW TABLE AND NOT COLUMNS ON raw.pitches
==============================================
`raw.pitches` holds one row per PITCH. Some plays contain no pitch, so there is
no row to hang a column on. Measured over 150 real 2024 games (11,247 plays):

    plays with ZERO pitch events ............ 24
      of which Intent Walk .................. 21
      of which Pickoff Caught Stealing ...... 3

An intentional walk is signalled, not thrown, so it generates no pitch events
and therefore no rows. Confirmed against the live database: `raw.pitches` holds
**0** rows with `events = 'intent_walk'` against 14,683 ordinary walks, for a
season that really contained roughly 340-450 of them.

Adding synthetic rows to `raw.pitches` was rejected. Eight profile metrics
divide by `COUNT(*)` treating one row as one pitch — `zone_rate`, `csw_rate`,
`whiff_rate` and others — plus `sample_pitches` and the `MIN_PITCHER_PITCHES`
sample gate. A row without a pitch silently inflates every one of those
denominators. Nothing would error; the numbers would just drift.

WHAT GOES IN, AND WHY EACH EARNS ITS PLACE
==========================================
Measured over the same 150 games, extrapolated to a 2,430-game season:

    pickoff throws .... 601  (~9,700/season)  the DENOMINATOR of a pickoff
                                              decision: how often does this
                                              pitcher throw over?
    stepoffs .......... 269  (~4,400/season)  how a pitcher holds a runner.
                                              Under the pitch-clock rules a
                                              third failed disengagement is a
                                              balk, so it bounds the steal model.
    pickoff outs ...... 21   (~340/season)    ~1/3 are lost today: a pickoff for
                                              the THIRD out has no following
                                              pitch, so nothing records it.
    balks ............. 12   (~194/season)    a free base, currently no channel.
    pickoff errors .... 9    (~145/season)    errant throw, runner advances.
    intentional walks . 21   (~340/season)    the whole plate appearance is
                                              invisible. `walk_rate` filters
                                              `events IN ('walk','intent_walk')`
                                              and that second branch is DEAD —
                                              no such row has ever existed.

WHERE EACH SIGNAL LIVES IN THE MLB PAYLOAD (verified against real games)
=======================================================================
These are NOT in one place, and the obvious guess is wrong for three of them:

  * a pickoff THROW  -> playEvent `type == "pickoff"`.  Its
    `details.eventType` is EMPTY, so an eventType filter never matches it.
  * a stepoff        -> playEvent `type == "stepoff"`, also empty eventType.
  * a balk           -> playEvent `type == "action"` with
    `details.eventType` of `balk` or `forced_balk`.
  * an intent walk   -> the PLAY's `result.event == "Intent Walk"`, with no
    pitch events at all.
  * a pickoff OUT    -> not on the playEvent. On the RUNNER entry, where
    `details.eventType` reads `pickoff_1b` / `pickoff_2b` /
    `pickoff_caught_stealing_2b` and `movementReason` reads `r_pickoff_*`.

NOT DESTRUCTIVE. Creates one table and its indexes. Nothing is dropped, and no
existing column changes, so a downgrade loses only the new table.
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.play_events (
            id              BIGSERIAL       PRIMARY KEY,

            -- Joins to raw.pitches on (game_pk, at_bat_number). `play_index` is
            -- the MLB playEvent index WITHIN the plate appearance, so the pair
            -- (at_bat_number, play_index) orders every event of a game exactly.
            game_pk         INTEGER         NOT NULL,
            at_bat_number   INTEGER         NOT NULL,
            play_index      SMALLINT        NOT NULL,

            game_date       DATE            NOT NULL,
            season          SMALLINT        NOT NULL,

            event_type      VARCHAR(32)     NOT NULL
                                CHECK (event_type IN (
                                    'pickoff', 'pickoff_error', 'stepoff',
                                    'balk', 'intent_walk'
                                )),

            -- Situation, so a similarity draw can filter on it without a join.
            inning          SMALLINT        NOT NULL,
            inning_topbot   VARCHAR(3)      NOT NULL
                                CHECK (inning_topbot IN ('Top','Bot')),
            outs_before     SMALLINT        NOT NULL
                                CHECK (outs_before BETWEEN 0 AND 2),
            runners_state   SMALLINT        NOT NULL
                                CHECK (runners_state BETWEEN 0 AND 7),
            bat_score       SMALLINT        NOT NULL,
            fld_score       SMALLINT        NOT NULL,

            -- Participants.  `base` is the base the throw went to (pickoff) or
            -- the base awarded (intent walk = 1); NULL for a stepoff or balk
            -- that names no base.
            pitcher_id      INTEGER,
            batter_id       INTEGER,
            runner_id       INTEGER,
            base            SMALLINT        CHECK (base BETWEEN 1 AND 4),

            -- Outcome.  `out_number` is the MLB `outNumber`: 3 means this event
            -- ENDED the half-inning, which is exactly the case a pitch-row
            -- design cannot represent.
            is_out          BOOLEAN         NOT NULL DEFAULT FALSE,
            out_number      SMALLINT        CHECK (out_number BETWEEN 1 AND 3),

            fielder_putout  INTEGER,
            fielder_assist  INTEGER,

            created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

            -- One row per event per plate appearance.  A pitcher can throw over
            -- repeatedly, and each throw is its own play_index, so this holds
            -- without collapsing them.
            CONSTRAINT uq_play_events_natural
                UNIQUE (game_pk, at_bat_number, play_index, event_type)
        );
    """)

    # Reload path: `reload_game` deletes a game before re-inserting it.
    op.execute("CREATE INDEX IF NOT EXISTS idx_play_events_game ON raw.play_events(game_pk);")
    # Pool builds read by season and by kind.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_play_events_season_type "
        "ON raw.play_events(season, event_type);"
    )
    # The steal / pickoff model looks a runner up directly.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_play_events_runner "
        "ON raw.play_events(runner_id) WHERE runner_id IS NOT NULL;"
    )

    op.execute("""
        COMMENT ON TABLE raw.play_events IS
        'SIM-502: non-pitch play events (pickoff, stepoff, balk, intentional walk). '
        'raw.pitches holds one row per PITCH and cannot represent a play with no '
        'pitch — an intentional walk is signalled rather than thrown, and a pickoff '
        'for the third out has no following pitch. Both were invisible before this.';
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS raw.idx_play_events_runner;")
    op.execute("DROP INDEX IF EXISTS raw.idx_play_events_season_type;")
    op.execute("DROP INDEX IF EXISTS raw.idx_play_events_game;")
    op.execute("DROP TABLE IF EXISTS raw.play_events;")


__all__ = ["downgrade", "revision", "upgrade"]
