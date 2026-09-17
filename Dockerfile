FROM node:24-bookworm-slim AS frontend

WORKDIR /src
COPY package.json package-lock.json tsconfig.json vite.config.ts ./
COPY frontend ./frontend
RUN npm ci --ignore-scripts && npm run build


FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY backend/requirements-runtime.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt \
    && groupadd --gid 10001 keba \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin keba \
    && install -d -o 10001 -g 10001 /var/lib/keba

COPY --chown=10001:10001 backend/loadmanager /app/loadmanager
COPY --chown=10001:10001 deploy/run_backend.py /app/run_backend.py
COPY --from=frontend --chown=10001:10001 /src/dist /app/static

USER 10001:10001
EXPOSE 8000 8080
VOLUME ["/var/lib/keba"]

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"]

ENTRYPOINT ["python", "/app/run_backend.py"]
