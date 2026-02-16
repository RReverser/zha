"""Mapping registries for zha cluster handlers."""

from zha.decorators import DictRegistry, NestedDictRegistry
from zha.zigbee.cluster_handlers import ClientClusterHandler, ClusterHandler

CLIENT_CLUSTER_HANDLER_REGISTRY: DictRegistry[type[ClientClusterHandler]] = (
    DictRegistry()
)
CLUSTER_HANDLER_REGISTRY: NestedDictRegistry[type[ClusterHandler]] = (
    NestedDictRegistry()
)
