# Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration v5.0 — full Windows container deployment

v5.0 adds a second, complete deployment mode: **every service as a Windows
container**, described by `docker-compose.windows.yml`. The Linux deployment
(`docker-compose.yml`) is unchanged and remains the default.

## Why this mode exists

PSS SINCAL is proprietary, Windows-only Siemens software driven over COM. It
cannot run in a Linux container, so in the Linux deployment the `sincal-solver`
adapter has to run as a host-side process outside compose — a split-brain
stack. Under Windows containers the whole stack shares one engine, and the
SINCAL solver becomes a first-class compose service like any other.

| | Linux mode (`docker-compose.yml`) | Windows mode (`docker-compose.windows.yml`) |
|---|---|---|
| Services in compose | 7 | **8 (adds `sincal-solver`)** |
| PSS SINCAL | host-run side process | first-class container service |
| Disk footprint | ~1–2 GB | **~10–15 GB** (Server Core base layers) |
| First build | minutes | **tens of minutes** (base image pull is multi-GB) |
| Start-up | seconds | slower (hence 60 s healthcheck grace) |
| UI `server.js` live edit | yes (file bind mount) | no — rebuild the `ui` image |

Use Linux mode for everyday OpenDSS work. Use Windows mode when you want
SINCAL in the same `docker compose up` as everything else.

## Prerequisites

- **Windows 11 Pro / Enterprise / Education** (Home cannot run Windows
  containers). Windows 11 Pro is confirmed sufficient.
- Docker Desktop with the **Hyper-V** and **Containers** Windows features
  enabled (`Turn Windows features on or off`).
- ~20 GB free disk for images and build cache.

## Switching container modes

Docker Desktop runs **either** Linux **or** Windows containers, never both at
once. Right-click the Docker tray icon → *Switch to Windows containers…*
(the daemon restarts; your Linux images stay on disk and come back when you
switch back). Confirm the current mode with:

```bash
docker info --format "{{.OSType}}"
```

That must print `windows` before building this compose file.

## Build and run

```bash
docker compose -f docker-compose.windows.yml up --build
```

Then open <http://localhost:3001>. Upload a network, pick your scenario
controls, and choose the solver (OpenDSS or PSS SINCAL) in the control panel.

Rebuild a single service after a code change:

```bash
docker compose -f docker-compose.windows.yml up -d --build simulation-engine
```

## Base images

| Service | Base | Note |
|---------|------|------|
| broker | `nats:2-windowsservercore-ltsc2022` | official NATS Windows image |
| Python services | `python:3.12-windowsservercore-ltsc2022` | see the version note below |
| ui | `mcr.microsoft.com/windows/servercore:ltsc2022` + Node zip | Node.js publishes **no** official Windows images, so Node is installed from `nodejs.org` (pin `NODE_VERSION`) |

**Python 3.12, not 3.11.** The Linux images use `python:3.11-slim`, but Python
3.11 publishes no Windows Server Core variant — the oldest available is 3.12,
and the newest (3.13+) breaks this stack's `numpy < 2.0` pin, which has no
cp313 wheels. 3.12 is wheel-compatible with every pin in the existing
`requirements.txt` files, so the Windows and Linux images install identical
dependency versions.

Server Core (not Nano Server) is used deliberately: Nano Server is far
smaller but lacks runtime DLLs that OpenDSSDirect.py's bundled native
libraries expect. Moving the pure-Python services to Nano Server would shrink
the footprint, but it needs testing per service and is not done here.

## Isolation mode

Docker Desktop defaults to **Hyper-V isolation** for Windows containers,
which is what lets a Windows 11 host run `ltsc2022` (Server 2022) images at
all. Process isolation is faster but requires the host and container builds
to match, which they do not here. Leave the default alone unless you know you
need otherwise; if you do, add `isolation: process` per service in the
compose file.

## PSS SINCAL setup

The SINCAL container needs two things you must supply — the licensed software
and an empty project to clone.

**1. Give the container a SINCAL.** Pick one in
`sincal-solver/Dockerfile.windows`:

- **Option A — install into the image.** Drop your installer into
  `sincal-solver/` and uncomment the `INSTALL` block. Self-contained but
  large, and Siemens installers are not always silent-install friendly.
- **Option B — mount a host installation** (lighter; recommended for
  PSS SINCAL Xplore, whose licence is tied to the host anyway). Leave the
  install block commented and uncomment the `volumes:` block in
  `docker-compose.windows.yml`, which read-only mounts
  `C:\Program Files\Siemens\PSS SINCAL Platform 22.5` into the container.

Neither can be automated from this repository: the installer and licence are
Siemens' to distribute, not ours.

**2. Create the template project.** SINCAL ships no standalone empty-project
template, so make one once in the SINCAL GUI (e.g.
`sincal-solver/template/dtstack_template.sin`); the adapter clones it — and
its `<name>_files` folder — into a fresh working directory per session.
`SINCAL_TEMPLATE` in the compose file points at it.

**Xplore edition caps.** PSS SINCAL Xplore 22.5 includes the COM automation
server this adapter drives (verified: `Sincal.Simulation` dispatches), but
caps network size at a small node count. Fine for IEEE test feeders and
thesis-scale demonstrations; check the cap before planning full-feeder SINCAL
runs. The OpenDSS solver has no such limit and remains the default.

**Without SINCAL present** the service still starts, reports
`sincal_available: false` from its `health` command, and fails `build` with a
clear error event. It never silently fakes a solve. The rest of the stack is
unaffected — `simulation-engine` deliberately does not depend on
`sincal-solver` being healthy.

## Windows-container limitations baked into the compose file

- **No single-file bind mounts.** Windows containers can only bind-mount
  directories. The Linux compose mounts `broker/nats.conf`, `ui/server.js`,
  and `ui/bus.js` as individual files; the Windows compose mounts the
  `broker/` directory instead, and drops the two UI file mounts. `ui/public`
  is still mounted (it is a directory), so browser assets stay live-editable
  — but **`server.js` and `bus.js` changes now need an image rebuild**.
- **Healthchecks cannot use shell built-ins** like `test -f`. The NATS
  modules are probed with `python -c "os.path.exists(...)"` against
  `C:\ready\service.ready` (`READY_FILE`); the FastAPI services keep the
  cross-platform `urllib` probe.
- **Slower start-up.** All healthchecks use a 60 s `start_period`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no matching manifest for windows/amd64` | Docker is still in Linux mode — switch containers and retry |
| Build hangs on the first `FROM` | Pulling a multi-GB Server Core base; let it finish once, later builds reuse it |
| `hcsshim::CreateComputeSystem` failure | Hyper-V / Containers Windows features not enabled |
| Bind mount silently empty | You mounted a file, not a directory — see the limitation above |
| `sincal_available: false` in `health` | SINCAL not installed in / mounted into the container (Option A or B) |
| Services healthy but UI cannot reach engines | Check all services are on the compose default network and `NATS_URL=nats://broker:4222` |
