# ZHA development tools

This module contains development tools for ZHA.

## Import diagnostics JSON

ZHA relies on device diagnostics files to allow us to test entity creation with "real" devices as part of our CI. For this, we collect device diagnostics data from Home Assistant. You can create these JSON files by navigating to individual devices, clicking the three dot dropdown menu, and then selecting "Download diagnostics". These files can then be imported into the database.

```console
$ python -m tools.import_diagnostics /path/to/diagnostics1.json /path/to/diagnostics2.json ...
```

## Regenerate diagnostics JSON

If entities change, the current diagnostics JSON will no longer be valid and CI will fail. This is normal. If you are changing entities on existing devices, the diagnostics JSON will need to be regenerated. Our diagnostics loading/serialization is idempotent so just regenerate all of them:

```console
$ python -m tools.regenerate_diagnostics
```

## Sync constants from Home Assistant

ZHA keeps near 1:1 copies of some Home Assistant enums (unit enums in `zha.units`, and the device-class / mode enums under `zha.application.platforms`). This tool copies them verbatim — including docstrings and comments — from the installed `homeassistant` package, so they stay in sync. It requires `homeassistant` (and `zha-quirks`) to be installed.

```console
$ python -m tools.sync_constants          # copy the enums from HA into ZHA
$ python -m tools.sync_constants --check  # dry run, exits 1 if anything is out of sync
$ ruff format zha/                         # normalise whitespace afterwards
```

Only enums are synced; ZHA-only symbols and module-level constants are left untouched. The `Sync device classes from Home Assistant` GitHub workflow runs this against Home Assistant's `dev` branch on a schedule and opens a pull request with the result.
