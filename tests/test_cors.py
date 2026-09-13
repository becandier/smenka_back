"""CORS — preflight и заголовки для браузерных клиентов (админка + flutter web).

Приёмка web_cors:
- preflight OPTIONS с разрешённого origin проходит, Allow-Origin = origin;
- allow-headers покрывает Authorization/Content-Type, allow-methods — POST/PATCH/DELETE/OPTIONS;
- allow_credentials выключен (web шлёт JWT в Authorization, не cookie);
- origin вне whitelist не получает Allow-Origin (браузер режет ответ).
"""

import pytest
from httpx import AsyncClient

from src.app.core.config import get_settings
from src.app.main import app

settings = get_settings()

# CORSMiddleware подключается в main.py только при непустом cors_origins; если в
# окружении CORS выключен (CORS_ORIGINS пуст), приёмочные проверки нерелевантны.
pytestmark = pytest.mark.skipif(
    not settings.cors_origins, reason="CORS отключён (CORS_ORIGINS пуст)"
)

ALLOWED_ORIGIN = settings.cors_origins[0] if settings.cors_origins else ""
DISALLOWED_ORIGIN = "https://evil.example.com"


class TestCorsPreflight:
    async def test_preflight_allowed_origin(self, client: AsyncClient):
        resp = await client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN

        allow_methods = resp.headers["access-control-allow-methods"]
        for method in ("POST", "PATCH", "PUT", "DELETE", "OPTIONS"):
            assert method in allow_methods, f"метод {method} не в Allow-Methods"

        allow_headers = resp.headers["access-control-allow-headers"].lower()
        assert "authorization" in allow_headers
        assert "content-type" in allow_headers

    async def test_preflight_credentials_disabled(self, client: AsyncClient):
        # allow_credentials=False → Starlette не выставляет Allow-Credentials.
        resp = await client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert resp.headers.get("access-control-allow-credentials") != "true"

    async def test_preflight_disallowed_origin(self, client: AsyncClient):
        resp = await client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": DISALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        # Origin не в whitelist → Allow-Origin не равен этому origin (браузер режет ответ).
        assert resp.headers.get("access-control-allow-origin") != DISALLOWED_ORIGIN


class TestCorsSimpleRequest:
    async def test_actual_request_echoes_origin(self, client: AsyncClient):
        # Реальный (не preflight) запрос с разрешённого origin получает Allow-Origin.
        resp = await client.get("/health", headers={"Origin": ALLOWED_ORIGIN})
        assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN

    async def test_actual_request_disallowed_origin_no_header(self, client: AsyncClient):
        resp = await client.get("/health", headers={"Origin": DISALLOWED_ORIGIN})
        assert resp.headers.get("access-control-allow-origin") != DISALLOWED_ORIGIN


class TestCorsOnUnhandledException:
    """Регрессия (bug 2026-09-07): 500 из глобального перехвата необработанных
    исключений должен нести те же CORS-заголовки, что и обычные ответы. До
    фикса `@app.exception_handler(Exception)` жил в ServerErrorMiddleware —
    самом внешнем слое стека, снаружи CORSMiddleware — и такой 500 уходил без
    Access-Control-Allow-Origin; браузер превращал его в сетевую ошибку
    ("Failed to fetch"/"Load failed") вместо показа кода ошибки клиенту."""

    BOOM_PATH = "/__test_unhandled_exception__"

    @pytest.fixture(autouse=True)
    def _boom_route(self):
        async def _boom() -> None:
            raise RuntimeError("boom")

        app.add_api_route(self.BOOM_PATH, _boom, methods=["GET"])
        try:
            yield
        finally:
            app.router.routes = [
                route
                for route in app.router.routes
                if getattr(route, "path", None) != self.BOOM_PATH
            ]

    async def test_allowed_origin_gets_cors_header_on_500(self, client: AsyncClient):
        resp = await client.get(self.BOOM_PATH, headers={"Origin": ALLOWED_ORIGIN})
        assert resp.status_code == 500
        assert resp.json() == {
            "data": None,
            "error": {
                "code": "ERROR",
                "message": "Внутренняя ошибка сервера",
                "validation": None,
            },
        }
        assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN

    async def test_disallowed_origin_gets_no_cors_header_on_500(self, client: AsyncClient):
        resp = await client.get(self.BOOM_PATH, headers={"Origin": DISALLOWED_ORIGIN})
        assert resp.status_code == 500
        assert resp.headers.get("access-control-allow-origin") != DISALLOWED_ORIGIN
