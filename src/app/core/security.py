import re
import secrets
import string
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from jose import jwt
from passlib.context import CryptContext

from src.app.core.config import get_settings

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"

# Логин учётки, заведённой админом организации (admin_created_accounts):
# латиница/цифры/._-, 3-32 символа. Уникальность — глобальная, регистронезависимая
# (сравнение по lower(), см. модель User).
LOGIN_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")

# Символы, визуально неотличимые друг от друга (0/O/o, 1/l/I), исключены из
# сгенерированного пароля — чтобы его можно было продиктовать голосом.
_AMBIGUOUS_PASSWORD_CHARS = "0Oo1lI"  # noqa: S105 — не пароль, алфавит-исключение
_PASSWORD_ALPHABET = "".join(
    c for c in string.ascii_letters + string.digits if c not in _AMBIGUOUS_PASSWORD_CHARS
)
_PASSWORD_LETTERS = [c for c in _PASSWORD_ALPHABET if c.isalpha()]
_PASSWORD_DIGITS = [c for c in _PASSWORD_ALPHABET if c.isdigit()]
GENERATED_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return cast(str, pwd_context.hash(password))


def verify_password(plain: str, hashed: str) -> bool:
    return cast(bool, pwd_context.verify(plain, hashed))


def validate_password_strength(password: str) -> str:
    """Общие правила пароля (регистрация, admin_created_accounts): минимум 8
    символов, хотя бы одна буква и одна цифра. Raises ValueError — рассчитано на
    вызов из Pydantic field_validator."""
    if len(password) < 8:
        raise ValueError("Пароль должен быть не менее 8 символов")
    if not re.search(r"[a-zA-Zа-яА-ЯёЁ]", password):
        raise ValueError("Пароль должен содержать хотя бы одну букву")
    if not re.search(r"\d", password):
        raise ValueError("Пароль должен содержать хотя бы одну цифру")
    return password


def validate_login_format(raw: str) -> str:
    """Trim + формат логина (admin_created_accounts). Raises ValueError — рассчитано
    на вызов из Pydantic field_validator."""
    trimmed = raw.strip()
    if not LOGIN_PATTERN.match(trimmed):
        raise ValueError("Логин: 3-32 символа, латиница/цифры/точка/подчёркивание/дефис")
    return trimmed


def generate_password(length: int = GENERATED_PASSWORD_LENGTH) -> str:
    """Пароль по умолчанию для учётки, заведённой админом организации
    (admin_created_accounts). Без визуально неоднозначных символов; гарантированно
    содержит минимум одну букву и одну цифру — всегда проходит
    validate_password_strength."""
    chars = [secrets.choice(_PASSWORD_LETTERS), secrets.choice(_PASSWORD_DIGITS)]
    chars += [secrets.choice(_PASSWORD_ALPHABET) for _ in range(length - len(chars))]
    # Fisher-Yates на secrets.randbelow (криптостойкий) — иначе буква и цифра
    # всегда оказывались бы на первых двух позициях.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def create_access_token(subject: str) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    return cast(
        str, jwt.encode({"sub": subject, "exp": expire}, settings.secret_key, algorithm=ALGORITHM)
    )


def create_refresh_token(subject: str) -> str:
    expire = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
    return cast(
        str,
        jwt.encode(
            {"sub": subject, "exp": expire, "type": "refresh", "jti": str(uuid.uuid4())},
            settings.secret_key,
            algorithm=ALGORITHM,
        ),
    )
