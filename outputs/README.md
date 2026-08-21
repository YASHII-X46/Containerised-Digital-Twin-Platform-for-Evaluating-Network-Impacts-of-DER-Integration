# Outputs

This directory is owned by the UI container.

The current stack sends generate/simulate payloads over NATS, so engines do not
exchange profiles or results through this directory. The UI stores persisted
comparison losses under `results/losses.json` when scenarios are run.

Optional offline CSV exports, if produced by local tooling, should also live
under this folder.
