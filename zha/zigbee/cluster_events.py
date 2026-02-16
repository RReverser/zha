"""Shared cluster event and diagnostics dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from zha.application.const import (
    ZHA_CLUSTER_MSG,
    ZHA_CLUSTER_MSG_BIND,
    ZHA_CLUSTER_MSG_CFG_RPT,
)
from zha.application.platforms.cluster_names import (
    CLUSTER_ATTRIBUTE_UPDATED,
    CLUSTER_COMMAND_RECEIVED,
    CLUSTER_EVENT,
)


@dataclass(kw_only=True, frozen=True)
class ClusterAttributeUpdatedEvent:
    """Event to signal that a cluster attribute has been updated."""

    attribute_id: int
    attribute_name: str
    attribute_value: Any
    cluster_unique_id: str
    cluster_id: int
    event_type: Final[str] = CLUSTER_EVENT
    event: Final[str] = CLUSTER_ATTRIBUTE_UPDATED


@dataclass(kw_only=True, frozen=True)
class ClusterCommandEvent:
    """Event to signal that a cluster command has been received."""

    tsn: int
    command_id: int
    args: list[Any]
    cluster_unique_id: str
    cluster_id: int
    event_type: Final[str] = CLUSTER_EVENT
    event: Final[str] = CLUSTER_COMMAND_RECEIVED


@dataclass(kw_only=True, frozen=True)
class ClusterBindEvent:
    """Event generated when the cluster is bound."""

    cluster_name: str
    cluster_id: int
    success: bool
    cluster_unique_id: str
    event_type: Final[str] = ZHA_CLUSTER_MSG
    event: Final[str] = ZHA_CLUSTER_MSG_BIND


@dataclass(kw_only=True, frozen=True)
class ClusterConfigureReportingEvent:
    """Event generated when a cluster configures attribute reporting."""

    cluster_name: str
    cluster_id: int
    attributes: dict[str, dict[str, Any]]
    cluster_unique_id: str
    event_type: Final[str] = ZHA_CLUSTER_MSG
    event: Final[str] = ZHA_CLUSTER_MSG_CFG_RPT
