"""Рендер письма «Код подтверждения» — общий для любого провайдера, которому
нужно готовое тело письма в запросе (в отличие от Loops, где шаблон жил в
вебе провайдера и адресовался по id).

Исходник вёрстки — `docs/email-templates/verification-code/index.mjml` (правки
вносить там). `templates/verification_code_email.html` — уже собранный HTML
той же вёрстки: логотип встроен как `data:image/png;base64,...` (само письмо
не зависит от внешнего хостинга картинки — то, ради чего шаблон и переехал в
репозиторий), а плейсхолдеры `__VERIFICATION_CODE__` /
`__VERIFICATION_TTL_MINUTES__` подставляются здесь простой заменой строк (не
`str.format` — в CSS `<mj-style>` полно фигурных скобок). Инструкция по
пересборке HTML из mjml — в шапке самого файла шаблона.
"""

from functools import lru_cache
from pathlib import Path

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "verification_code_email.html"
_CODE_PLACEHOLDER = "__VERIFICATION_CODE__"
_TTL_PLACEHOLDER = "__VERIFICATION_TTL_MINUTES__"

SUBJECT = "Код подтверждения Smenka"


@lru_cache(maxsize=1)
def _load_template() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def render_verification_code_html(code: str, ttl_minutes: int) -> str:
    return _load_template().replace(_CODE_PLACEHOLDER, code).replace(
        _TTL_PLACEHOLDER, str(ttl_minutes)
    )


def render_verification_code_text(code: str, ttl_minutes: int) -> str:
    """Текстовая версия — обязательное поле у SendPulse (`email.text`), заодно
    fallback для клиентов без HTML."""
    return (
        f"Код подтверждения: {code}\n"
        f"Код действует {ttl_minutes} минут.\n"
        "Если вы не запрашивали код — просто проигнорируйте это письмо."
    )
