FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# OS deps:
# - curl: healthcheck
# - libxml2 + xmlsec1 + pkg-config: required by python3-saml (SAML SSO)
# - libpq5: required by psycopg2-binary (Postgres engine)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libxml2 libxml2-dev \
        libxmlsec1-dev libxmlsec1-openssl \
        pkg-config \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0

COPY . .

# Non-root for runtime.
RUN useradd --create-home --shell /bin/sh filenergy \
    && mkdir -p /data /app/files \
    && chown -R filenergy:filenergy /data /app
USER filenergy

ENV FILENERGY_DB_PATH=/data/filenergy.db \
    FILENERGY_UPLOAD_DIR=/data/files \
    FILENERGY_BASE_URL=http://localhost:5000

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:5000/healthz || exit 1

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "120", "manage:app"]
