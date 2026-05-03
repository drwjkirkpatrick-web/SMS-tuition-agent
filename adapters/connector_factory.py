"""
adapters/connector_factory.py — Connector instantiation
═══════════════════════════════════════════════════

Maps adapter_type strings to concrete connector classes.
Example:
    connector = get_connector(school_id=1, adapter_type="csv", config={...})
═══════════════════════════════════════════════════
"""

from typing import Optional

from adapters.csv_connector import CSVConnector
from adapters.sis_connector import SISConnector


_CONNECTOR_REGISTRY: dict[str, type[SISConnector]] = {
    "csv": CSVConnector,
    # Future: "powerschool": PowerSchoolConnector,
    # Future: "facts": FACTSConnector,
    # Future: "rest_json": GenericRESTConnector,
}


def get_connector(
    school_id: int,
    adapter_type: str,
    config: dict,
) -> Optional[SISConnector]:
    """
    Factory function: returns a configured SIS connector instance.
    Returns None if adapter_type is not registered.
    """
    connector_cls = _CONNECTOR_REGISTRY.get(adapter_type)
    if connector_cls is None:
        return None
    return connector_cls(school_id=school_id, config=config)


def list_available_connectors() -> list[str]:
    """Return all registered connector type names."""
    return list(_CONNECTOR_REGISTRY.keys())
