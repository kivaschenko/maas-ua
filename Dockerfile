FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project

COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

RUN uv sync --locked --no-dev

USER 10001:10001

CMD ["python", "-m", "flask", "--app", "maas_ua.entrypoints.flask_app:create_app", "--no-debug", "run", "--host", "127.0.0.1", "--port", "8000", "--no-reload", "--no-debugger"]
