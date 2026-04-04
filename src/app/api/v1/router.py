from fastapi import APIRouter

from src.app.api.v1.auth import router as auth_router
from src.app.api.v1.organizations import router as organizations_router
from src.app.api.v1.shifts import router as shifts_router
from src.app.api.v1.users import router as users_router
from src.app.api.v1.work_locations import router as work_locations_router

router = APIRouter(prefix="/v1")
router.include_router(auth_router)
router.include_router(shifts_router)
router.include_router(users_router)
router.include_router(organizations_router)
router.include_router(work_locations_router)
