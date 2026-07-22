# Базовый образ запиннен по digest (multi-arch manifest list) — воспроизводимость
# сборки и контроль supply-chain. Обновляется осознанно через Dependabot (docker).
# Тег python:3.12-slim на момент пина указывал на этот digest.
FROM python:3.12-slim@sha256:a39549e211a16149edf74e5fdc9ef03a6767e46cd987c5048b6659b6c9904c94 AS base

# Непривилегированный пользователь — контейнер не работает под root.
RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml .
RUN uv pip install --system --no-cache .

COPY src/ ./src/

# Воркдир должен принадлежать appuser: celery beat (-B) пишет файл расписания в CWD;
# uvicorn / alembic — только читают. Зависимости лежат в /usr/local (read-only, ok).
RUN chown -R appuser:appuser /app

USER appuser

FROM base AS migration
CMD ["alembic", "-c", "src/alembic.ini", "upgrade", "head"]

FROM base AS app
EXPOSE 8000
# HEALTHCHECK в самом образе — здоровье видит и docker, и оркестратор, не только
# compose-healthcheck. Воркер переопределяет его в compose (celery ping), т.к. не
# слушает HTTP; миграционный one-shot — отключает (см. docker-compose.prod.yml).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" || exit 1
CMD ["uvicorn", "src.app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Только для локальной разработки (docker-compose.yml → api: target: dev).
# CI (ci.yml) ставит dev-зависимости сам через `uv sync --extra dev` на раннере,
# в Docker не собирается вовсе. release.yml и docker-compose.prod.yml используют
# target: app — эта стадия НЕ собирается ни при сборке прод-образа, ни в CI/release,
# поэтому ruff/mypy/pytest никогда не попадают в то, что уезжает в ghcr.io.
FROM app AS dev
USER root
RUN uv pip install --system --no-cache ".[dev]"
USER appuser
