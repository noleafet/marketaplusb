from django.urls import path, re_path

from .views.backtests import BacktestDetailView, BacktestFoldsView, BacktestListView
from .views.base import NotFoundView
from .views.market import CandlesView, FeaturesView, FundingView
from .views.meta import HealthView, SchemaView
from .views.sessions import (
    EquityView,
    EvaluationsView,
    EventsView,
    FillsView,
    OrderAuditView,
    SessionDetailView,
    SessionFillsView,
    SessionListView,
    SnapshotsView,
)
from .views.versions import (
    ConfigVersionDetailView,
    ConfigVersionListView,
    StrategyVersionDetailView,
    StrategyVersionListView,
)

urlpatterns = [
    path("meta/health/", HealthView.as_view(), name="etl-health"),
    path("meta/schema/", SchemaView.as_view(), name="etl-schema"),
    path("strategy-versions/", StrategyVersionListView.as_view(), name="etl-strategy-versions"),
    path(
        "strategy-versions/<str:pk>/",
        StrategyVersionDetailView.as_view(),
        name="etl-strategy-version",
    ),
    path("config-versions/", ConfigVersionListView.as_view(), name="etl-config-versions"),
    path(
        "config-versions/<str:pk>/", ConfigVersionDetailView.as_view(), name="etl-config-version"
    ),
    path("market/candles/", CandlesView.as_view(), name="etl-candles"),
    path("market/funding/", FundingView.as_view(), name="etl-funding"),
    path("market/features/", FeaturesView.as_view(), name="etl-features"),
    path("backtests/", BacktestListView.as_view(), name="etl-backtests"),
    path("backtests/<int:pk>/", BacktestDetailView.as_view(), name="etl-backtest"),
    path("backtests/<int:pk>/folds/", BacktestFoldsView.as_view(), name="etl-backtest-folds"),
    path("sessions/", SessionListView.as_view(), name="etl-sessions"),
    path("sessions/<uuid:pk>/", SessionDetailView.as_view(), name="etl-session"),
    path("sessions/<uuid:pk>/snapshots/", SnapshotsView.as_view(), name="etl-snapshots"),
    path("sessions/<uuid:pk>/equity/", EquityView.as_view(), name="etl-equity"),
    path("sessions/<uuid:pk>/evaluations/", EvaluationsView.as_view(), name="etl-evaluations"),
    path("sessions/<uuid:pk>/fills/", SessionFillsView.as_view(), name="etl-session-fills"),
    path("fills/", FillsView.as_view(), name="etl-fills"),
    path("events/", EventsView.as_view(), name="etl-events"),
    path("live/order-audit/", OrderAuditView.as_view(), name="etl-order-audit"),
    re_path(r"^.*$", NotFoundView.as_view(), name="etl-not-found"),
]
