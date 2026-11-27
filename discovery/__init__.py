from discovery.models import NormalizedJob, WorkArrangement, DeduplicationResult
from discovery.geography import parse_work_arrangement
from discovery.dedup import normalize_apply_url, deduplicate_jobs
from discovery.portal_registry import PortalRegistry, PortalSource
from discovery.base import BaseAdapter

__all__ = [
    "NormalizedJob",
    "WorkArrangement",
    "DeduplicationResult",
    "parse_work_arrangement",
    "normalize_apply_url",
    "deduplicate_jobs",
    "PortalRegistry",
    "PortalSource",
    "BaseAdapter",
]
