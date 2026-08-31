"""Интеграция с ЮKassa API v3 (`online_payments`).

Аутентификация — HTTP Basic: `shopId` как логин, `secretKey` как пароль
(https://yookassa.ru/developers/api). Создание платежа обязано нести
заголовок `Idempotence-Key` (наш `payments.idempotence_key`) — повторный
запрос с тем же ключом возвращает исходный платёж вместо создания нового,
это защита от повторной отправки формы/сетевого ретрая на НАШЕЙ стороне
(отдельно от идемпотентности ПРИМЕНЕНИЯ платежа к подписке, которая держится
на `payments.applied_at`, см. `services/billing.py::apply_payment`).

`amount.value` в API ЮKassa — десятичная строка в рублях («500.00»), не
целые копейки — конвертация через `Decimal`, чтобы не словить ошибки
округления float на не круглых суммах (апгрейд может дать любую разницу).

Объект `receipt` в запросы не передаётся — чеки по 54-ФЗ/НПД сервис не
формирует (см. backend.md, «Границы»).

IP-адреса ЮKassa для вебхуков (`POST /billing/yookassa/webhook`) — официальный
список сетей из документации ЮKassa (раздел «Уведомления», проверка IP
источника), снят 2026-08-29. Список — не ENV-переменная: он публикуется
провайдером как часть контракта API, а не окружением конкретного деплоя;
обновляется правкой этого модуля при изменении списка провайдером.
"""

import ipaddress
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Any

import httpx

from src.app.core.config import Settings

API_BASE = "https://api.yookassa.ru/v3"
_REQUEST_TIMEOUT_SECONDS = 15

YOOKASSA_WEBHOOK_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("185.71.76.0/27"),
    ipaddress.ip_network("185.71.77.0/27"),
    ipaddress.ip_network("77.75.153.0/25"),
    ipaddress.ip_network("77.75.156.11/32"),
    ipaddress.ip_network("77.75.156.35/32"),
    ipaddress.ip_network("77.75.154.128/25"),
    ipaddress.ip_network("2a02:5180::/32"),
)


class YooKassaClientError(Exception):
    """Сетевая ошибка/нестандартный ответ ЮKassa при вызове API. Маппится
    вызывающим сервисным кодом (`services/billing.py`) в `PAYMENT_PROVIDER_ERROR`."""


def is_trusted_webhook_ip(ip: str) -> bool:
    """Источник вебхука входит в официальные сети ЮKassa. Невалидный IP
    (пустая строка/мусор) считается недоверенным, а не падает исключением."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in network for network in YOOKASSA_WEBHOOK_NETWORKS)


def minor_to_amount_value(amount_minor: int) -> str:
    """Копейки → десятичная строка ЮKassa («142500» → «1425.00»)."""
    return str((Decimal(amount_minor) / 100).quantize(Decimal("0.01")))


def amount_value_to_minor(value: str) -> int:
    """Десятичная строка ЮKassa → копейки. `ROUND_DOWN` — сверка суммы не
    должна округлить чужие копейки в нашу пользу."""
    return int((Decimal(value) * 100).to_integral_value(rounding=ROUND_DOWN))


async def create_payment(
    settings: Settings,
    *,
    idempotence_key: uuid.UUID,
    amount_minor: int,
    currency: str,
    description: str,
    return_url: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """`POST /v3/payments` — `capture: true` (одностадийный платёж, деньги
    списываются сразу по подтверждению картой), без `receipt`."""
    payload = {
        "amount": {"value": minor_to_amount_value(amount_minor), "currency": currency},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description,
        "metadata": metadata,
    }
    try:
        response = await _create_payment_request(settings, idempotence_key, payload)
    except httpx.HTTPError as exc:
        raise YooKassaClientError(f"ЮKassa HTTP error (create): {exc!r}") from exc
    if response.status_code not in (200, 201):
        raise YooKassaClientError(
            f"ЮKassa create_payment failed: status={response.status_code} body={response.text}"
        )
    return response.json()  # type: ignore[no-any-return]


async def get_payment(settings: Settings, provider_payment_id: str) -> dict[str, Any]:
    """`GET /v3/payments/{id}` — источник правды о состоянии платежа. Вебхук
    и поллинг статуса обязаны сверяться именно с этим ответом, а не с телом
    уведомления (ЮKassa не подписывает вебхуки)."""
    try:
        response = await _get_payment_request(settings, provider_payment_id)
    except httpx.HTTPError as exc:
        raise YooKassaClientError(f"ЮKassa HTTP error (get): {exc!r}") from exc
    if response.status_code != 200:
        raise YooKassaClientError(
            f"ЮKassa get_payment failed: status={response.status_code} body={response.text}"
        )
    return response.json()  # type: ignore[no-any-return]


async def _create_payment_request(
    settings: Settings,
    idempotence_key: uuid.UUID,
    payload: dict[str, Any],
) -> httpx.Response:
    """Изолирована для мокания в тестах (см. `email_sendpulse._send_email_request`)."""
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        return await client.post(
            f"{API_BASE}/payments",
            auth=(settings.yookassa_shop_id, settings.yookassa_secret_key),
            headers={"Idempotence-Key": str(idempotence_key)},
            json=payload,
        )


async def _get_payment_request(settings: Settings, provider_payment_id: str) -> httpx.Response:
    """Изолирована для мокания в тестах."""
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        return await client.get(
            f"{API_BASE}/payments/{provider_payment_id}",
            auth=(settings.yookassa_shop_id, settings.yookassa_secret_key),
        )
