Home Assistant Philips Hue Debugger
===================================

An instrumentation-only custom integration that logs exactly what Home
Assistant sends to, and receives from, a Philips Hue bridge (V2 API). It is
built to investigate bugs where lights do not transition as expected, for
example:

- turning fully on but coming up at 1% brightness, or
- one light in a group turning on while another stays off.

It does **not** change any behaviour. It only adds logging around the existing
Home Assistant Hue integration by wrapping (monkey-patching) these internals:

- `HueBridge.async_request_call` – the HA-level command (with the mapped entity).
- `HueBridgeV2.request` – the logical aiohue API call.
- `HueBridgeV2.create_request` – every individual HTTP attempt and status.
- `EventStream.emit` – events pushed from the bridge.
- `HueBaseEntity._handle_event` – resulting Hue resource and HA entity state.

Because the wrappers only log and then call the original code unchanged, they do
not alter light control. If a future Home Assistant or aiohue update changes the
internals this relies on, setup fails safely: it logs an error and continues
**without** instrumentation, so your lights keep working.

Requirements
------------

- Home Assistant 2024.12.0 or newer.
- The core Philips Hue integration configured and using the **V2 API**
  (a Hue Bridge that supports the CLIP v2 API, e.g. the square/Pro bridge).

Installation
------------

### HACS (recommended)

1. Add this repository as a custom repository in HACS (category: Integration).
2. Install "Philips Hue Debugger".
3. Restart Home Assistant.

### Manual

Copy `custom_components/hue_debug` into your Home Assistant
`config/custom_components` directory and restart Home Assistant.

Enabling it (required)
----------------------

This integration has no config flow. It is loaded from `configuration.yaml`.
Add the following, then restart Home Assistant:

```yaml
# Load the instrumentation.
hue_debug:

# Control how much is logged.
logger:
  default: warning
  logs:
    # INFO shows all mutating commands, events and resulting light state.
    custom_components.hue_debug: info
    # Use DEBUG instead to also capture read-only GET traffic (noisy, but
    # useful for a full picture during a short reproduction window):
    # custom_components.hue_debug: debug
```

On startup you should see a single warning confirming it is active:

> Hue debug instrumentation enabled: ...

If you do **not** see that line, the integration was not loaded — check that
`hue_debug:` is present in `configuration.yaml` and that the core Hue
integration is set up.

Reading the logs
----------------

Each log line is tagged so you can filter and correlate:

| Tag                  | Meaning                                                          |
| -------------------- | --------------------------------------------------------------- |
| `HUE_HA_REQUEST`     | Home Assistant command, with mapped `entity` and `resource_id`. |
| `HUE_API_REQUEST`    | aiohue logical request (with `ha_request` correlation id).      |
| `HUE_HTTP_REQUEST`   | Individual HTTP attempt to the bridge (includes retries).       |
| `HUE_HTTP_RESPONSE`  | HTTP status and time to headers.                                |
| `HUE_HTTP_COMPLETE`  | HTTP attempt finished (status and total time).                  |
| `HUE_EVENT`          | Event pushed from the bridge over the event stream.            |
| `HUE_ENTITY_EVENT`   | Hue resource + HA entity state **before** HA processes it.      |
| `HUE_ENTITY_STATE`   | Resulting HA entity state **after** processing.                 |

The `#<n>` request numbers and the `ha_request=<n>` field let you trace a single
user action from the HA command, through the logical API call, down to each HTTP
attempt.

### For the "turns on at 1%" bug

Look at the `HUE_HA_REQUEST`/`HUE_API_REQUEST` for the turn-on and check the
`brightness` sent, then the following `HUE_EVENT`/`HUE_ENTITY_STATE` to see what
the bridge reports back. The relevant Home Assistant workaround values
(`_last_brightness` for individual lights, `_restore_brightness` for groups) are
included in the entity snapshots.

### For the "one light stays off" bug

For a group, `HUE_ENTITY_EVENT`/`HUE_ENTITY_STATE` include `group_members` with
each underlying light's `on`/`brightness_pct`, and each member light also logs
its own transition. Compare the per-member state against the group command.

To capture a clean trace: set the logger to `info` (or `debug`), reproduce the
issue once, then share the log window around that time.

Safety notes
------------

- Instrumentation is logging-only and calls the original code unchanged.
- If installation fails (e.g. after a breaking HA/aiohue update), it disables
  itself and logs an error; the Hue integration is unaffected.
- Patching happens once per Python process and is safe across integration
  reloads.

Compatibility caveat
--------------------

This works by wrapping Home Assistant and aiohue internals, which are not a
public API and can change between releases. It is pinned to the current
behaviour of both. After a major Home Assistant upgrade, confirm the startup
warning still appears; if it does not, the internals may have changed and the
wrappers may need updating.
