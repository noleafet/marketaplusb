from rest_framework.routers import DefaultRouter

from apps.trading.views import (
    LiveApprovalViewSet,
    LiveAuditViewSet,
    LiveStatusView,
    PaperSessionViewSet,
)
from apps.trading.strategy_views import (
    MarketRegimeView,
    StrategySettingsView,
    StrategyValidationView,
)

router = DefaultRouter()
router.register("paper/sessions", PaperSessionViewSet, basename="paper-session")
router.register("live/approvals", LiveApprovalViewSet, basename="live-approval")
router.register("live/status", LiveStatusView, basename="live-status")
router.register("live/audit", LiveAuditViewSet, basename="live-audit")
router.register("strategy/settings", StrategySettingsView, basename="strategy-settings")
router.register("strategy/validation", StrategyValidationView, basename="strategy-validation")
router.register("strategy/regime", MarketRegimeView, basename="strategy-regime")

urlpatterns = router.urls
