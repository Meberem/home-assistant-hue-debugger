"""Instrumentation for debugging the Home Assistant Philips Hue integration.

Logging is deliberately lean: every individual HTTP attempt to the bridge is
logged (the most valuable layer), Home Assistant level commands are logged
with their mapped entity, and geofence_client traffic is ignored entirely.
The aiohue logical request layer is not logged because it duplicates the HTTP
layer line for line.

Before each interesting HA command, the state the bridge last reported for
the target resource (from aiohue's EventStream-maintained cache) is logged,
which captures e.g. the brightness a light reports while it is still off.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from contextlib import asynccontextmanager
from itertools import count
from typing import Any, Callable

from aiohue import HueBridgeV2
from aiohue.v2.controllers.events import EventStream, EventType
from aiohue.v2.models.resource import ResourceTypes
from homeassistant.core import HomeAssistant, callback

LOGGER = logging.getLogger(__name__)

_REQUEST_COUNTER = count(1)

# Correlates a Home Assistant Hue command with the aiohue HTTP request(s)
# created while servicing that command. EventStream callbacks run in their
# own task, so they intentionally won't inherit this value.
_CURRENT_HA_REQUEST: ContextVar[int | None] = ContextVar(
    "hue_debug_current_ha_request",
    default=None,
)

# These names are stored on the patched classes themselves rather than only
# in module globals. This prevents an integration reload from wrapping an
# already-wrapped method.
_ORIG_HA_REQUEST = "_hue_debug_original_async_request_call"
_ORIG_AIOHUE_CREATE_REQUEST = "_hue_debug_original_create_request"
_ORIG_EVENT_EMIT = "_hue_debug_original_event_emit"
_ORIG_ENTITY_HANDLE_EVENT = "_hue_debug_original_entity_handle_event"
_PATCH_MARKER = "_hue_debug_patched"

# Controller calls which are particularly useful when investigating light
# state transitions.
_INTERESTING_COMMANDS = {
    "set_state",
    "set_brightness",
    "set_color",
    "set_color_temperature",
    "set_flash",
    "turn_on",
    "turn_off",
    "set_dimming_delta",
}

# Resource types that are never logged at any level. The bridge updates the
# geofence_client resource once per minute; it is pure noise when
# investigating light behaviour.
_IGNORED_RESOURCE_TYPES = {"geofence_client"}


# ---------------------------------------------------------------------------
# Safe diagnostic helpers
# ---------------------------------------------------------------------------


def _safe_repr(value: Any, max_length: int = 6000) -> str:
    """Return a bounded repr suitable for logging."""
    try:
        result = repr(value)
    except Exception:
        result = "<unrepresentable>"

    if len(result) > max_length:
        return f"{result[:max_length]}...<truncated>"

    return result


def _callable_name(value: Callable[..., Any]) -> str:
    """Return a useful name for a callable."""
    try:
        name = getattr(value, "__name__", repr(value))
        owner = getattr(value, "__self__", None)

        if owner is not None:
            return f"{type(owner).__name__}.{name}"

        return name
    except Exception:
        return repr(value)


def _resource_type_from_path(path: str) -> str | None:
    """Extract the resource type from a CLIP v2 path, if present."""
    parts = path.lstrip("/").split("/")

    if (
        len(parts) >= 4
        and parts[0] == "clip"
        and parts[2] == "resource"
    ):
        return parts[3]

    return None


def _resource_state(resource: Any) -> dict[str, Any]:
    """Return Hue state fields relevant to this investigation."""
    result: dict[str, Any] = {
        "type": getattr(
            getattr(resource, "type", None),
            "value",
            None,
        ),
        "id": getattr(resource, "id", None),
    }

    try:
        on = getattr(resource, "on", None)
        if on is not None:
            result["on"] = getattr(on, "on", None)
    except Exception as err:
        result["on"] = f"<error {type(err).__name__}: {err}>"

    try:
        dimming = getattr(resource, "dimming", None)
        if dimming is not None:
            result["brightness_pct"] = getattr(
                dimming,
                "brightness",
                None,
            )
    except Exception as err:
        result["brightness_pct"] = (
            f"<error {type(err).__name__}: {err}>"
        )

    try:
        color_temperature = getattr(
            resource,
            "color_temperature",
            None,
        )
        if color_temperature is not None:
            result["mirek"] = getattr(
                color_temperature,
                "mirek",
                None,
            )
            result["mirek_valid"] = getattr(
                color_temperature,
                "mirek_valid",
                None,
            )
    except Exception as err:
        result["color_temperature"] = (
            f"<error {type(err).__name__}: {err}>"
        )

    try:
        dynamics = getattr(resource, "dynamics", None)
        if dynamics is not None:
            result["dynamics"] = getattr(
                getattr(dynamics, "status", None),
                "value",
                None,
            )
    except Exception as err:
        result["dynamics"] = (
            f"<error {type(err).__name__}: {err}>"
        )

    return result


def _entity_snapshot(entity: Any) -> dict[str, Any]:
    """Return HA properties relevant to the Hue state investigation."""
    snapshot: dict[str, Any] = {}

    for attribute in ("is_on", "brightness", "available"):
        try:
            snapshot[attribute] = getattr(entity, attribute)
        except Exception as err:
            snapshot[attribute] = (
                f"<error {type(err).__name__}: {err}>"
            )

    # These are the two HA-specific Hue brightness workarounds relevant
    # to the 1% transition issue.
    snapshot["_last_brightness"] = getattr(
        entity,
        "_last_brightness",
        None,
    )
    snapshot["_restore_brightness"] = getattr(
        entity,
        "_restore_brightness",
        None,
    )

    return snapshot


def _group_members(entity: Any) -> list[dict[str, Any]]:
    """
    Return current underlying Hue-light state for a grouped light.

    This deliberately mirrors the source of truth used by HA's
    GroupedHueLight: controller.get_lights(resource.id).
    """
    controller = getattr(entity, "controller", None)
    resource = getattr(entity, "resource", None)

    if controller is None or resource is None:
        return []

    get_lights = getattr(controller, "get_lights", None)

    if get_lights is None:
        return []

    try:
        lights = get_lights(resource.id)

        return [
            {
                "id": light.id,
                "name": getattr(
                    getattr(light, "metadata", None),
                    "name",
                    None,
                ),
                "on": getattr(
                    getattr(light, "on", None),
                    "on",
                    None,
                ),
                "brightness_pct": getattr(
                    getattr(light, "dimming", None),
                    "brightness",
                    None,
                ),
            }
            for light in lights
        ]
    except Exception as err:
        return [
            {
                "error": f"{type(err).__name__}: {err}",
            }
        ]


def _is_light_entity(entity: Any) -> bool:
    """Return whether this is a Home Assistant light entity."""
    return str(
        getattr(entity, "entity_id", "")
    ).startswith("light.")


def _is_interesting_command(
    task: Callable[..., Any],
) -> bool:
    """Return whether a controller call is worth INFO-level logging."""
    try:
        return getattr(task, "__name__", None) in _INTERESTING_COMMANDS
    except Exception:
        return False


def _extract_resource_id(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str | None:
    """Extract the Hue resource ID from a controller call."""
    resource_id = kwargs.get("id")

    if isinstance(resource_id, str):
        return resource_id

    if args and isinstance(args[0], str):
        return args[0]

    return None


def _find_entity_id(
    hass: HomeAssistant,
    config_entry_id: str,
    resource_id: str | None,
) -> str | None:
    """Map a Hue resource ID to its HA entity ID when possible."""
    if resource_id is None:
        return None

    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)

        # Hue lights and grouped lights are in the "light" domain with the Hue
        # resource id as the entity unique_id, so this is an O(1) lookup.
        entity_id = registry.async_get_entity_id(
            "light",
            "hue",
            resource_id,
        )
        if entity_id is not None:
            return entity_id

        # Fall back to a scan for any non-light resource types.
        for entity in registry.entities.values():
            if entity.config_entry_id != config_entry_id:
                continue

            if entity.unique_id == resource_id:
                return entity.entity_id

    except Exception:
        # Entity registry lookup is diagnostic only and must never affect HA.
        return None

    return None


def _controller_resource(
    task: Callable[..., Any],
    resource_id: str,
) -> Any:
    """
    Return the bridge-cached resource for a controller command.

    HA passes bound controller methods (e.g. GroupedLightController.set_state)
    as the task; every aiohue BaseResourcesController keeps an EventStream-
    maintained cache and exposes .get(id) on it. This is the state the bridge
    last reported on the API, without making an extra request.
    """
    controller = getattr(task, "__self__", None)
    getter = getattr(controller, "get", None)

    if getter is None:
        return None

    try:
        return getter(resource_id)
    except Exception:
        return None


def _log_pre_command_state(
    request_id: int,
    entity_id: str | None,
    task: Callable[..., Any],
    resource_id: str,
    task_name: str,
) -> None:
    """
    Log the state the bridge last reported for the target resource *before*
    the command is sent.

    This captures exactly what the API reports for a light while it is still
    off at the moment a turn-on command is issued (e.g. the brightness a
    light reports after being turned on to minimum / turned off).
    """
    try:
        resource = _controller_resource(task, resource_id)

        if resource is None:
            return

        group_members: Any = "-"
        controller = getattr(task, "__self__", None)
        get_lights = getattr(controller, "get_lights", None)

        if get_lights is not None:
            try:
                group_members = [
                    {
                        "id": light.id,
                        "on": getattr(
                            getattr(light, "on", None),
                            "on",
                            None,
                        ),
                        "brightness_pct": getattr(
                            getattr(light, "dimming", None),
                            "brightness",
                            None,
                        ),
                    }
                    for light in get_lights(resource_id)
                ]
            except Exception:
                group_members = "<error>"

        LOGGER.info(
            "HUE_PRE_COMMAND #%d entity=%s task=%s resource_id=%s "
            "reported_state=%s group_members=%s",
            request_id,
            entity_id or "<unknown>",
            task_name,
            resource_id,
            _safe_repr(_resource_state(resource)),
            _safe_repr(group_members),
        )

    except Exception:
        LOGGER.exception(
            "Hue debug instrumentation failed logging pre-command "
            "state for request #%d",
            request_id,
        )


def _log_event(
    bridge: HueBridgeV2,
    event_type: EventType,
    data: dict | None,
    event_id: str | None,
) -> None:
    """Log an EventStream event without affecting event processing."""
    try:
        resource_type = data.get("type") if data else None

        if resource_type in _IGNORED_RESOURCE_TYPES:
            return

        resource_id = (
            data.get("id") or data.get("rid")
            if data is not None
            else None
        )

        interesting_types = {
            ResourceTypes.LIGHT.value,
            ResourceTypes.GROUPED_LIGHT.value,
            ResourceTypes.ZIGBEE_CONNECTIVITY.value,
        }

        message = (
            "HUE_EVENT bridge=%s event_id=%s type=%s "
            "resource_type=%s id=%s data=%s"
        )

        args = (
            bridge.host,
            event_id or "<none>",
            event_type.value,
            resource_type,
            resource_id,
            _safe_repr(data),
        )

        if resource_type in interesting_types or data is None:
            LOGGER.info(message, *args)
        else:
            LOGGER.debug(message, *args)

    except Exception:
        # Debugging code must never interfere with the Hue event stream.
        LOGGER.exception(
            "Hue debug instrumentation failed while logging an event"
        )


# ---------------------------------------------------------------------------
# Home Assistant Hue bridge request instrumentation
# ---------------------------------------------------------------------------


async def _patched_ha_request_call(
    self: Any,
    task: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Instrument Home Assistant's HueBridge.async_request_call()."""
    request_id = next(_REQUEST_COUNTER)
    token = _CURRENT_HA_REQUEST.set(request_id)
    started = time.perf_counter()

    task_name = _callable_name(task)
    important = _is_interesting_command(task)
    level = LOGGER.info if important else LOGGER.debug

    resource_id = _extract_resource_id(args, kwargs)
    entity_id = None

    try:
        if important:
            config_entry = getattr(
                self,
                "config_entry",
                None,
            )
            hass = getattr(self, "hass", None)

            if config_entry is not None and hass is not None:
                entity_id = _find_entity_id(
                    hass,
                    config_entry.entry_id,
                    resource_id,
                )

        level(
            "HUE_HA_REQUEST #%d -> bridge=%s task=%s "
            "entity=%s resource_id=%s args=%s kwargs=%s",
            request_id,
            getattr(self, "host", "<unknown>"),
            task_name,
            entity_id or "<unknown>",
            resource_id or "<unknown>",
            _safe_repr(args),
            _safe_repr(kwargs),
        )

        if important and resource_id is not None:
            _log_pre_command_state(
                request_id,
                entity_id,
                task,
                resource_id,
                task_name,
            )

    except Exception:
        LOGGER.exception(
            "Hue debug instrumentation failed logging "
            "HA request #%d",
            request_id,
        )

    try:
        original = getattr(
            type(self),
            _ORIG_HA_REQUEST,
        )

        result = await original(
            self,
            task,
            *args,
            **kwargs,
        )

    except Exception as err:
        LOGGER.error(
            "HUE_HA_REQUEST #%d <- ERROR after %.1fms "
            "type=%s error=%s",
            request_id,
            (time.perf_counter() - started) * 1000,
            type(err).__name__,
            err,
            exc_info=True,
        )
        raise

    finally:
        _CURRENT_HA_REQUEST.reset(token)

    elapsed_ms = (
        time.perf_counter() - started
    ) * 1000

    # The completion line is DEBUG-only: its result/elapsed duplicate the
    # HUE_HTTP_COMPLETE line, which is the layer worth reading.
    LOGGER.debug(
        "HUE_HA_REQUEST #%d <- result=%s elapsed=%.1fms",
        request_id,
        _safe_repr(result),
        elapsed_ms,
    )

    return result


# ---------------------------------------------------------------------------
# aiohue individual HTTP-attempt instrumentation
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _patched_aiohue_create_request(
    self: HueBridgeV2,
    method: str,
    path: str,
    *args: Any,
    **kwargs: Any,
):
    """Instrument every individual HTTP attempt made by aiohue."""
    request_id = next(_REQUEST_COUNTER)
    ha_request_id = _CURRENT_HA_REQUEST.get()
    started = time.perf_counter()

    method_upper = method.upper()
    is_eventstream = path == "eventstream/clip/v2"

    # Ignored resource types (e.g. geofence_client) are not logged at all.
    ignored = (
        _resource_type_from_path(path) in _IGNORED_RESOURCE_TYPES
    )

    suffix = (
        " [EVENTSTREAM]"
        if is_eventstream
        else ""
    )

    # State reads are debug-level to avoid filling the log at startup.
    # EventStream connection status remains INFO-level.
    level = (
        LOGGER.info
        if method_upper != "GET" or is_eventstream
        else LOGGER.debug
    )

    if not ignored:
        level(
            "HUE_HTTP_REQUEST #%d ha_request=%s -> %s %s%s json=%s",
            request_id,
            ha_request_id if ha_request_id is not None else "-",
            method_upper,
            path,
            suffix,
            _safe_repr(kwargs.get("json")),
        )

    response = None
    status: int | None = None

    try:
        original = getattr(
            type(self),
            _ORIG_AIOHUE_CREATE_REQUEST,
        )

        # IMPORTANT:
        # Do not read response.json() or response.content here.
        #
        # HueBridgeV2.request() owns normal response processing and the
        # EventStream uses this same context manager as a long-lived stream.
        async with original(
            self,
            method,
            path,
            *args,
            **kwargs,
        ) as response:
            status = response.status

            if not ignored:
                level(
                    "HUE_HTTP_RESPONSE #%d ha_request=%s "
                    "<- status=%s elapsed_to_headers=%.1fms%s",
                    request_id,
                    ha_request_id if ha_request_id is not None else "-",
                    status,
                    (time.perf_counter() - started) * 1000,
                    suffix,
                )

            yield response

    except Exception as err:
        if not ignored:
            LOGGER.error(
                "HUE_HTTP_REQUEST #%d ha_request=%s <- ERROR "
                "after %.1fms type=%s error=%s%s",
                request_id,
                ha_request_id if ha_request_id is not None else "-",
                (time.perf_counter() - started) * 1000,
                type(err).__name__,
                err,
                suffix,
                exc_info=True,
            )
        raise

    finally:
        if not ignored:
            level(
                "HUE_HTTP_COMPLETE #%d ha_request=%s <- "
                "status=%s total=%.1fms%s",
                request_id,
                ha_request_id if ha_request_id is not None else "-",
                status,
                (time.perf_counter() - started) * 1000,
                suffix,
            )


# ---------------------------------------------------------------------------
# Hue EventStream instrumentation
# ---------------------------------------------------------------------------


def _patched_event_emit(
    self: EventStream,
    event_type: EventType,
    data: dict | None = None,
    *args: Any,
    **kwargs: Any,
) -> None:
    """
    Log EventStream events before aiohue dispatches them.

    `_last_event_id` is maintained by aiohue from the SSE stream and gives us
    an ordering/correlation identifier for events coming from the bridge.
    """
    _log_event(
        self._bridge,
        event_type,
        data,
        getattr(self, "_last_event_id", None),
    )

    original = getattr(
        type(self),
        _ORIG_EVENT_EMIT,
    )

    original(
        self,
        event_type,
        data,
        *args,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Home Assistant Hue entity instrumentation
# ---------------------------------------------------------------------------


@callback
def _patched_entity_handle_event(
    self: Any,
    event_type: EventType,
    resource: Any,
    *args: Any,
    **kwargs: Any,
) -> None:
    """
    Log Hue resource state and HA entity state around on_update().

    The original implementation performs:

        _check_availability()
        on_update()
        async_write_ha_state()

    We log once before that processing and once after it returns.
    """
    light_entity = _is_light_entity(self)

    if light_entity:
        try:
            group_members = (
                _group_members(self)
                if hasattr(self, "hue_group")
                else []
            )

            LOGGER.info(
                "HUE_ENTITY_EVENT -> entity=%s event=%s "
                "resource=%s entity_state_before=%s "
                "group_members=%s",
                getattr(
                    self,
                    "entity_id",
                    "<unknown>",
                ),
                event_type.value,
                _safe_repr(
                    _resource_state(resource)
                ),
                _safe_repr(
                    _entity_snapshot(self)
                ),
                _safe_repr(group_members),
            )

        except Exception:
            LOGGER.exception(
                "Hue debug instrumentation failed before "
                "entity event for %s",
                getattr(
                    self,
                    "entity_id",
                    "<unknown>",
                ),
            )

    original = getattr(
        type(self),
        _ORIG_ENTITY_HANDLE_EVENT,
    )

    original(
        self,
        event_type,
        resource,
        *args,
        **kwargs,
    )

    # RESOURCE_DELETED returns early in HA's original implementation,
    # so there is no useful post-update state to log.
    if (
        light_entity
        and event_type != EventType.RESOURCE_DELETED
    ):
        try:
            group_members = (
                _group_members(self)
                if hasattr(self, "hue_group")
                else []
            )

            LOGGER.info(
                "HUE_ENTITY_STATE <- entity=%s event=%s "
                "is_on=%s brightness=%s available=%s "
                "last_brightness=%s restore_brightness=%s "
                "group_members=%s",
                getattr(
                    self,
                    "entity_id",
                    "<unknown>",
                ),
                event_type.value,
                getattr(
                    self,
                    "is_on",
                    None,
                ),
                getattr(
                    self,
                    "brightness",
                    None,
                ),
                getattr(
                    self,
                    "available",
                    None,
                ),
                getattr(
                    self,
                    "_last_brightness",
                    None,
                ),
                getattr(
                    self,
                    "_restore_brightness",
                    None,
                ),
                _safe_repr(group_members),
            )

        except Exception:
            LOGGER.exception(
                "Hue debug instrumentation failed after "
                "entity event for %s",
                getattr(
                    self,
                    "entity_id",
                    "<unknown>",
                ),
            )


# ---------------------------------------------------------------------------
# Patching and integration setup
# ---------------------------------------------------------------------------


def _patch_method(
    cls: type,
    name: str,
    original_attr_name: str,
    wrapper: Callable[..., Any],
) -> None:
    """Patch a class method once and retain the real implementation."""
    if not hasattr(
        cls,
        original_attr_name,
    ):
        setattr(
            cls,
            original_attr_name,
            getattr(cls, name),
        )

    setattr(
        cls,
        name,
        wrapper,
    )


def _patch_classes() -> None:
    """Install instrumentation exactly once per Python process."""
    from homeassistant.components.hue.bridge import HueBridge
    from homeassistant.components.hue.v2.entity import (
        HueBaseEntity,
    )

    # A module reload resets this module's globals, so use a class-level
    # marker as well.
    if getattr(
        HueBridge,
        _PATCH_MARKER,
        False,
    ):
        return

    _patch_method(
        HueBridge,
        "async_request_call",
        _ORIG_HA_REQUEST,
        _patched_ha_request_call,
    )

    _patch_method(
        HueBridgeV2,
        "create_request",
        _ORIG_AIOHUE_CREATE_REQUEST,
        _patched_aiohue_create_request,
    )

    _patch_method(
        EventStream,
        "emit",
        _ORIG_EVENT_EMIT,
        _patched_event_emit,
    )

    _patch_method(
        HueBaseEntity,
        "_handle_event",
        _ORIG_ENTITY_HANDLE_EVENT,
        _patched_entity_handle_event,
    )

    setattr(
        HueBridge,
        _PATCH_MARKER,
        True,
    )

    LOGGER.warning(
        "Hue debug instrumentation enabled: HA Hue commands (with "
        "pre-command bridge-reported state), individual HTTP "
        "attempts/statuses, EventStream events, and resulting Hue "
        "light entity state will be logged. geofence_client traffic "
        "is ignored and the aiohue logical request layer is not "
        "logged (it duplicates the HTTP layer)."
    )


async def async_setup(
    hass: HomeAssistant,
    config: dict[str, Any],
) -> bool:
    """Set up Hue debug instrumentation."""
    del hass, config

    # Installing the instrumentation must never prevent Home Assistant from
    # starting or the Hue integration from working. If the HA/aiohue internals
    # this relies on have changed, log and continue without instrumentation
    # rather than blocking startup and the ability to control the lights.
    try:
        _patch_classes()
    except Exception:
        LOGGER.exception(
            "Hue debug instrumentation could not be installed; continuing "
            "without it. The Home Assistant Hue integration is unaffected."
        )

    return True
