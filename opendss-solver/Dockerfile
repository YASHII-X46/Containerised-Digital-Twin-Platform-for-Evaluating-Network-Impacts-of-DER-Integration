# OpenDSS solver — Windows container (Containerised Digital Twin Platform v5.0).
#
# The OpenDSSDirect.py wheel bundles its own native DLLs, so no separate KLU
# runtime package is needed; the Python image already carries the MSVC runtime
# those DLLs link against.
FROM python:3.12-windowsservercore-ltsc2025

WORKDIR C:\\app

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt

# Package is dss_solver (not "app") so the Simulation Engine test suite can
# import it alongside its own "app" package for contract tests.
COPY dss_solver/ ./dss_solver/

# The official Python Windows image sets SHELL to PowerShell, whose mkdir
# takes one positional path, so create both directories explicitly.
RUN New-Item -ItemType Directory -Force C:\ready, C:\dss_work | Out-Null

CMD ["python", "-m", "dss_solver.main"]
