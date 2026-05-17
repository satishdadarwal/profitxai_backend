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
    pip install --no-cache-dir -r requirements.txt

# ── Copy project ──────────────────────────────────────────────────
COPY . .

# ── Default port ─────────────────────────────────────────────────
EXPOSE 8000