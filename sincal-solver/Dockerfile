# PSS SINCAL solver adapter, Windows container (Containerised Digital Twin Platform v5.0).
#
# Built on the locally-built `sincal-com` image, which already carries a
# licensed PSS SINCAL 22.5 installation plus Python 3.12, pywin32 and nats-py.
# That image is NOT public and is not built by this repository: PSS SINCAL is
# proprietary, Windows-only, licensed Siemens software whose installer cannot
# be redistributed here. Build it yourself first (see README.md), or override
# the base with:
#
#     docker compose build \
#       --build-arg SINCAL_BASE=my-sincal-image:tag sincal-solver
#
# Without a SINCAL-bearing base the service still starts and reports
# sincal_available=false from `health`, and fails `build` with a clear error.
ARG SINCAL_BASE=sincal-com:22.5
FROM ${SINCAL_BASE}

SHELL ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';"]

# nats-py and pywin32 ship in the base image; only the settings layer is new.
RUN python -m pip install --no-cache-dir --timeout 120 --retries 5 "pydantic-settings>=2.3.4"

WORKDIR C:\\app

COPY sincal_solver/ ./sincal_solver/

RUN New-Item -ItemType Directory -Force C:\ready, C:\sincal_work | Out-Null

# The base image's Initialize-SincalUser step provisions the per-user SINCAL
# data that the COM server needs, and must run before the service starts. It
# writes progress to stderr, so it never pollutes the service's stdout.
ENTRYPOINT ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", \
            "& C:\\scripts\\Initialize-SincalUser.ps1 -Version $env:SINCAL_VERSION; python -m sincal_solver.main"]
