# ═══════════════════════════════════════════════════
# Dockerfile — ARM64-optimized Python service image
# ═══════════════════════════════════════════════════
# 
# BASE IMAGE: We use python:3.11-slim-bookworm because it has
# pre-built wheels for ARM64 (aarch64) and a small footprint.
# 
# BUILD NOTE: On Raspberry Pi 4/5 or Jetson, building from
# `python:3.11-alpine` often fails because packages like psycopg2
# and cryptography need to compile C extensions. Slim Debian-based
# images include the necessary system libraries.
# ═══════════════════════════════════════════════════

FROM python:3.11-slim-bookworm

# Install system dependencies needed for compilation (if any wheel
# is missing) and for runtime (libpq for PostgreSQL).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies FIRST (layer caching).
# If requirements.txt doesn't change, Docker skips this step.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code LAST (changes frequently)
COPY . .

# Non-root user for security
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Expose FastAPI port
EXPOSE 8000

# Default command (overridden in docker-compose per service)
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
