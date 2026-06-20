from datetime import datetime

from pydantic import BaseModel, Field


class FileResponse(BaseModel):
    """Метаданные файла + свежий presigned GET URL.

    `url` короткоживущий (TTL = S3_PRESIGN_EXPIRE_SECONDS); протух — обновляется
    через `GET /api/v1/files/{id}`."""

    id: str
    category: str
    original_filename: str
    content_type: str
    size_bytes: int
    url: str = Field(description="Presigned GET URL для прямого скачивания из storage")
    url_expires_at: datetime
    created_at: datetime
