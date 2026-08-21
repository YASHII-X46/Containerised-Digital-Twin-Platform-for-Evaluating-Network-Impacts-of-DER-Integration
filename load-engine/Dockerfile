# Load Engine — Windows container (Containerised Digital Twin Platform v5.0).
#
# Python 3.12 is the newest Server Core base that still satisfies this stack's
# dependency pins (numpy < 2.0 has no cp313+ wheels), and every pin in
# requirements.txt has a cp312 wheel.
FROM python:3.12-windowsservercore-ltsc2025

WORKDIR C:\\app

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt

COPY app/ ./app/

# Readiness/health probe target (see docker-compose.yml).
RUN mkdir C:\\ready

EXPOSE 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
