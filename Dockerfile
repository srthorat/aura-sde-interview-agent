# ── Aura — Google SDE Interview Coach ───────────────────────────────────────
# Stage 1: Build the React frontend
FROM node:20-bookworm-slim AS frontend-build

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Python runtime ──────────────────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PORT=7862

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    ffmpeg \
    pkg-config \
    libsndfile1 \
    libspeexdsp-dev \
    swig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN python -m venv "$VIRTUAL_ENV" && \
    . "$VIRTUAL_ENV/bin/activate" && \
    pip install --upgrade pip && \
    pip install uv && \
    uv sync --frozen --no-dev --no-editable --active

COPY bot ./bot
COPY .env.example ./
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

EXPOSE $PORT

CMD ["python", "-m", "bot.bot"]
