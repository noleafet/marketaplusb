from rest_framework.routers import DefaultRouter

from apps.trading.views import (
    LiveApprovalViewSet,
    LiveAuditViewSet,
    LiveStatusView,
    PaperSessionViewSet,
)

router = DefaultRouter()
router.register("paper/sessions", PaperSessionViewSet, basename="paper-session")
router.register("live/approvals", LiveApprovalViewSet, basename="live-approval")
router.register("live/status", LiveStatusView, basename="live-status")
router.register("live/audit", LiveAuditViewSet, basename="live-audit")

urlpatterns = router.urls
