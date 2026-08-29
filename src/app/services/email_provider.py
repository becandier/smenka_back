"""Граница между `services/email.py` и конкретным email-провайдером.

Ровно ради этого файла и заводился сменный транспорт (третий провайдер
подряд: Яндекс-SMTP → Loops → SendPulse, оба предыдущих отвалились не по
нашей вине). Новый провайдер реализует `EmailProvider` в своём файле
(`email_<provider>.py`) и поднимает `EmailDeliveryError` при сбое — `email.py`
и его вызывающие (`api/v1/auth.py`) при этом не меняются."""

from typing import Protocol


class EmailDeliveryError(Exception):
    """Провайдер не смог доставить письмо (сеть, таймаут, отказ API, сбой токена).

    Сообщение не должно содержать код подтверждения — `email.py` логирует
    `repr(exc)` при сбое отправки."""


class EmailProvider(Protocol):
    async def send_verification_code(self, *, to_email: str, code: str, ttl_minutes: int) -> None:
        """Отправить код подтверждения. При сбое поднимает `EmailDeliveryError`."""
        ...
