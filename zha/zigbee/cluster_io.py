"""Low-level Zigbee cluster I/O helpers shared by entity runtime paths."""

from __future__ import annotations

from collections.abc import Callable
import functools
from typing import Any

import zigpy.exceptions
from zigpy.typing import UNDEFINED, UndefinedType
import zigpy.util
import zigpy.zcl
from zigpy.zcl import foundation
from zigpy.zcl.foundation import Status

from zha.exceptions import ZHAException

RETRYABLE_REQUEST_DECORATOR = zigpy.util.retryable_request(tries=3)


def _retry_request(func: Callable[..., Any]) -> Callable[..., Any]:
    """Retry a request and wrap transport errors as `ZHAException`."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await RETRYABLE_REQUEST_DECORATOR(func)(*args, **kwargs)
        except TimeoutError as exc:
            raise ZHAException(
                "Failed to send request: device did not respond"
            ) from exc
        except zigpy.exceptions.ZigbeeException as exc:
            message = "Failed to send request"
            if str(exc):
                message = f"{message}: {exc}"
            raise ZHAException(message) from exc

    return wrapper


async def retryable_cluster_call(
    method: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Execute a cluster call with retries and transport error wrapping."""
    return await _retry_request(method)(*args, **kwargs)


async def safe_read(
    cluster: zigpy.zcl.Cluster,
    attributes: list[int | str | foundation.ZCLAttributeDef],
    allow_cache: bool = True,
    only_cache: bool = False,
    manufacturer: int | UndefinedType | None = UNDEFINED,
) -> dict[Any, Any]:
    """Swallow all exceptions from cluster read operations."""
    try:
        result, _ = await cluster.read_attributes(
            attributes,
            allow_cache=allow_cache,
            only_cache=only_cache,
            manufacturer=manufacturer,
        )
        return result
    except Exception:  # pylint: disable=broad-except
        return {}


async def get_attribute_value(
    cluster: zigpy.zcl.Cluster,
    attribute: int | str,
    *,
    from_cache: bool = True,
) -> Any:
    """Read and return a single attribute value without raising."""
    result = await safe_read(
        cluster,
        [attribute],
        allow_cache=from_cache,
        only_cache=from_cache,
    )
    return result.get(attribute)


async def get_attributes(
    cluster: zigpy.zcl.Cluster,
    attributes: list[int | str],
    *,
    from_cache: bool = True,
    only_cache: bool = True,
) -> dict[int | str, Any]:
    """Read and return multiple attribute values without raising."""
    return await safe_read(
        cluster,
        attributes,
        allow_cache=from_cache,
        only_cache=only_cache,
    )


async def write_attributes_safe(
    cluster: zigpy.zcl.Cluster,
    attributes: dict[str, Any],
    *,
    manufacturer: int | UndefinedType | None = UNDEFINED,
) -> None:
    """Write attributes and raise `ZHAException` when any write fails."""
    try:
        result = await RETRYABLE_REQUEST_DECORATOR(cluster.write_attributes)(
            attributes,
            manufacturer=manufacturer,
        )
    except (zigpy.exceptions.ZigbeeException, TimeoutError) as exc:
        raise ZHAException(f"Failed to write attributes: {exc}") from exc

    for record in result[0]:
        if record.status != Status.SUCCESS:
            attr = cluster.attributes.get(record.attrid)
            attr_name = attr.name if attr is not None else f"0x{record.attrid:04x}"
            value = attributes.get(attr_name, "unknown")
            raise ZHAException(
                f"Failed to write attribute {attr_name}={value}: {record.status}",
            )
