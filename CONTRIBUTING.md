# Contributing

Thanks for looking. This is an academic project (Swinburne ENG40007), so the
bar is "does it hold up in a thesis defence" rather than "does it ship": claims
in the README should be reproducible, and behaviour should be covered by a test.

## Getting set up

The Python services run and test straight from a checkout:

```bash
cd load-engine && pip install -r requirements.txt && python -m pytest -q
```

To run the whole stack, see [Quick start](README.md#quick-start), and
[WINDOWS.md](WINDOWS.md) for prerequisites and the PSS SINCAL setup.

## Running the tests

Every service carries its own suite and is run from its own directory:

```bash
python -m pytest -q
```

The `simulation-engine` suite is the slow one: it starts the **real**
`opendss-solver` in-process on a loopback bus, imported from the sibling
checkout, so it needs `pip install -r requirements-dev.txt` rather than
`requirements.txt`.

**Test on the version the containers run.** The images are Python 3.12,
pinned to `numpy<2.0`. CI runs the suites on 3.12, and a run inside the
container is a second tier of testing worth doing before a change lands.

## Architectural rules

These are constraints, not preferences. A change that breaks one will be asked
to change.

- **Services talk over the NATS bus.** Every cross-service exchange is a
  command or event under `{prefix}/command/{service}/{action}`, and profiles,
  results and KPIs travel inline in the message.
- **Every engine is network-agnostic.** Behaviour comes from the supplied model
  and configuration, so the platform runs on any feeder.
- **Solvers do power flow.** Control logic — Volt-VAr/Volt-Watt, envelopes, DR —
  lives in the Simulation Engine.
- **Extend through the registries.** Load archetypes, DER element types, KPIs,
  DR strategies, tariffs and allocation policies each have one; see
  [Modularity](README.md#modularity-the-registries).
- **Keep contributed networks synthetic or already public**, such as the IEEE
  test feeders the suites use.

## The duplicated schema module

`sincal_schema.py` exists twice on purpose — once for the generators and once
vendored into `sincal-solver/sincal_solver/` so the container has no dependency
on an excluded directory. A test asserts the two copies are **byte identical**.
If you edit one, copy it over the other in the same commit.

## Pull requests

1. Branch off `main`.
2. Add or update tests — a bug fix should come with the test that would have
   caught it.
3. Make sure every affected service's suite passes, and CI is green.
4. Describe what you changed and why. If it changes a number the README quotes
   (test counts, KPI counts, service counts), update the README too.

Match the style of the file you are editing: comment density, naming and idiom
vary a little between services, and local consistency wins over a global rule.
