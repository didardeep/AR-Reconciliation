"""Abstract reconciliation service.

Every stage (sales, settlement, final, and later bank) is a subclass. This keeps
the pipeline modular: adding Bank reconciliation later means implementing one
more ``ReconciliationService`` — no restructuring of existing stages.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

import pandas as pd

from app.exceptions_framework import ExceptionCollector


@dataclass
class ReconResult:
    name: str
    frame: pd.DataFrame                 # detailed line-level result
    collector: ExceptionCollector       # exceptions raised by this stage


class ReconciliationService(abc.ABC):
    """Base class for a reconciliation stage."""

    name: str = "base"

    def __init__(self) -> None:
        self.collector = ExceptionCollector(stage_default=self.name)

    @abc.abstractmethod
    def reconcile(self, *args, **kwargs) -> ReconResult:  # noqa: D401
        """Run the stage and return a ReconResult."""

    # shared helper: coerce a money column, recording invalid values as needed
    @staticmethod
    def to_amount(series: pd.Series) -> pd.Series:
        return pd.to_numeric(series, errors="coerce")
