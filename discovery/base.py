"""Abstract base adapter — every portal adapter must subclass this."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable

from discovery.models import NormalizedJob


class BaseAdapter(ABC):
    """Base class for all job-board portal adapters."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config

    @abstractmethod
    def fetch_jobs(self, company_slug: str) -> Iterable[dict]:
        """Fetch raw job data from the portal for *company_slug*.

        Yields raw dicts as returned by the portal API.
        """

    @abstractmethod
    def normalize(self, raw: dict, company_slug: str) -> NormalizedJob:
        """Map a raw portal dict to the canonical NormalizedJob shape."""
