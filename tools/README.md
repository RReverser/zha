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

## Compare constants with Home Assistant

ZHA keeps local copies of some Home Assistant constants (unit enums in `zha.units`, and the device-class / mode enums under `zha.application.platforms`). This tool compares them against the installed `homeassistant` package to surface drift. It requires `homeassistant` (and `zha-quirks`) to be installed.

```console
$ python -m tools.compare_constants          # report drift, exits 1 if out of sync
$ python -m tools.compare_constants --write  # apply the safe fixes in place
```

`--write` only applies the unambiguous, additive fixes (adding enum members HA has but ZHA is missing, and correcting value mismatches). Anything needing human judgement — ZHA-only symbols, type mismatches, or entirely new enums HA has that ZHA doesn't mirror — is reported but never written. The `Sync device classes from Home Assistant` GitHub workflow runs this on a schedule and opens a pull request with any safe fixes.
