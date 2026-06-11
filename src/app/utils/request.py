"""Утилиты разбора HTTP-запроса."""

from fastapi import Request


def get_client_ip(request: Request) -> str:
    """IP клиента для rate-limit и аудита.

    За обратным прокси (Caddy) реальный адрес клиента — первый в
    `X-Forwarded-For` (Caddy сам перезаписывает заголовок, поэтому ему доверяем).
    В dev без прокси берём `request.client.host`.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request.client is not None:
        return request.client.host
    return "unknown"
