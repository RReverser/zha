# Verification Matrix

This matrix defines the required verification sequence for large changes.

## Environment Rule

- Use the existing workspace virtual environment at `venv/`.
- Do not run `./setup` as part of this verification workflow.

Activation example:

```shell
. venv/bin/activate
```

## Required Execution Order

## Step 1: Contract-Focused Gate (must pass first)

```shell
pytest -n auto tests/test_discover.py tests/test_event.py tests/test_device.py
```

Purpose:

- Detect regressions against core contracts:
  - entity identity behavior
  - event schema behavior
  - discovery/registry parity

## Step 2: Broad Confidence Pass (run only after Step 1 passes)

```shell
pytest -n auto tests
```

Purpose:

- Validate that broader behavior still holds across the suite.

## Step 3: Linting Gate (all files)

```shell
pre-commit run --all-files
```

Purpose:

- Enforce formatting, linting, typing, and repo quality hooks.

## Optional Diagnostics Step

Run when changing entity discovery/identity behavior:

```shell
python -m tools.regenerate_diagnostics
```

Then rerun affected diagnostics-related tests.

## Contract-to-Test Mapping

- `unique_id` stability:
  - `tests/test_discover.py`
  - diagnostics-based tests impacted by entity identity
- Event payload/schema stability:
  - `tests/test_event.py`
  - platform tests that assert event-derived state
- Registry/discovery parity:
  - `tests/test_discover.py`
  - selected platform tests in `tests/test_*.py`
