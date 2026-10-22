FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ODE_CONFIG=/app/config/config.yaml \
    ODE_LOG_JSON=1

WORKDIR /app

# 1) dependencies only (cached layer; rebuilt only when pyproject changes)
COPY pyproject.toml ./
RUN python - <<'PY' > /tmp/requirements.txt
import tomllib
print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))
PY
RUN pip install -r /tmp/requirements.txt

# 2) the app itself (package includes its SQL migrations)
COPY ode ./ode
COPY config ./config
RUN pip install --no-deps . && ode --help > /dev/null

# Non-root at runtime
RUN useradd --create-home --uid 10001 ode
USER ode

# Default: one full daily cycle (schema -> discover -> collect).
CMD ["ode", "daily"]
