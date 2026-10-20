"""The first eleven Alembic migrations form one unbroken chain: each names the
one before it as its ``down_revision``."""

from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MIG_DIR = _ROOT / "db" / "migrations" / "versions"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


class TestMigrationChain:
    """Each of 0001 to 0011 names the one before it."""

    def test_chain_unbroken(self) -> None:
        chain = {
            "0001_initial_schema.py": None,
            "0002_sim082_lineup_state_live_game_unique_index.py": "0001",
            "0003_sim083_etl_freshness_and_game_odds_to_schema.py": "0002",
            "0004_sim134_prop_odds_prop_stat_column_and_index.py": "0003",
            "0005_sim085_pitches_situation_index.py": "0004",
            "0006_sim086_games_venue_id_nullable.py": "0005",
            "0007_sim087_release_speed_threshold.py": "0006",
            "0008_sim088_drop_pitches_pitch_type_index.py": "0007",
            "0009_sim089_pitches_pitcher_season_clean_index.py": "0008",
            "0010_sim092_game_odds_dedup.py": "0009",
            "0011_sim093_etl_errors_table.py": "0010",
        }
        for filename, expected_parent in chain.items():
            mig = _read(_MIG_DIR / filename)
            if expected_parent is None:
                assert "down_revision = None" in mig, f"{filename}: expected down_revision = None"
            else:
                assert f'down_revision = "{expected_parent}"' in mig, (
                    f'{filename}: expected down_revision = "{expected_parent}"'
                )
