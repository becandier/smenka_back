from fastapi import APIRouter

from src.app.api.v1.admin import router as admin_router
from src.app.api.v1.auth import router as auth_router
from src.app.api.v1.checklist_assignments import router as checklist_assignments_router
from src.app.api.v1.checklist_instances import router as checklist_instances_router
from src.app.api.v1.checklist_overrides import router as checklist_overrides_router
from src.app.api.v1.checklist_templates import router as checklist_templates_router
from src.app.api.v1.files import router as files_router
from src.app.api.v1.organization_roles import router as organization_roles_router
from src.app.api.v1.organizations import router as organizations_router
from src.app.api.v1.payroll import router as payroll_router
from src.app.api.v1.penalties import router as penalties_router
from src.app.api.v1.shifts import router as shifts_router
from src.app.api.v1.users import router as users_router
from src.app.api.v1.work_locations import router as work_locations_router

router = APIRouter(prefix="/v1")
router.include_router(auth_router)
router.include_router(shifts_router)
router.include_router(users_router)
router.include_router(organizations_router)
router.include_router(organization_roles_router)
router.include_router(payroll_router)
router.include_router(penalties_router)
router.include_router(checklist_templates_router)
router.include_router(checklist_assignments_router)
router.include_router(checklist_overrides_router)
router.include_router(checklist_instances_router)
router.include_router(work_locations_router)
router.include_router(files_router)
router.include_router(admin_router)
