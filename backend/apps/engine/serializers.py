from rest_framework import serializers

from apps.coins.serializers import CoinSerializer
from apps.engine.models import AnalysisRun, CoinAnalysis, DecisionLog, Report


class AnalysisRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisRun
        fields = [
            "id",
            "status",
            "trigger",
            "mode",
            "scope",
            "params",
            "summary",
            "error",
            "started_at",
            "finished_at",
            "data_cutoff",
            "duration_seconds",
        ]


class CoinAnalysisSerializer(serializers.ModelSerializer):
    coin = CoinSerializer(read_only=True)

    class Meta:
        model = CoinAnalysis
        fields = [
            "id",
            "analysis",
            "coin",
            "rank",
            "score",
            "suitability",
            "eligible",
            "exclusion_reason",
            "oos_metrics",
            "in_sample_metrics",
            "features",
            "component_scores",
            "params",
            "folds",
            "chart",
            "trade_sample",
            "diagnostics",
            "data_start",
            "data_end",
            "created_at",
        ]


class CoinAnalysisCompactSerializer(serializers.ModelSerializer):
    coin = CoinSerializer(read_only=True)

    class Meta:
        model = CoinAnalysis
        fields = [
            "id",
            "analysis",
            "coin",
            "rank",
            "score",
            "suitability",
            "eligible",
            "exclusion_reason",
            "oos_metrics",
            "features",
            "component_scores",
            "diagnostics",
            "params",
            "data_start",
            "data_end",
        ]


class ReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = Report
        fields = [
            "id",
            "analysis",
            "kind",
            "title",
            "body_markdown",
            "payload",
            "created_at",
        ]


class DecisionLogSerializer(serializers.ModelSerializer):
    coin_symbol = serializers.CharField(source="coin.symbol", read_only=True, default=None)

    class Meta:
        model = DecisionLog
        fields = [
            "id",
            "created_at",
            "category",
            "severity",
            "message",
            "payload",
            "analysis",
            "job_run_id",
            "coin_symbol",
        ]
