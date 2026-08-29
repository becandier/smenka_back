"""Тесты рендера HTML письма «Код подтверждения» (без похода в БД/HTTP).

Логотип раньше был встроен как `data:image/png;base64,...` — Gmail и часть
других клиентов блокируют `data:` URI в `<img>`, из-за чего логотип не
отображался (см. docs/tasks/... или коммит фикса). Теперь используется
внешняя ссылка на лендинг — тест закрепляет, что base64 не возвращается.
"""

from src.app.services.email_template import render_verification_code_html


def test_render_substitutes_placeholders() -> None:
    html = render_verification_code_html(code="4821", ttl_minutes=15)

    assert "4821" in html
    assert "15" in html
    assert "__VERIFICATION_CODE__" not in html
    assert "__VERIFICATION_TTL_MINUTES__" not in html


def test_render_logo_is_external_link_not_base64() -> None:
    html = render_verification_code_html(code="1234", ttl_minutes=10)

    assert "https://smenka.space/email/logo.png" in html
    assert "data:image" not in html
