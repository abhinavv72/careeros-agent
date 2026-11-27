"""Portal registry — loads adapters from config/portals.json."""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from discovery.base import BaseAdapter

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "portals.json"


@dataclass
class PortalSource:
    """Metadata + instantiated adapter for one portal."""

    name: str
    adapter_class: str
    enabled: bool
    base_url: str
    companies: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def get_adapter(self) -> "BaseAdapter":
        """Dynamically import and instantiate the adapter class."""
        module_path, class_name = self.adapter_class.rsplit(".", 1)
        # Handle stubs mapping if needed
        if "smartrecruiters" in module_path or "bamboohr" in module_path or "workable" in module_path:
            module_path = "discovery.adapters.stubs"
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls({"base_url": self.base_url, **self.extra})


class PortalRegistry:
    """Loads portal adapter manifests from config/portals.json."""

    def __init__(self, config_path: Path | str | None = None) -> None:
        self._path = Path(config_path) if config_path else _CONFIG_PATH
        self._sources: list[PortalSource] = []
        self._load()

    def get_all_sources(self, enabled_only: bool = True) -> list[PortalSource]:
        """Return all portal sources, optionally filtering to enabled-only."""
        if enabled_only:
            return [s for s in self._sources if s.enabled]
        return list(self._sources)

    def get(self, name: str) -> Optional[PortalSource]:
        """Return a single portal by name, or None if not found."""
        return next((s for s in self._sources if s.name.lower() == name.lower()), None)

    def names(self, enabled_only: bool = True) -> list[str]:
        return [s.name for s in self.get_all_sources(enabled_only)]

    def _load(self) -> None:
        if not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        for entry in data.get("portals", []):
            self._sources.append(
                PortalSource(
                    name=entry["name"],
                    adapter_class=entry["adapter"],
                    enabled=entry.get("enabled", True),
                    base_url=entry.get("base_url", ""),
                    companies=entry.get("companies", []),
                    extra=entry.get("extra", {}),
                )
            )
