## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Why

<!-- The engineering reason. If it changes modelled behaviour, cite the
     standard or reference it now follows. -->

## Checklist

- [ ] Tests added or updated, and every affected service's suite passes
      (`python -m pytest -q` in each service directory)
- [ ] Cross-service changes ride the NATS bus
- [ ] New capability goes through a registry and stays network-agnostic
- [ ] If `sincal_schema.py` changed, both copies were updated in this commit
- [ ] README updated if this changes a number it quotes (test, KPI or service counts)
- [ ] Any network data added is synthetic or already public
