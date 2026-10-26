from rest_framework import serializers

from apps.coins.serializers import CoinSerializer
from apps.trading.models import (
    LiveApproval,
    LiveOrderAudit,
    LiveTradingState,
    PaperEvaluation,
    PaperFill,
    PaperSession,
)


class PaperSessionListSerializer(serializers.ModelSerializer):
    coin = CoinSerializer(read_only=True)
    net_return = serializers.FloatField(read_only=True)

    class Meta:
        model = PaperSession
        fields = [
            "id",
            "coin",
            "analysis",
            "status",
            "promotion_ready",
            "stop_reason",
            "params",
            "capital",
            "leverage",
            "position_qty",
            "avg_entry",
            "mark_price",
            "equity",
            "peak_equity",
            "max_drawdown",
            "net_return",
            "realized_pnl",
            "fees_paid",
            "funding_paid",
            "fill_count",
            "closed_lots",
            "wins",
            "losses",
            "started_at",
            "last_tick_at",
            "last_evaluated_at",
            "updated_at",
        ]


class PaperSessionSerializer(PaperSessionListSerializer):
    class Meta(PaperSessionListSerializer.Meta):
        fields = PaperSessionListSerializer.Meta.fields + ["config", "grid_state"]


class PaperFillSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperFill
        fields = [
            "id",
            "time",
            "side",
            "price",
            "qty",
            "notional",
            "fee",
            "realized_pnl",
            "level",
            "reason",
        ]


class PaperEvaluationSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperEvaluation
        fields = ["id", "evaluated_at", "passed", "metrics", "failures", "thresholds"]


class LiveApprovalSerializer(serializers.ModelSerializer):
    paper_symbol = serializers.CharField(
        source="paper_session.coin.symbol", read_only=True, default=None
    )

    class Meta:
        model = LiveApproval
        fields = [
            "id",
            "status",
            "requested_by",
            "requested_at",
            "reviewed_by",
            "reviewed_at",
            "notes",
            "paper_session",
            "paper_symbol",
            "risk_acknowledged",
        ]


class LiveOrderAuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = LiveOrderAudit
        fields = [
            "id",
            "created_at",
            "symbol",
            "side",
            "order_type",
            "qty",
            "price",
            "notional",
            "allowed",
            "reason",
            "response",
        ]


class LiveTradingStateSerializer(serializers.ModelSerializer):
    active_approval = LiveApprovalSerializer(read_only=True)

    class Meta:
        model = LiveTradingState
        fields = [
            "kill_switch",
            "kill_reason",
            "active_approval",
            "limits",
            "updated_at",
        ]


class ApprovalRequestSerializer(serializers.Serializer):
    paper_session = serializers.UUIDField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    requested_by = serializers.CharField(required=False, allow_blank=True, default="dashboard")
    risk_acknowledged = serializers.BooleanField(default=False)

    def validate_risk_acknowledged(self, value):
        if not value:
            raise serializers.ValidationError(
                "Risk acknowledgement is required before requesting live approval."
            )
        return value


class ApprovalReviewSerializer(serializers.Serializer):
    reviewed_by = serializers.CharField(required=False, allow_blank=True, default="dashboard")
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class KillSwitchSerializer(serializers.Serializer):
    engaged = serializers.BooleanField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")
