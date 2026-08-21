# Containerised Digital Twin Platform for Evaluating Network Impacts of DER Integration v5.0 — deployment guide

Every service runs as a Windows container, described by `docker-compose.yml`.

## Why Windows containers

PSS SINCAL is proprietary, Windows-only Siemens software driven over COM, so
the whole stack shares one container engine and the SINCAL solver is a
first-class compose service alongside the other seven.

What to expect from this footprint:

| | |
|---|---|
| Services in compose | 8 |
| Disk | ~10–15 GB (Server Core base layers) |
| First build | tens of minutes — the base image pull is multi-GB |
| Start-up | slow enough to warrant the 60 s healthcheck grace |
| UI edits | `ui/public` is live; `server.js` needs an image rebuild |

## Prerequisites

- **Windows 11 Pro / Enterprise / Education** (Home cannot run Windows
  containers). Windows 11 Pro is confirmed sufficient.
- Docker Desktop with the **Hyper-V** and **Containers** Windows features
  enabled (`Turn Windows features on or off`).
- ~20 GB free disk for images and build cache.

## Container mode

Docker Desktop runs either Linux or Windows containers, one at a time, so it
must be set to Windows containers before anything here works. Right-click the
Docker tray icon → *Switch to Windows containers…* — the daemon restarts, and
any Linux images you have stay on disk. Confirm the current mode with:

```bash
docker info --format "{{.OSType}}"
```

That must print `windows` before building this compose file.

## Build and run

```bash
docker compose up --build
```

Then open <http://localhost:3001>. Upload a network, pick your scenario
controls, and choose the solver (OpenDSS or PSS SINCAL) in the control panel.

Rebuild a single service after a code change:

```bash
docker compose up -d --build simulation-engine
```

## Base images

| Service | Base | Note |
|---------|------|------|
| broker | `nats:2.14-nanoserver` | official NATS Windows image |
| Python services | `python:3.12-windowsservercore-ltsc2025` | see the version note below |
| sincal-solver | `sincal-com:22.5` (build arg `SINCAL_BASE`) | your own image carrying a licensed PSS SINCAL 22.5 plus Python |
| ui | `mcr.microsoft.com/windows/servercore:ltsc2025` + Node zip | Node.js publishes **no** official Windows images, so Node is installed from `nodejs.org` (pin `NODE_VERSION`) |

The `ltsc2025` tag matches this host (Windows 11 build 26200). On an older
host, move every base tag to the generation that host runs.

**Python 3.12 throughout.** It is the oldest Python with a Windows Server Core
variant, and 3.13+ breaks this stack's `numpy < 2.0` pin, which has no cp313
wheels. Every pin in the `requirements.txt` files has a cp312 wheel.

The Python services and the UI use Server Core rather than Nano Server, which
is far smaller but lacks runtime DLLs that OpenDSSDirect.py's bundled native
libraries expect. The broker runs the official NATS Nano Server image, which
needs none of them. Moving the pure-Python services to Nano Server would shrink
the footprint, and needs testing per service.

## Isolation mode

Docker Desktop defaults to **Hyper-V isolation** for Windows containers,
which is what lets a Windows 11 host run `ltsc2025` (Server 2025) images at
all. Process isolation is faster but requires the host and container builds
to match, which they do not here. Leave the default alone unless you know you
need otherwise; if you do, add `isolation: process` per service in the
compose file.

## PSS SINCAL setup

The SINCAL container needs one thing you must supply: the licensed software.

**1. Give the container a SINCAL.** Pick one in `sincal-solver/Dockerfile`:

- **Option A — install into the image.** Drop your installer into
  `sincal-solver/` and uncomment the `INSTALL` block. Self-contained but
  large, and Siemens installers are not always silent-install friendly.
- **Option B — mount a host installation** (lighter; recommended for
  PSS SINCAL Xplore, whose licence is tied to the host anyway). Leave the
  install block commented and uncomment the `volumes:` block in
  `docker-compose.yml`, which read-only mounts
  `C:\Program Files\Siemens\PSS SINCAL Platform 22.5` into the container.

Neither can be automated from this repository: the installer and licence are
Siemens' to distribute, not ours.

**2. Project creation.** The adapter builds a fresh SINCAL project for each
session with SINCAL's own `SinDBCreate.exe` (`/DBSYS:SQLITE /TYPE:E`) and writes
the network model into it. To have every run inherit a project's house settings
instead, point `SINCAL_TEMPLATE` at an existing `.sin` file; the adapter clones
that file together with its `<name>_files` folder.

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

- **Bind mounts are directories.** Windows containers mount a folder, not a
  single file, so the compose file mounts the `broker/` directory, and the UI
  mounts `ui/public` only. Browser assets stay live-editable; **`server.js` and
  `bus.js` changes need an image rebuild**.
- **Healthchecks cannot use shell built-ins** like `test -f`. The NATS
  modules are probed with `python -c "os.path.exists(...)"` against
  `C:\ready\service.ready` (`READY_FILE`); the FastAPI services keep the
  cross-platform `urllib` probe.
- **Slower start-up.** All healthchecks use a 60 s `start_period`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no matching manifest for windows/amd64` | Docker Desktop is in Linux-container mode — switch to Windows containers and retry |
| Build hangs on the first `FROM` | Pulling a multi-GB Server Core base; let it finish once, later builds reuse it |
| `hcsshim::CreateComputeSystem` failure | Hyper-V / Containers Windows features not enabled |
| Bind mount silently empty | You mounted a file, not a directory — see the limitation above |
| `sincal_available: false` in `health` | SINCAL not installed in / mounted into the container (Option A or B) |
| Services healthy but UI cannot reach engines | Check all services are on the compose default network and `NATS_URL=nats://broker:4222` |
