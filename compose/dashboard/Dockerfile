# MediaHub dashboard. Build context is the repo root (compose/..).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/mediahub

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Read by the installer helpers (`python -m app.setup bot-prompt|config-diff|version`).
COPY templates ./templates
COPY VERSION ./VERSION
COPY compose/stack.env.example compose/secrets.env.example ./compose-examples/

# The container runs as PUID:PGID from stack.env (compose `user:`; 1000 by default), so the state folders
# are writable for any uid: a fresh named volume copies its mode from here.
RUN useradd -u 1000 -m mediahub && mkdir -p data logs /taste && chown -R mediahub:mediahub /opt/mediahub /taste     && chmod 0777 /opt/mediahub/data /opt/mediahub/logs /taste
USER mediahub

EXPOSE 8000
# uvicorn directly: logs go to stdout for `docker compose logs`.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
