from datetime import UTC, datetime

from sqlalchemy import delete

from src.app.core.celery_app import celery_app
from src.app.core.database import get_sync_session
from src.app.core.logging import get_logger
from src.app.models.user import RefreshToken, VerificationCode

logger = get_logger(__name__)


@celery_app.task(name="cleanup_expired_tokens")
def cleanup_expired_tokens() -> None:
    """Delete expired/revoked refresh tokens and expired verification codes."""
    with get_sync_session() as session:
        now = datetime.now(UTC)

        # Delete expired or revoked refresh tokens
        tokens_result = session.execute(
            delete(RefreshToken).where(
                (RefreshToken.expires_at < now) | (RefreshToken.revoked.is_(True))
            )
        )
        tokens_deleted = tokens_result.rowcount

        # Delete expired verification codes
        codes_result = session.execute(
            delete(VerificationCode).where(VerificationCode.expires_at < now)
        )
        codes_deleted = codes_result.rowcount

        if tokens_deleted > 0 or codes_deleted > 0:
            logger.info(
                "expired_data_cleaned",
                tokens_deleted=tokens_deleted,
                codes_deleted=codes_deleted,
            )
