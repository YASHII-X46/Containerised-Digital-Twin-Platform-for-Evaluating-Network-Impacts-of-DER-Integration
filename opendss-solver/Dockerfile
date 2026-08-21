# Pinned to bookworm: on trixie (current :slim) the KLU runtime package
# 'libklu1' that OpenDSSDirect.py needs is no longer available under that name.
FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y libklu1 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The package is named dss_solver (not "app") so the Simulation Engine's test
# suite can import it alongside its own "app" package for contract tests.
COPY dss_solver/ ./dss_solver/

CMD ["python", "-m", "dss_solver.main"]
