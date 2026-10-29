import os
from unittest.mock import MagicMock, patch

from django.db import IntegrityError
from django.test import TestCase

from apps.engine.models import ConfigVersion, StrategyVersion
from apps.engine.versioning import (
    digest,
    resolve_config_version,
    resolve_strategy_version,
    strategy_snapshot,
)
from apps.trading.models import StrategySettings

_NO_MATCH = MagicMock()
_NO_MATCH.first.return_value = None


class CanonicalHashTests(TestCase):
    def test_key_order_and_float_noise_do_not_change_hash(self):
        a = {"b": 0.1 + 0.2, "a": [1, {"y": 2, "x": 1}]}
        b = {"a": [1, {"x": 1, "y": 2}], "b": 0.3}
        self.assertEqual(digest(a), digest(b))

    def test_real_change_changes_hash(self):
        self.assertNotEqual(digest({"spacing": 0.01}), digest({"spacing": 0.011}))


class StrategyVersionTests(TestCase):
    def test_same_settings_same_version(self):
        self.assertEqual(resolve_strategy_version().id, resolve_strategy_version().id)
        self.assertEqual(StrategyVersion.objects.count(), 1)

    def test_changed_setting_creates_child_with_diff(self):
        v1 = resolve_strategy_version()
        row = StrategySettings.load()
        row.trade_through_bps = 5.0
        row.save()
        v2 = resolve_strategy_version(row)
        self.assertNotEqual(v1.id, v2.id)
        self.assertEqual(v2.parent_id, v1.id)
        self.assertEqual(v2.diff["trade_through_bps"], {"from": 2.0, "to": 5.0})

    def test_missing_git_sha_is_unknown(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GIT_SHA", None)
            self.assertEqual(resolve_strategy_version().code_sha, "unknown")

    def test_git_sha_is_part_of_identity(self):
        with patch.dict(os.environ, {"GIT_SHA": "abc"}):
            a = resolve_strategy_version()
        with patch.dict(os.environ, {"GIT_SHA": "def"}):
            b = resolve_strategy_version()
        self.assertNotEqual(a.id, b.id)

    def test_git_sha_only_change_records_code_sha_diff(self):
        with patch.dict(os.environ, {"GIT_SHA": "abc"}):
            a = resolve_strategy_version()
        with patch.dict(os.environ, {"GIT_SHA": "def"}):
            b = resolve_strategy_version()
        self.assertEqual(b.parent_id, a.id)
        self.assertEqual(b.diff["code_sha"], {"from": "abc", "to": "def"})

    def test_resolve_is_race_safe(self):
        # Lost race: the existence check misses, the other writer's row is
        # already committed, and our get_or_create hits the unique key.
        snap = strategy_snapshot()
        vid = digest({"snapshot": snap, "code_sha": "unknown"})
        StrategyVersion.objects.create(id=vid, snapshot=snap, code_sha="unknown")
        with patch.object(
            StrategyVersion.objects, "filter", return_value=_NO_MATCH
        ), patch(
            "django.db.models.query.QuerySet.get_or_create",
            side_effect=IntegrityError("duplicate key"),
        ):
            v = resolve_strategy_version(snapshot=snap, code_sha="unknown")
        self.assertEqual(v.id, vid)
        self.assertEqual(StrategyVersion.objects.count(), 1)
        self.assertEqual(v.snapshot, snap)


class ConfigVersionTests(TestCase):
    def test_same_params_same_version_per_symbol(self):
        p = {"grid_levels": 12, "spacing": 0.01}
        self.assertEqual(
            resolve_config_version("BTCUSDT", p).id,
            resolve_config_version("BTCUSDT", dict(p)).id,
        )
        self.assertNotEqual(
            resolve_config_version("BTCUSDT", p).id,
            resolve_config_version("ETHUSDT", p).id,
        )

    def test_resolve_is_race_safe(self):
        p = {"grid_levels": 12}
        vid = digest({"symbol": "BTCUSDT", "params": p})

        ConfigVersion.objects.create(id=vid, symbol="BTCUSDT", params=p)
        with patch.object(
            ConfigVersion.objects, "filter", return_value=_NO_MATCH
        ), patch(
            "django.db.models.query.QuerySet.get_or_create",
            side_effect=IntegrityError("duplicate key"),
        ):
            v = resolve_config_version("BTCUSDT", p)
        self.assertEqual(v.id, vid)
        self.assertEqual(ConfigVersion.objects.count(), 1)
