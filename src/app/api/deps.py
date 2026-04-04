# src/app/api/deps.py
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.core.database import get_session
from src.app.core.security import ALGORITHM
from src.app.models.user import User
from src.app.services.auth import get_user_by_id

SessionDep = Annotated[AsyncSession, Depends(get_session)]

security_scheme = HTTPBearer()
settings = get_settings()


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security_scheme)],
) -> User:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        user_id: str | None = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="INVALID_TOKEN")
        if payload.get("type") == "refresh":
            raise HTTPException(status_code=401, detail="INVALID_TOKEN")
    except JWTError:
        raise HTTPException(status_code=401, detail="INVALID_TOKEN") from None

    user = await get_user_by_id(session, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="USER_NOT_FOUND")
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]
