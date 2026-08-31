import json

from fastapi import APIRouter, Request, Response

from src.app.api.deps import SessionDep
from src.app.core.config import get_settings
from src.app.services import billing as billing_service
from src.app.services import yookassa_client
from src.app.services.common import AccessError
from src.app.utils.request import get_client_ip

router = APIRouter(tags=["billing-webhook"])
settings = get_settings()


@router.post(
    "/billing/yookassa/webhook",
    summary="Уведомления ЮKassa (публичный эндпоинт, без JWT)",
    description=(
        "События payment.succeeded / payment.canceled / refund.succeeded. ЮKassa не "
        "подписывает уведомления — доверять телу запроса нельзя: IP источника сверяется со "
        "списком официальных сетей ЮKassa ДО разбора тела (чужой IP → 403, тело не "
        "разбирается), затем состояние платежа перезапрашивается напрямую у провайдера "
        "(GET /v3/payments/{id}) и работа идёт только с этим ответом. Всегда 200 с пустым "
        "телом при успешной обработке; 4xx/5xx — только когда доставку стоит повторить."
    ),
)
async def yookassa_webhook(request: Request, session: SessionDep) -> Response:
    client_ip = get_client_ip(request)
    if not yookassa_client.is_trusted_webhook_ip(client_ip):
        # Тело запроса намеренно не читается — недоверенный IP отсекается раньше.
        raise AccessError("FORBIDDEN", "IP источника не входит в список сетей ЮKassa", 403)

    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise billing_service.PaymentError(
            "VALIDATION_ERROR", "Некорректное тело вебхука (невалидный JSON)", 400
        ) from exc
    if not isinstance(body, dict):
        raise billing_service.PaymentError("VALIDATION_ERROR", "Некорректное тело вебхука", 400)

    await billing_service.process_webhook_event(session, settings, body)
    return Response(status_code=200)
