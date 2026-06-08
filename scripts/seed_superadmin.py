"""Идемпотентно повышает существующего верифицированного пользователя до super_admin.

Запуск (из контейнера API, см. docs/DEPLOY_PLAYBOOK.md):

    python -m scripts.seed_superadmin <email>

Роль super_admin иначе ставится только вручную в БД — этот скрипт нужен для
первичной выдачи прав платформенному администратору. Без интерактивного ввода.

Коды выхода: 0 — успех или уже super_admin (идемпотентно); 1 — ошибка
(пользователь не найден или не верифицирован).
"""

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.core.config import get_settings
from src.app.models.user import User, UserRole


async def promote_to_super_admin(email: str) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            result = await session.execute(select(User).where(User.email == email))
            user = result.scalar_one_or_none()

            if user is None:
                print(f"[seed_superadmin] Пользователь с email {email!r} не найден.")
                return 1
            if not user.is_verified:
                print(
                    f"[seed_superadmin] Пользователь {email!r} не верифицирован — "
                    "повышение отклонено."
                )
                return 1
            if user.role == UserRole.super_admin:
                print(f"[seed_superadmin] {email!r} уже super_admin — изменений нет.")
                return 0

            user.role = UserRole.super_admin
            await session.commit()
            print(f"[seed_superadmin] {email!r} повышен до super_admin.")
            return 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Повысить верифицированного пользователя до super_admin."
    )
    parser.add_argument("email", help="Email существующего верифицированного пользователя")
    args = parser.parse_args()
    sys.exit(asyncio.run(promote_to_super_admin(args.email)))


if __name__ == "__main__":
    main()
