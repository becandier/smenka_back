# Phase 2 — Personal Shifts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement personal shift tracking — start/pause/resume/finish shifts, list with pagination and filters, statistics by period, auto-finish stale shifts.

**Architecture:** Same three-layer approach as Phase 1: ORM models → service layer → FastAPI endpoints. `ShiftError` exception for business rule violations (analogous to `AuthError`). Auto-finish runs synchronously on shift-related requests. Stats calculated via SQL aggregation. No pause limits in personal mode (limits come in Phase 4).

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, asyncpg, Pydantic v2, pytest + httpx

---

## File Structure

### New files

| File | Responsibility |
|------|---------------|
| `src/app/models/shift.py` | `Shift`, `Pause` ORM models, `ShiftStatus` enum |
| `src/app/schemas/shift.py` | Shift request/response schemas |
| `src/app/services/shift.py` | Shift business logic + `ShiftError` exception |
| `src/app/api/v1/shifts.py` | Shift endpoints router |
| `tests/test_shifts.py` | Shift lifecycle + edge case tests |

### Modified files

| File | Changes |
|------|---------|
| `src/app/models/user.py` | Add `shifts` relationship to `User` |
| `src/app/models/__init__.py` | Export `Shift`, `Pause` |
| `src/app/api/v1/router.py` | Include shifts router |
| `src/app/main.py` | Add `ShiftError` exception handler |

---

### Task 1: Models — Shift, Pause, ShiftStatus

**Files:**
- Create: `src/app/models/shift.py`
- Modify: `src/app/models/user.py`
- Modify: `src/app/models/__init__.py`

- [ ] **Step 1: Create shift models file**

```python
# src/app/models/shift.py
import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.core.database import Base


class ShiftStatus(str, enum.Enum):
    active = "active"
    paused = "paused"
    finished = "finished"


class Shift(Base):
    __tablename__ = "shifts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    status: Mapped[ShiftStatus] = mapped_column(
        Enum(ShiftStatus),
        default=ShiftStatus.active,
    )

    user: Mapped["User"] = relationship(back_populates="shifts")
    pauses: Mapped[list["Pause"]] = relationship(
        back_populates="shift",
        cascade="all, delete-orphan",
        order_by="Pause.started_at",
    )


class Pause(Base):
    __tablename__ = "pauses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    shift_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shifts.id", ondelete="CASCADE"),
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    shift: Mapped["Shift"] = relationship(back_populates="pauses")
```

- [ ] **Step 2: Add `shifts` relationship to User model**

In `src/app/models/user.py`, add after the `verification_codes` relationship:

```python
    shifts: Mapped[list["Shift"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
```

- [ ] **Step 3: Export new models in `__init__.py`**

Replace `src/app/models/__init__.py` with:

```python
from src.app.models.shift import Pause, Shift
from src.app.models.user import RefreshToken, User, VerificationCode

__all__ = ["User", "RefreshToken", "VerificationCode", "Shift", "Pause"]
```

- [ ] **Step 4: Verify imports work**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -c "from src.app.models import Shift, Pause; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add src/app/models/shift.py src/app/models/user.py src/app/models/__init__.py
git commit -m "feat: add Shift and Pause models with ShiftStatus enum"
```

---

### Task 2: Schemas

**Files:**
- Create: `src/app/schemas/shift.py`

- [ ] **Step 1: Create shift schemas**

```python
# src/app/schemas/shift.py
from datetime import datetime

from pydantic import BaseModel


class PauseResponse(BaseModel):
    id: str
    shift_id: str
    started_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class ShiftResponse(BaseModel):
    id: str
    user_id: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    pauses: list[PauseResponse]
    worked_seconds: int

    model_config = {"from_attributes": True}


class ShiftListResponse(BaseModel):
    items: list[ShiftResponse]
    total: int
    limit: int
    offset: int


class ShiftStatsResponse(BaseModel):
    period: str
    total_worked_seconds: int
    shift_count: int
    average_shift_seconds: int
```

- [ ] **Step 2: Commit**

```bash
git add src/app/schemas/shift.py
git commit -m "feat: add shift Pydantic schemas"
```

---

### Task 3: Service skeleton + ShiftError + wiring

**Files:**
- Create: `src/app/services/shift.py`
- Create: `src/app/api/v1/shifts.py`
- Modify: `src/app/api/v1/router.py`
- Modify: `src/app/main.py`

- [ ] **Step 1: Create shift service with ShiftError and helper**

```python
# src/app/services/shift.py
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.config import get_settings
from src.app.models.shift import Pause, Shift, ShiftStatus

settings = get_settings()


class ShiftError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


def calculate_worked_seconds(shift: Shift) -> int:
    """Calculate total worked seconds for a shift (total duration minus pauses)."""
    now = datetime.now(UTC)
    end = shift.finished_at or now

    total = (end - shift.started_at).total_seconds()

    for pause in shift.pauses:
        pause_end = pause.finished_at or now
        total -= (pause_end - pause.started_at).total_seconds()

    return max(0, int(total))


async def _get_shift_with_pauses(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Shift:
    """Load shift with pauses, verify ownership."""
    result = await session.execute(
        select(Shift)
        .options(selectinload(Shift.pauses))
        .where(Shift.id == shift_id, Shift.user_id == user_id)
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        raise ShiftError("SHIFT_NOT_FOUND", "Смена не найдена", 404)
    return shift


async def _auto_finish_stale_shifts(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> None:
    """Auto-finish shifts that exceeded the timeout (default 16h)."""
    timeout_hours = settings.default_auto_finish_hours
    cutoff = datetime.now(UTC) - __import__("datetime").timedelta(hours=timeout_hours)

    result = await session.execute(
        select(Shift)
        .options(selectinload(Shift.pauses))
        .where(
            Shift.user_id == user_id,
            Shift.status.in_([ShiftStatus.active, ShiftStatus.paused]),
            Shift.started_at < cutoff,
        )
    )
    stale_shifts = result.scalars().all()

    for shift in stale_shifts:
        # Close active pause if any
        for pause in shift.pauses:
            if pause.finished_at is None:
                pause.finished_at = datetime.now(UTC)
        shift.status = ShiftStatus.finished
        shift.finished_at = datetime.now(UTC)

    if stale_shifts:
        await session.flush()
```

- [ ] **Step 2: Create shifts router stub**

```python
# src/app/api/v1/shifts.py
from fastapi import APIRouter

router = APIRouter(prefix="/shifts", tags=["shifts"])
```

- [ ] **Step 3: Wire shifts router into v1 router**

In `src/app/api/v1/router.py`, add:

```python
from src.app.api.v1.shifts import router as shifts_router
```

And add at the end:

```python
router.include_router(shifts_router)
```

- [ ] **Step 4: Add ShiftError exception handler in main.py**

In `src/app/main.py`, add import:

```python
from src.app.services.shift import ShiftError
```

Add handler after the `AuthError` handler:

```python
@app.exception_handler(ShiftError)
async def shift_error_handler(request: Request, exc: ShiftError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(exc.code, exc.message).model_dump(),
    )
```

- [ ] **Step 5: Verify app starts**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -c "from src.app.main import app; print('OK')"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add src/app/services/shift.py src/app/api/v1/shifts.py src/app/api/v1/router.py src/app/main.py
git commit -m "feat: add shift service skeleton, router, and ShiftError handler"
```

---

### Task 4: POST /shifts/start — test + implement

**Files:**
- Modify: `src/app/services/shift.py`
- Modify: `src/app/api/v1/shifts.py`
- Create: `tests/test_shifts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shifts.py
from httpx import AsyncClient


class TestStartShift:
    async def test_start_shift_success(self, client: AsyncClient, auth_headers):
        response = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "active"
        assert data["pauses"] == []
        assert data["worked_seconds"] >= 0
        assert data["finished_at"] is None

    async def test_start_shift_already_active(self, client: AsyncClient, auth_headers):
        await client.post("/api/v1/shifts/start", headers=auth_headers)
        response = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SHIFT_ALREADY_ACTIVE"

    async def test_start_shift_unauthorized(self, client: AsyncClient):
        response = await client.post("/api/v1/shifts/start")
        assert response.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py::TestStartShift -v`
Expected: FAIL (endpoints not implemented yet)

- [ ] **Step 3: Implement `start_shift` in service**

Add to `src/app/services/shift.py`:

```python
async def start_shift(session: AsyncSession, user_id: uuid.UUID) -> Shift:
    """Start a new shift. Only one active/paused shift allowed at a time."""
    await _auto_finish_stale_shifts(session, user_id)

    result = await session.execute(
        select(Shift).where(
            Shift.user_id == user_id,
            Shift.status.in_([ShiftStatus.active, ShiftStatus.paused]),
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        raise ShiftError(
            "SHIFT_ALREADY_ACTIVE",
            "У вас уже есть активная смена",
            409,
        )

    shift = Shift(user_id=user_id)
    session.add(shift)
    await session.flush()

    # Reload with pauses relationship
    return await _get_shift_with_pauses(session, shift.id, user_id)
```

- [ ] **Step 4: Add endpoint in shifts router**

Replace `src/app/api/v1/shifts.py` with:

```python
# src/app/api/v1/shifts.py
from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.shift import ShiftResponse
from src.app.services import shift as shift_service
from src.app.services.shift import calculate_worked_seconds

router = APIRouter(prefix="/shifts", tags=["shifts"])


def _shift_to_response(shift) -> dict:
    return ShiftResponse(
        id=str(shift.id),
        user_id=str(shift.user_id),
        started_at=shift.started_at,
        finished_at=shift.finished_at,
        status=shift.status.value,
        pauses=[
            {
                "id": str(p.id),
                "shift_id": str(p.shift_id),
                "started_at": p.started_at,
                "finished_at": p.finished_at,
            }
            for p in shift.pauses
        ],
        worked_seconds=calculate_worked_seconds(shift),
    ).model_dump(mode="json")


@router.post("/start", status_code=201)
async def start_shift(
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await shift_service.start_shift(session, user.id)
    await session.commit()
    return ApiResponse.success(_shift_to_response(shift))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py::TestStartShift -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add src/app/services/shift.py src/app/api/v1/shifts.py tests/test_shifts.py
git commit -m "feat: implement POST /shifts/start with duplicate check"
```

---

### Task 5: POST /shifts/{id}/pause — test + implement

**Files:**
- Modify: `tests/test_shifts.py`
- Modify: `src/app/services/shift.py`
- Modify: `src/app/api/v1/shifts.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_shifts.py`:

```python
class TestPauseShift:
    async def test_pause_active_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        response = await client.post(
            f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "paused"
        assert len(data["pauses"]) == 1
        assert data["pauses"][0]["finished_at"] is None

    async def test_pause_already_paused(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        response = await client.post(
            f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SHIFT_NOT_ACTIVE"

    async def test_pause_finished_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        response = await client.post(
            f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SHIFT_NOT_ACTIVE"

    async def test_pause_not_own_shift(self, client: AsyncClient, auth_headers):
        response = await client.post(
            "/api/v1/shifts/00000000-0000-0000-0000-000000000000/pause",
            headers=auth_headers,
        )
        assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py::TestPauseShift -v`
Expected: FAIL

- [ ] **Step 3: Implement `pause_shift` in service**

Add to `src/app/services/shift.py`:

```python
async def pause_shift(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Shift:
    """Pause an active shift."""
    shift = await _get_shift_with_pauses(session, shift_id, user_id)

    if shift.status != ShiftStatus.active:
        raise ShiftError("SHIFT_NOT_ACTIVE", "Смена не активна", 400)

    pause = Pause(shift_id=shift.id)
    session.add(pause)
    shift.status = ShiftStatus.paused
    await session.flush()

    return await _get_shift_with_pauses(session, shift.id, user_id)
```

- [ ] **Step 4: Add pause endpoint**

Add to `src/app/api/v1/shifts.py`:

```python
@router.post("/{shift_id}/pause")
async def pause_shift(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await shift_service.pause_shift(session, shift_id, user.id)
    await session.commit()
    return ApiResponse.success(_shift_to_response(shift))
```

Add `import uuid` at the top of the file.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py::TestPauseShift -v`
Expected: 4 passed

Note: `test_pause_finished_shift` requires `finish_shift` — implement that endpoint stub temporarily (return 501) or implement Task 7 first. Alternative: reorder so `finish` comes before edge-case tests. The test depends on `/finish` working. If running tests incrementally, skip `test_pause_finished_shift` until Task 7 is done, then re-run.

- [ ] **Step 6: Commit**

```bash
git add src/app/services/shift.py src/app/api/v1/shifts.py tests/test_shifts.py
git commit -m "feat: implement POST /shifts/{id}/pause"
```

---

### Task 6: POST /shifts/{id}/resume — test + implement

**Files:**
- Modify: `tests/test_shifts.py`
- Modify: `src/app/services/shift.py`
- Modify: `src/app/api/v1/shifts.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_shifts.py`:

```python
class TestResumeShift:
    async def test_resume_paused_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        response = await client.post(
            f"/api/v1/shifts/{shift_id}/resume", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "active"
        assert len(data["pauses"]) == 1
        assert data["pauses"][0]["finished_at"] is not None

    async def test_resume_active_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        response = await client.post(
            f"/api/v1/shifts/{shift_id}/resume", headers=auth_headers
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SHIFT_NOT_PAUSED"

    async def test_resume_not_own_shift(self, client: AsyncClient, auth_headers):
        response = await client.post(
            "/api/v1/shifts/00000000-0000-0000-0000-000000000000/resume",
            headers=auth_headers,
        )
        assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py::TestResumeShift -v`
Expected: FAIL

- [ ] **Step 3: Implement `resume_shift` in service**

Add to `src/app/services/shift.py`:

```python
async def resume_shift(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Shift:
    """Resume a paused shift."""
    shift = await _get_shift_with_pauses(session, shift_id, user_id)

    if shift.status != ShiftStatus.paused:
        raise ShiftError("SHIFT_NOT_PAUSED", "Смена не на паузе", 400)

    # Close the active pause
    for pause in shift.pauses:
        if pause.finished_at is None:
            pause.finished_at = datetime.now(UTC)
            break

    shift.status = ShiftStatus.active
    await session.flush()

    return await _get_shift_with_pauses(session, shift.id, user_id)
```

- [ ] **Step 4: Add resume endpoint**

Add to `src/app/api/v1/shifts.py`:

```python
@router.post("/{shift_id}/resume")
async def resume_shift(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await shift_service.resume_shift(session, shift_id, user.id)
    await session.commit()
    return ApiResponse.success(_shift_to_response(shift))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py::TestResumeShift -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add src/app/services/shift.py src/app/api/v1/shifts.py tests/test_shifts.py
git commit -m "feat: implement POST /shifts/{id}/resume"
```

---

### Task 7: POST /shifts/{id}/finish — test + implement

**Files:**
- Modify: `tests/test_shifts.py`
- Modify: `src/app/services/shift.py`
- Modify: `src/app/api/v1/shifts.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_shifts.py`:

```python
class TestFinishShift:
    async def test_finish_active_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        response = await client.post(
            f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "finished"
        assert data["finished_at"] is not None

    async def test_finish_paused_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        response = await client.post(
            f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "finished"
        # Pause should be closed
        assert data["pauses"][0]["finished_at"] is not None

    async def test_finish_already_finished(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        response = await client.post(
            f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SHIFT_ALREADY_FINISHED"

    async def test_finish_not_own_shift(self, client: AsyncClient, auth_headers):
        response = await client.post(
            "/api/v1/shifts/00000000-0000-0000-0000-000000000000/finish",
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_can_start_new_shift_after_finish(
        self, client: AsyncClient, auth_headers
    ):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)

        response = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert response.status_code == 201
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py::TestFinishShift -v`
Expected: FAIL

- [ ] **Step 3: Implement `finish_shift` in service**

Add to `src/app/services/shift.py`:

```python
async def finish_shift(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Shift:
    """Finish an active or paused shift."""
    shift = await _get_shift_with_pauses(session, shift_id, user_id)

    if shift.status == ShiftStatus.finished:
        raise ShiftError("SHIFT_ALREADY_FINISHED", "Смена уже завершена", 400)

    # Close active pause if any
    for pause in shift.pauses:
        if pause.finished_at is None:
            pause.finished_at = datetime.now(UTC)

    shift.status = ShiftStatus.finished
    shift.finished_at = datetime.now(UTC)
    await session.flush()

    return await _get_shift_with_pauses(session, shift.id, user_id)
```

- [ ] **Step 4: Add finish endpoint**

Add to `src/app/api/v1/shifts.py`:

```python
@router.post("/{shift_id}/finish")
async def finish_shift(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await shift_service.finish_shift(session, shift_id, user.id)
    await session.commit()
    return ApiResponse.success(_shift_to_response(shift))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py -v`
Expected: all passed (including `test_pause_finished_shift` from Task 5)

- [ ] **Step 6: Commit**

```bash
git add src/app/services/shift.py src/app/api/v1/shifts.py tests/test_shifts.py
git commit -m "feat: implement POST /shifts/{id}/finish"
```

---

### Task 8: Auto-finish stale shifts — test

**Files:**
- Modify: `tests/test_shifts.py`

- [ ] **Step 1: Write the auto-finish test**

Add to `tests/test_shifts.py`:

```python
from datetime import UTC, datetime, timedelta
from unittest.mock import patch


class TestAutoFinish:
    async def test_stale_shift_auto_finished_on_start(
        self, client: AsyncClient, auth_headers, db_session
    ):
        """A shift older than 16h should be auto-finished when starting a new one."""
        from src.app.models.shift import Shift, ShiftStatus

        # Get user_id from /me
        me_resp = await client.get("/api/v1/users/me", headers=auth_headers)
        user_id = me_resp.json()["data"]["id"]

        # Create a stale shift directly in DB (started 17h ago)
        stale_shift = Shift(
            user_id=user_id,
            started_at=datetime.now(UTC) - timedelta(hours=17),
            status=ShiftStatus.active,
        )
        db_session.add(stale_shift)
        await db_session.commit()

        # Starting a new shift should auto-finish the stale one
        response = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert response.status_code == 201

        # Verify stale shift is finished
        list_resp = await client.get("/api/v1/shifts", headers=auth_headers)
        shifts = list_resp.json()["data"]["items"]
        finished_shifts = [s for s in shifts if s["status"] == "finished"]
        assert len(finished_shifts) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py::TestAutoFinish -v`
Expected: FAIL (GET /shifts not implemented yet — will pass after Task 9)

- [ ] **Step 3: Commit**

```bash
git add tests/test_shifts.py
git commit -m "test: add auto-finish stale shifts test"
```

---

### Task 9: GET /shifts — list with pagination and filters

**Files:**
- Modify: `tests/test_shifts.py`
- Modify: `src/app/services/shift.py`
- Modify: `src/app/api/v1/shifts.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_shifts.py`:

```python
class TestListShifts:
    async def test_list_shifts_empty(self, client: AsyncClient, auth_headers):
        response = await client.get("/api/v1/shifts", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0

    async def test_list_shifts_with_data(self, client: AsyncClient, auth_headers):
        # Create and finish a shift
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]
        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)

        # Create another active shift
        await client.post("/api/v1/shifts/start", headers=auth_headers)

        response = await client.get("/api/v1/shifts", headers=auth_headers)
        data = response.json()["data"]
        assert data["total"] == 2
        assert len(data["items"]) == 2
        # Most recent first
        assert data["items"][0]["status"] == "active"
        assert data["items"][1]["status"] == "finished"

    async def test_list_shifts_filter_by_status(
        self, client: AsyncClient, auth_headers
    ):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]
        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        await client.post("/api/v1/shifts/start", headers=auth_headers)

        response = await client.get(
            "/api/v1/shifts", headers=auth_headers, params={"status": "finished"}
        )
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["status"] == "finished"

    async def test_list_shifts_pagination(self, client: AsyncClient, auth_headers):
        # Create 3 shifts
        for _ in range(3):
            start_resp = await client.post(
                "/api/v1/shifts/start", headers=auth_headers
            )
            shift_id = start_resp.json()["data"]["id"]
            await client.post(
                f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers
            )

        response = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"limit": 2, "offset": 0},
        )
        data = response.json()["data"]
        assert data["total"] == 3
        assert len(data["items"]) == 2
        assert data["limit"] == 2
        assert data["offset"] == 0

    async def test_list_shifts_unauthorized(self, client: AsyncClient):
        response = await client.get("/api/v1/shifts")
        assert response.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py::TestListShifts -v`
Expected: FAIL

- [ ] **Step 3: Implement `get_shifts` in service**

Add to `src/app/services/shift.py`:

```python
async def get_shifts(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: ShiftStatus | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Shift], int]:
    """Get paginated shift list with optional filters. Returns (shifts, total_count)."""
    await _auto_finish_stale_shifts(session, user_id)

    conditions = [Shift.user_id == user_id]

    if status is not None:
        conditions.append(Shift.status == status)
    if date_from is not None:
        conditions.append(Shift.started_at >= date_from)
    if date_to is not None:
        conditions.append(Shift.started_at <= date_to)

    # Count total
    count_query = select(func.count()).select_from(Shift).where(*conditions)
    total = (await session.execute(count_query)).scalar_one()

    # Fetch page
    query = (
        select(Shift)
        .options(selectinload(Shift.pauses))
        .where(*conditions)
        .order_by(Shift.started_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    shifts = list(result.scalars().all())

    return shifts, total
```

- [ ] **Step 4: Add list endpoint**

Add to `src/app/api/v1/shifts.py`:

```python
from datetime import datetime

from fastapi import Query

from src.app.schemas.shift import ShiftListResponse


@router.get("")
async def list_shifts(
    user: CurrentUserDep,
    session: SessionDep,
    status: str | None = Query(None, description="Filter by status: active, paused, finished"),
    date_from: datetime | None = Query(None, description="Filter shifts started after this datetime"),
    date_to: datetime | None = Query(None, description="Filter shifts started before this datetime"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    from src.app.models.shift import ShiftStatus

    status_enum = None
    if status is not None:
        try:
            status_enum = ShiftStatus(status)
        except ValueError:
            pass

    shifts, total = await shift_service.get_shifts(
        session,
        user.id,
        status=status_enum,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    await session.commit()
    return ApiResponse.success(
        ShiftListResponse(
            items=[_shift_to_response(s) for s in shifts],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py::TestListShifts tests/test_shifts.py::TestAutoFinish -v`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add src/app/services/shift.py src/app/api/v1/shifts.py tests/test_shifts.py
git commit -m "feat: implement GET /shifts with pagination and filters"
```

---

### Task 10: GET /shifts/stats — statistics by period

**Files:**
- Modify: `tests/test_shifts.py`
- Modify: `src/app/services/shift.py`
- Modify: `src/app/api/v1/shifts.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_shifts.py`:

```python
class TestShiftStats:
    async def test_stats_empty(self, client: AsyncClient, auth_headers):
        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "day"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["period"] == "day"
        assert data["total_worked_seconds"] == 0
        assert data["shift_count"] == 0
        assert data["average_shift_seconds"] == 0

    async def test_stats_with_finished_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]
        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)

        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "day"},
        )
        data = response.json()["data"]
        assert data["shift_count"] == 1
        assert data["total_worked_seconds"] >= 0
        assert data["average_shift_seconds"] >= 0

    async def test_stats_includes_active_shift(
        self, client: AsyncClient, auth_headers
    ):
        """Active shifts should be counted in stats (using current time as end)."""
        await client.post("/api/v1/shifts/start", headers=auth_headers)

        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "day"},
        )
        data = response.json()["data"]
        assert data["shift_count"] == 1

    async def test_stats_invalid_period(self, client: AsyncClient, auth_headers):
        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "year"},
        )
        assert response.status_code == 400

    async def test_stats_unauthorized(self, client: AsyncClient):
        response = await client.get(
            "/api/v1/shifts/stats", params={"period": "day"}
        )
        assert response.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py::TestShiftStats -v`
Expected: FAIL

- [ ] **Step 3: Implement `get_shift_stats` in service**

Add to `src/app/services/shift.py`:

```python
from datetime import timedelta


VALID_PERIODS = {"day", "week", "month"}


async def get_shift_stats(
    session: AsyncSession,
    user_id: uuid.UUID,
    period: str,
) -> dict:
    """Calculate shift statistics for the given period."""
    if period not in VALID_PERIODS:
        raise ShiftError("INVALID_PERIOD", f"Период должен быть: {', '.join(VALID_PERIODS)}", 400)

    await _auto_finish_stale_shifts(session, user_id)

    now = datetime.now(UTC)
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:  # month
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    result = await session.execute(
        select(Shift)
        .options(selectinload(Shift.pauses))
        .where(
            Shift.user_id == user_id,
            Shift.started_at >= start,
        )
    )
    shifts = list(result.scalars().all())

    total_seconds = sum(calculate_worked_seconds(s) for s in shifts)
    count = len(shifts)
    avg = total_seconds // count if count > 0 else 0

    return {
        "period": period,
        "total_worked_seconds": total_seconds,
        "shift_count": count,
        "average_shift_seconds": avg,
    }
```

- [ ] **Step 4: Add stats endpoint**

Add to `src/app/api/v1/shifts.py` (must be defined **before** the `/{shift_id}` routes to avoid path conflicts):

```python
from src.app.schemas.shift import ShiftStatsResponse


@router.get("/stats")
async def shift_stats(
    user: CurrentUserDep,
    session: SessionDep,
    period: str = Query(..., description="Period: day, week, month"),
) -> ApiResponse:
    stats = await shift_service.get_shift_stats(session, user.id, period)
    await session.commit()
    return ApiResponse.success(
        ShiftStatsResponse(**stats).model_dump()
    )
```

**Important:** Place `/stats` endpoint **above** `/{shift_id}/pause`, `/{shift_id}/resume`, `/{shift_id}/finish` routes in the file, otherwise FastAPI will try to parse "stats" as a UUID and return 422.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py::TestShiftStats -v`
Expected: 5 passed

- [ ] **Step 6: Run all shift tests**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py -v`
Expected: all passed

- [ ] **Step 7: Commit**

```bash
git add src/app/services/shift.py src/app/api/v1/shifts.py tests/test_shifts.py
git commit -m "feat: implement GET /shifts/stats with day/week/month periods"
```

---

### Task 11: Full lifecycle integration test

**Files:**
- Modify: `tests/test_shifts.py`

- [ ] **Step 1: Write full lifecycle test**

Add to `tests/test_shifts.py`:

```python
class TestShiftLifecycle:
    async def test_full_lifecycle(self, client: AsyncClient, auth_headers):
        """Start → pause → resume → pause → finish — full cycle."""
        # Start
        resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert resp.status_code == 201
        shift_id = resp.json()["data"]["id"]

        # Pause
        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers
        )
        assert resp.json()["data"]["status"] == "paused"

        # Resume
        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/resume", headers=auth_headers
        )
        assert resp.json()["data"]["status"] == "active"
        assert resp.json()["data"]["pauses"][0]["finished_at"] is not None

        # Pause again
        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers
        )
        assert resp.json()["data"]["status"] == "paused"
        assert len(resp.json()["data"]["pauses"]) == 2

        # Finish (while paused — should close active pause)
        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers
        )
        data = resp.json()["data"]
        assert data["status"] == "finished"
        assert data["finished_at"] is not None
        assert all(p["finished_at"] is not None for p in data["pauses"])
        assert data["worked_seconds"] >= 0

    async def test_multiple_pauses_tracked(self, client: AsyncClient, auth_headers):
        """Multiple pause/resume cycles should all be tracked."""
        resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = resp.json()["data"]["id"]

        for _ in range(3):
            await client.post(
                f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers
            )
            await client.post(
                f"/api/v1/shifts/{shift_id}/resume", headers=auth_headers
            )

        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers
        )
        assert len(resp.json()["data"]["pauses"]) == 3
```

- [ ] **Step 2: Run tests**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/test_shifts.py -v`
Expected: all passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_shifts.py
git commit -m "test: add full shift lifecycle integration tests"
```

---

### Task 12: Alembic migration

**Files:**
- New migration file (auto-generated)

- [ ] **Step 1: Generate migration**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back/src && alembic revision --autogenerate -m "add shifts and pauses tables"`

- [ ] **Step 2: Review the generated migration**

Open the generated file in `src/migrations/versions/` and verify it creates:
- Table `shifts` with columns: id, user_id, started_at, finished_at, status
- Table `pauses` with columns: id, shift_id, started_at, finished_at
- Indexes on `shifts.user_id` and `pauses.shift_id`
- Foreign keys with CASCADE delete
- `shiftstatus` enum type

- [ ] **Step 3: Run migration against dev DB**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back/src && alembic upgrade head`
Expected: migration applies successfully

- [ ] **Step 4: Run all tests (existing + new)**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -m pytest tests/ -v`
Expected: all tests pass (auth + users + shifts)

- [ ] **Step 5: Commit**

```bash
git add src/migrations/versions/
git commit -m "migration: add shifts and pauses tables"
```

---

### Task 13: Update documentation

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/ROADMAP.md`

- [ ] **Step 1: Update ARCHITECTURE.md**

Add to the Models table:

```markdown
| `Shift` | `shifts` | Рабочая смена (user_id, started_at, finished_at, status) |
| `Pause` | `pauses` | Пауза внутри смены (shift_id, started_at, finished_at) |
```

Add to the Endpoints table:

```markdown
| POST | `/api/v1/shifts/start` | Начать смену | Bearer |
| POST | `/api/v1/shifts/{id}/pause` | Поставить на паузу | Bearer |
| POST | `/api/v1/shifts/{id}/resume` | Возобновить | Bearer |
| POST | `/api/v1/shifts/{id}/finish` | Завершить | Bearer |
| GET | `/api/v1/shifts` | История смен (пагинация, фильтры) | Bearer |
| GET | `/api/v1/shifts/stats` | Статистика (день/неделя/месяц) | Bearer |
```

Add to the Services table:

```markdown
| `services/shift.py` | Lifecycle смен, статистика, автозавершение |
```

- [ ] **Step 2: Update ROADMAP.md — mark Phase 2 as done**

Change `## Фаза 2 — Персональный режим (смены) [ ]` to `[x]` and mark all sub-tasks as `[x]`.

- [ ] **Step 3: Commit**

```bash
git add docs/ARCHITECTURE.md docs/ROADMAP.md
git commit -m "docs: update architecture and roadmap for phase 2"
```

---

## Final endpoint structure for `src/app/api/v1/shifts.py`

The order of routes matters — static paths must come before parametric:

```
GET  /shifts          → list_shifts
GET  /shifts/stats    → shift_stats
POST /shifts/start    → start_shift
POST /shifts/{id}/pause   → pause_shift
POST /shifts/{id}/resume  → resume_shift
POST /shifts/{id}/finish  → finish_shift
```
