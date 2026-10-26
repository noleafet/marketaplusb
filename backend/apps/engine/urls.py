from rest_framework.routers import DefaultRouter

from apps.engine.views import (
    AnalysisRunViewSet,
    CoinAnalysisViewSet,
    DashboardSummaryView,
    DecisionLogViewSet,
    ReportViewSet,
)

router = DefaultRouter()
router.register("engine/analysis", AnalysisRunViewSet, basename="analysis")
router.register("engine/results", CoinAnalysisViewSet, basename="analysis-result")
router.register("engine/reports", ReportViewSet, basename="report")
router.register("engine/decisions", DecisionLogViewSet, basename="decision")
router.register("engine/dashboard", DashboardSummaryView, basename="dashboard")

urlpatterns = router.urls
