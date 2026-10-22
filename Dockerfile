FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for layer caching
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# App code + SQL + config
COPY ode ./ode
COPY sql ./sql
COPY config ./config

# Default: apply schema, then run one harvest. The daily schedule is driven by
# host cron (see cron/crontab) calling `docker compose run --rm app ode collect`.
CMD ["sh", "-c", "ode initdb && ode collect"]
