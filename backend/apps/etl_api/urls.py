from django.urls import path, re_path

from .views.backtests import BacktestDetailView, BacktestFoldsView, BacktestListView
from .views.base import NotFoundView
from .views.market import CandlesView, FeaturesView, FundingView
from .views.meta import HealthView
from .views.versions import (
    ConfigVersionDetailView,
    ConfigVersionListView,
    StrategyVersionDetailView,
    StrategyVersionListView,
)

urlpatterns = [
    path("meta/health/", HealthView.as_view(), name="etl-health"),
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
    re_path(r"^.*$", NotFoundView.as_view(), name="etl-not-found"),
]
