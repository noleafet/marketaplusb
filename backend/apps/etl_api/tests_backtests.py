from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.coins.models import Coin
from apps.engine.models import (
    AnalysisRun,
    CoinAnalysis,
    ConfigVersion,
    DecisionLog,
    StrategyVersion,
)
from apps.etl_api.keys import create_client
from apps.etl_api.serializers import METRIC_FIELDS, metrics_block

LIST = "/api/v1/backtests/"

GOOD_FOLD = {
    "fold": 0,
    "train_start": "2026-01-01T00:00:00",
    "train_end": "2026-02-01T00:00:00+00:00",
    "test_start": "2026-02-01T00:00:00+00:00",
    "test_end": "2026-03-01T00:00:00",
    "params": {"a": 1},
    "train_metrics": {"sharpe": 1.0},
    "test_metrics": {"sharpe": 0.5},
    "regime": "trend",
    "stress": {"x": 1},
    "alt_fill": {"y": 2},
    "train_objective": 3.0,
}
BAD_FOLD = {**GOOD_FOLD, "fold": 1, "test_start": "2026-01-15T00:00:00+00:00"}
UNPARSEABLE_FOLD = {**GOOD_FOLD, "fold": 2, "train_start": "garbage"}


class BacktestBase(TestCase):
    def setUp(self):
        _, key = create_client("t")
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
        self.past = timezone.now() - timedelta(minutes=5)
        self.sv = StrategyVersion.objects.create(id="sv1", snapshot={}, code_sha="abc")
        self.sv2 = StrategyVersion.objects.create(id="sv2", snapshot={}, code_sha="def")
        self.cv = ConfigVersion.objects.create(id="cv1", symbol="BTCUSDT", params={})
        self.coin = Coin.objects.create(
            coingecko_id="b", symbol="BTC", name="B", binance_symbol="BTCUSDT"
        )

    def make(self, sv=None, status="succeeded", params=None, **kw):
        run = AnalysisRun.objects.create(
            status=status,
            strategy_version=sv or self.sv,
            data_cutoff=self.past,
            params=params
            if params is not None
            else {"interval": "1h", "config": {"holdout_days": 30}},
        )
        defaults = dict(
            analysis=run,
            coin=self.coin,
            rank=1,
            score=7.5,
            suitability="viable",
            eligible=True,
            oos_metrics={"sharpe": 1.2},
            in_sample_metrics={"sharpe": 2.0},
            data_start=self.past - timedelta(days=100),
            data_end=self.past,
            config_version=self.cv,
        )
        defaults.update(kw)
        ca = CoinAnalysis.objects.create(**defaults)
        CoinAnalysis.objects.filter(pk=ca.pk).update(created_at=self.past)
        ca.refresh_from_db()
        return ca


class MetricsTests(TestCase):
    def test_metrics_block_has_all_fields_null_when_missing(self):
        self.assertEqual(set(metrics_block({"sharpe": 1.2})), set(METRIC_FIELDS))
        self.assertIsNone(metrics_block({})["sortino"])
        self.assertEqual(metrics_block({"sharpe": 1.2})["sharpe"], 1.2)
        self.assertIsNone(metrics_block(None)["sharpe"])


class BacktestTests(BacktestBase):
    def test_backtest_fields_and_holdout_start(self):
        ca = self.make()
        r = self.api.get(LIST)
        self.assertEqual(r.status_code, 200)
        d = r.json()["data"][0]
        self.assertEqual(
            list(d),
            [
                "id", "run_id", "environment", "symbol", "timeframe", "trigger", "mode",
                "scope", "strategy_version", "config_version", "data_start", "data_end",
                "data_cutoff", "holdout_start", "in_sample", "out_of_sample", "score", "rank",
                "suitability", "eligible", "exclusion_reason", "component_scores",
                "trade_sample", "created_at",
            ],
        )
        self.assertEqual(d["id"], ca.pk)
        self.assertEqual(d["run_id"], str(ca.analysis_id))
        self.assertEqual(d["environment"], "backtest")
        self.assertEqual(d["symbol"], "BTCUSDT")
        self.assertEqual(d["timeframe"], "1h")
        self.assertEqual(d["strategy_version"], "sv1")
        self.assertEqual(d["config_version"], "cv1")
        want = ca.data_end - timedelta(days=30)
        self.assertEqual(d["holdout_start"][:19], want.strftime("%Y-%m-%dT%H:%M:%S"))
        self.assertTrue(d["holdout_start"].endswith("Z"))
        self.assertEqual(d["out_of_sample"]["sharpe"], 1.2)
        self.assertIsNone(d["out_of_sample"]["sortino"])
        self.assertEqual(set(d["in_sample"]), set(METRIC_FIELDS))
        self.assertEqual(r.json()["completeness"]["trade_sample_is_complete"], False)

    def test_holdout_start_null_when_missing(self):
        ca = self.make(params={"interval": "1h"})
        r = self.api.get(f"{LIST}{ca.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["data"]["holdout_start"])

    def test_running_run_is_partial(self):
        ca = self.make(status="running")
        r = self.api.get(LIST)
        self.assertEqual(r.json()["completeness"]["partial_run_ids"], [str(ca.analysis_id)])
        r = self.api.get(f"{LIST}{ca.pk}/")
        c = r.json()["completeness"]
        self.assertTrue(c["is_partial"])
        self.assertEqual(c["run_status"], "running")
        self.assertFalse(c["trade_sample_is_complete"])

    def test_filter_by_strategy_version(self):
        a = self.make(sv=self.sv)
        self.make(sv=self.sv2)
        r = self.api.get(LIST + "?strategy_version=sv1")
        self.assertEqual([d["id"] for d in r.json()["data"]], [a.pk])

    def test_other_filters(self):
        a = self.make()
        self.make(params={"interval": "4h"})
        self.assertEqual(len(self.api.get(LIST + "?timeframe=1h").json()["data"]), 1)
        self.assertEqual(len(self.api.get(LIST + "?symbol=ETHUSDT").json()["data"]), 0)
        self.assertEqual(len(self.api.get(LIST + "?config_version=cv1").json()["data"]), 2)
        self.assertEqual(len(self.api.get(LIST + "?config_version=zz").json()["data"]), 0)
        # start/end filter data_end, not created_at
        far = (self.past + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(len(self.api.get(LIST + f"?start={far}").json()["data"]), 0)
        near = (self.past - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(len(self.api.get(LIST + f"?start={near}").json()["data"]), 2)
        self.assertEqual(len(self.api.get(LIST + f"?end={near}").json()["data"]), 0)
        self.assertEqual(a.pk, self.api.get(LIST + "?timeframe=1h").json()["data"][0]["id"])

    def test_environment_filter(self):
        self.make()
        self.assertEqual(len(self.api.get(LIST + "?environment=backtest").json()["data"]), 1)
        r = self.api.get(LIST + "?environment=walk_forward")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"], [])

    def test_hold_back_hides_fresh_rows(self):
        ca = self.make()
        CoinAnalysis.objects.filter(pk=ca.pk).update(created_at=timezone.now())
        self.assertEqual(self.api.get(LIST).json()["data"], [])

    def test_huge_id_is_404(self):
        for suffix in ("", "folds/"):
            r = self.api.get(f"{LIST}{10**30}/{suffix}")
            self.assertEqual(r.status_code, 404)
            self.assertIn("problem+json", r["Content-Type"])

    def test_odd_params_give_null_holdout(self):
        from apps.etl_api.serializers import backtest_out

        ca = self.make()
        for params in (
            {"config": "x"},
            {"config": None},
            {"config": {"holdout_days": float("nan")}},
            {"config": {"holdout_days": 10**30}},
        ):
            ca.analysis.params = params
            self.assertIsNone(backtest_out(ca)["holdout_start"], params)
        ca.analysis.params = ["not", "a", "dict"]
        self.assertIsNone(backtest_out(ca)["holdout_start"])

    def test_unknown_detail_is_404(self):
        self.assertEqual(self.api.get(f"{LIST}999999/").status_code, 404)
        self.assertEqual(self.api.get(f"{LIST}999999/folds/").status_code, 404)


class FoldTests(BacktestBase):
    def test_folds_never_overlap_train_and_test(self):
        ca = self.make(folds=[GOOD_FOLD, BAD_FOLD, UNPARSEABLE_FOLD])
        r = self.api.get(f"{LIST}{ca.pk}/folds/")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual([f["fold"] for f in data], [0])
        f = data[0]
        self.assertEqual(f["train_start"], "2026-01-01T00:00:00Z")
        self.assertEqual(f["train_end"], "2026-02-01T00:00:00Z")
        self.assertEqual(f["test_end"], "2026-03-01T00:00:00Z")
        self.assertEqual(set(f["train_metrics"]), set(METRIC_FIELDS))
        self.assertEqual(f["test_metrics"]["sharpe"], 0.5)
        self.assertNotIn("stress", f)
        self.assertEqual(
            DecisionLog.objects.filter(category="etl_api", severity="warning").count(), 1
        )

    def test_non_string_timestamps_drop_fold(self):
        ca = self.make(
            folds=[
                GOOD_FOLD,
                {**GOOD_FOLD, "fold": 5, "train_start": 123},
                {**GOOD_FOLD, "fold": 6, "test_end": None},
                "junk",
            ]
        )
        r = self.api.get(f"{LIST}{ca.pk}/folds/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([f["fold"] for f in r.json()["data"]], [0])

    def test_no_warning_when_all_folds_valid(self):
        ca = self.make(folds=[GOOD_FOLD])
        self.api.get(f"{LIST}{ca.pk}/folds/")
        self.assertEqual(DecisionLog.objects.filter(category="etl_api").count(), 0)

    def test_fold_environment_is_walk_forward(self):
        ca = self.make(folds=[GOOD_FOLD])
        data = self.api.get(f"{LIST}{ca.pk}/folds/").json()["data"]
        self.assertEqual(data[0]["environment"], "walk_forward")
        self.assertEqual(
            list(data[0]),
            [
                "environment", "fold", "train_start", "train_end", "test_start", "test_end",
                "params", "train_metrics", "test_metrics", "regime",
            ],
        )
