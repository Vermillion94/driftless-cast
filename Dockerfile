# Production image — single-process FastAPI serving both API and static frontend.
# Build with: docker build -t driftless-cast .
# Run with:   docker run -p 8000:8000 -v dc-data:/app/data driftless-cast
FROM python:3.13-slim

# Build deps for sqlite (Python ships with sqlite3 but the image needs the C
# library at runtime) and curl for healthchecks.
RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so the layer caches when only source changes.
COPY pyproject.toml poetry.lock* /app/
RUN pip install --no-cache-dir poetry==1.8.3 \
    && poetry config virtualenvs.create false \
    && poetry install --without dev --no-root --no-interaction \
    && pip install --no-cache-dir "uvicorn[standard]"

# Source. Order matters: copy data/ before src/ so a code-only change doesn't
# bust the data layer.
COPY data /app/data
COPY web /app/web
COPY src /app/src

# Runtime config. SERVE_STATIC=1 makes FastAPI host the web/ directory.
ENV PYTHONUNBUFFERED=1 \
    SERVE_STATIC=1 \
    PORT=8000

EXPOSE 8000

# Healthcheck: hit the OpenAPI doc — fast, low-side-effect, no external API
# dependency. Hosting platforms (Fly, Render, Railway) honor this for routing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/openapi.json > /dev/null || exit 1

CMD ["sh", "-c", "exec uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
