from fastapi import APIRouter

from src.app.api.v1.adjustments import router as adjustments_router
from src.app.api.v1.admin import router as admin_router
from src.app.api.v1.admin_payments import router as admin_payments_router
from src.app.api.v1.admin_subscriptions import router as admin_subscriptions_router
from src.app.api.v1.auth import router as auth_router
from src.app.api.v1.billing import router as billing_router
from src.app.api.v1.billing_webhook import router as billing_webhook_router
from src.app.api.v1.checklist_assignments import router as checklist_assignments_router
from src.app.api.v1.checklist_instances import router as checklist_instances_router
from src.app.api.v1.checklist_overrides import router as checklist_overrides_router
from src.app.api.v1.checklist_templates import router as checklist_templates_router
from src.app.api.v1.employee_tests import router as employee_tests_router
from src.app.api.v1.files import router as files_router
from src.app.api.v1.knowledge import router as knowledge_router
from src.app.api.v1.manual_shifts import router as manual_shifts_router
from src.app.api.v1.my_tests import router as my_tests_router
from src.app.api.v1.notifications import router as notifications_router
from src.app.api.v1.organization_roles import router as organization_roles_router
from src.app.api.v1.organizations import router as organizations_router
from src.app.api.v1.overtime import router as overtime_router
from src.app.api.v1.payroll import router as payroll_router
from src.app.api.v1.penalties import router as penalties_router
from src.app.api.v1.plans import router as plans_router
from src.app.api.v1.shifts import router as shifts_router
from src.app.api.v1.subscriptions import router as subscription_router
from src.app.api.v1.users import router as users_router
from src.app.api.v1.work_locations import router as work_locations_router
from src.app.api.v1.work_locations_nearby import router as work_locations_nearby_router
from src.app.api.v1.work_schedules import router as work_schedules_router

router = APIRouter(prefix="/v1")
router.include_router(auth_router)
router.include_router(shifts_router)
router.include_router(users_router)
router.include_router(organizations_router)
router.include_router(organization_roles_router)
router.include_router(payroll_router)
router.include_router(penalties_router)
router.include_router(manual_shifts_router)
router.include_router(adjustments_router)
router.include_router(checklist_templates_router)
router.include_router(checklist_assignments_router)
router.include_router(checklist_overrides_router)
router.include_router(checklist_instances_router)
router.include_router(work_locations_router)
router.include_router(work_locations_nearby_router)
router.include_router(work_schedules_router)
router.include_router(overtime_router)
router.include_router(files_router)
router.include_router(knowledge_router)
router.include_router(notifications_router)
router.include_router(employee_tests_router)
router.include_router(my_tests_router)
router.include_router(admin_router)
router.include_router(admin_subscriptions_router)
router.include_router(plans_router)
router.include_router(subscription_router)
router.include_router(billing_router)
router.include_router(billing_webhook_router)
router.include_router(admin_payments_router)
