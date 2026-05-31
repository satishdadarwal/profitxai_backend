# ── Base image ───────────────────────────────────────────────────
FROM python:3.11-slim

# Prevent .pyc files, enable stdout logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ── System dependencies ───────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────
COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-deps kiteconnect==4.2.0

# ── Copy project ──────────────────────────────────────────────────
COPY . .

RUN mkdir -p /app/logs && chmod 777 /app/logs


# ── Default port ─────────────────────────────────────────────────
EXPOSE 8000