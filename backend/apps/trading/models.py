import uuid

from django.db import models
from django.utils import timezone

from apps.coins.models import Coin
from apps.engine.models import AnalysisRun


class PaperSession(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        STOPPED = "stopped", "Stopped"

    class Bias(models.TextChoices):
        BULLISH = "bullish", "Bullish"
        BEARISH = "bearish", "Bearish"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    coin = models.ForeignKey(Coin, on_delete=models.PROTECT, related_name="paper_sessions")
    analysis = models.ForeignKey(
        AnalysisRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="paper_sessions"
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    bias = models.CharField(max_length=8, choices=Bias.choices, default=Bias.BULLISH)
    promotion_ready = models.BooleanField(default=False)
    stop_reason = models.CharField(max_length=255, blank=True, default="")

    params = models.JSONField(default=dict, blank=True)
    config = models.JSONField(default=dict, blank=True)
    grid_state = models.JSONField(default=dict, blank=True)
    risk_state = models.JSONField(default=dict, blank=True)

    capital = models.FloatField(default=1000.0)
    leverage = models.FloatField(default=2.0)
    position_qty = models.FloatField(default=0.0)
    avg_entry = models.FloatField(default=0.0)
    mark_price = models.FloatField(default=0.0)
    equity = models.FloatField(default=1000.0)
    peak_equity = models.FloatField(default=1000.0)
    max_drawdown = models.FloatField(default=0.0)
    realized_pnl = models.FloatField(default=0.0)
    fees_paid = models.FloatField(default=0.0)
    funding_paid = models.FloatField(default=0.0)
    # Buy fees and funding still carried by open inventory (see the adapter).
    carry_cost = models.FloatField(default=0.0)
    # Consecutive universe analyses in which the coin no longer qualified.
    failed_runs = models.PositiveSmallIntegerField(default=0)

    fill_count = models.PositiveIntegerField(default=0)
    closed_lots = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)

    started_at = models.DateTimeField(default=timezone.now)
    last_tick_at = models.DateTimeField(null=True, blank=True)
    last_funding_at = models.DateTimeField(null=True, blank=True)
    last_evaluated_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    strategy_version = models.ForeignKey(
        "engine.StrategyVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    config_version = models.ForeignKey(
        "engine.ConfigVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["status", "coin"])]

    def __str__(self) -> str:
        return f"{self.coin.symbol} paper · {self.status} · {self.equity:.2f}"

    @property
    def net_return(self) -> float:
        return self.equity / self.capital - 1.0 if self.capital else 0.0


class PaperFill(models.Model):
    session = models.ForeignKey(PaperSession, on_delete=models.CASCADE, related_name="fills")
    time = models.DateTimeField(db_index=True)
    side = models.CharField(max_length=8)
    price = models.FloatField()
    qty = models.FloatField()
    notional = models.FloatField()
    fee = models.FloatField(default=0.0)
    realized_pnl = models.FloatField(default=0.0)
    level = models.IntegerField(null=True, blank=True)
    reason = models.CharField(max_length=32, default="grid")
    created_at = models.DateTimeField(default=timezone.now)
    strategy_version = models.ForeignKey(
        "engine.StrategyVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    config_version = models.ForeignKey(
        "engine.ConfigVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-time", "-id"]
        # (created_at, id) is the v1 fills paging key.
        indexes = [
            models.Index(fields=["time", "id"]),
            models.Index(fields=["created_at", "id"]),
        ]

    def __str__(self) -> str:
        return f"{self.session.coin.symbol} {self.side} {self.qty} @ {self.price}"


class GridEvent(models.Model):
    class Type(models.TextChoices):
        REBALANCE = "rebalance", "Rebalance"
        RECENTRE = "recentre", "Recentre"
        PAUSE = "pause", "Pause"
        RESUME = "resume", "Resume"
        PARAM_CHANGE = "param_change", "Param change"
        RETIRE = "retire", "Retire"
        STOP = "stop", "Stop"
        KILL_SWITCH = "kill_switch", "Kill switch"

    time = models.DateTimeField(db_index=True)
    type = models.CharField(max_length=16, choices=Type.choices)
    environment = models.CharField(max_length=16, default="paper")
    symbol = models.CharField(max_length=40, blank=True, default="")
    session = models.ForeignKey(
        PaperSession, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    reason = models.CharField(max_length=255, blank=True)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    strategy_version = models.ForeignKey(
        "engine.StrategyVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    config_version = models.ForeignKey(
        "engine.ConfigVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    backfilled = models.BooleanField(default=False)

    class Meta:
        ordering = ["time", "id"]
        indexes = [
            models.Index(fields=["time", "id"]),
            models.Index(fields=["session", "time"]),
        ]

    def __str__(self) -> str:
        return f"{self.type} {self.symbol} @ {self.time:%Y-%m-%d %H:%M}"


class SessionSnapshot(models.Model):
    session = models.ForeignKey(PaperSession, on_delete=models.CASCADE, related_name="snapshots")
    time = models.DateTimeField()
    trigger = models.CharField(max_length=16)
    state = models.JSONField()
    strategy_version = models.ForeignKey(
        "engine.StrategyVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    config_version = models.ForeignKey(
        "engine.ConfigVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    backfilled = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["session", "time"])]

    def __str__(self) -> str:
        return f"{self.session_id} {self.trigger} @ {self.time:%Y-%m-%d %H:%M}"


class PaperEvaluation(models.Model):
    session = models.ForeignKey(
        PaperSession, on_delete=models.CASCADE, related_name="evaluations"
    )
    evaluated_at = models.DateTimeField(default=timezone.now)
    passed = models.BooleanField(default=False)
    metrics = models.JSONField(default=dict, blank=True)
    failures = models.JSONField(default=list, blank=True)
    thresholds = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-evaluated_at"]


class LiveApproval(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REVOKED = "revoked", "Revoked"
        REJECTED = "rejected", "Rejected"

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    requested_by = models.CharField(max_length=128, default="dashboard")
    requested_at = models.DateTimeField(default=timezone.now)
    reviewed_by = models.CharField(max_length=128, blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    paper_session = models.ForeignKey(
        PaperSession, on_delete=models.SET_NULL, null=True, blank=True, related_name="approvals"
    )
    risk_acknowledged = models.BooleanField(default=False)

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self) -> str:
        return f"Live approval {self.id} ({self.status})"


class LiveTradingState(models.Model):
    """Singleton safety state. Live trading requires all gates to agree."""

    class Mode(models.TextChoices):
        NORMAL = "normal", "Normal"
        HYBRID = "hybrid", "Hybrid"

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    mode = models.CharField(
        max_length=8, choices=Mode.choices, default=Mode.NORMAL, db_index=True
    )
    kill_switch = models.BooleanField(default=True)
    kill_reason = models.CharField(max_length=255, default="Manual release required")
    active_approval = models.ForeignKey(
        LiveApproval, on_delete=models.SET_NULL, null=True, blank=True
    )
    limits = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Live trading state"

    def __str__(self) -> str:
        return f"Live state kill_switch={self.kill_switch}"

    @classmethod
    def load(cls) -> "LiveTradingState":
        state, _ = cls.objects.get_or_create(
            id=1,
            defaults={"kill_switch": True, "kill_reason": "Manual release required"},
        )
        return state


class LiveOrderAudit(models.Model):
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    symbol = models.CharField(max_length=40)
    side = models.CharField(max_length=8)
    order_type = models.CharField(max_length=16)
    qty = models.FloatField(null=True, blank=True)
    price = models.FloatField(null=True, blank=True)
    notional = models.FloatField(null=True, blank=True)
    allowed = models.BooleanField(default=False)
    reason = models.CharField(max_length=255, blank=True, default="")
    response = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]


class StrategySettings(models.Model):
    """Operator-tunable strategy and portfolio settings (singleton).

    The signal interval and fill model are shared by the daily backtest and
    paper trading so both measure the same strategy. Portfolio limits apply to
    the whole paper book rather than to a single session.
    """

    class Interval(models.TextChoices):
        ONE_HOUR = "1h", "1 hour"
        FOUR_HOURS = "4h", "4 hours"

    class FillModel(models.TextChoices):
        TRADE_THROUGH = "trade_through", "Trade-through"
        TOUCH = "touch", "Touch"

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    signal_interval = models.CharField(
        max_length=4, choices=Interval.choices, default=Interval.ONE_HOUR
    )
    fill_model = models.CharField(
        max_length=16, choices=FillModel.choices, default=FillModel.TRADE_THROUGH
    )
    trade_through_bps = models.FloatField(default=2.0)
    # The overbought guard has no backtest yet, so it stays off until one
    # shows it adds value.
    overbought_guard_enabled = models.BooleanField(default=False)
    holdout_days = models.PositiveSmallIntegerField(default=90)

    book_capital = models.FloatField(default=8000.0)
    max_sessions = models.PositiveSmallIntegerField(default=8)
    max_book_exposure = models.FloatField(default=1.0)
    daily_loss_limit_pct = models.FloatField(default=0.03)
    retire_after_failed_runs = models.PositiveSmallIntegerField(default=2)
    max_strategy_correlation = models.FloatField(default=0.7)

    book_state = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Strategy settings"

    def __str__(self) -> str:
        return f"Strategy settings ({self.signal_interval}, {self.fill_model})"

    @classmethod
    def load(cls) -> "StrategySettings":
        settings_row, _ = cls.objects.get_or_create(id=1)
        return settings_row

    @property
    def session_capital(self) -> float:
        return self.book_capital / max(1, self.max_sessions)

    @property
    def effective_trade_through_bps(self) -> float:
        if self.fill_model == self.FillModel.TOUCH:
            return 0.0
        return max(0.0, self.trade_through_bps)
