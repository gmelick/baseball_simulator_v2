"""
api/schemas/_base.py
====================
The base of every response model.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _ApiModel(BaseModel):
    """Shared base for every SIM-350 response model.

    ``extra="forbid"`` keeps the wire contract tight (a typo'd field is an error,
    not silently accepted); the models are response-only so we do not need
    ``populate_by_name`` / aliasing here.
    """

    model_config = ConfigDict(extra="forbid")
