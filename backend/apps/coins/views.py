from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.coins.models import Coin
from apps.coins.serializers import CoinAddSerializer, CoinSerializer
from apps.coins.services.ingest import ensure_manual_coin
from apps.coins.services.timescale import market_snapshots
from apps.engine.models import log_decision
from apps.jobs.models import JobRun
from apps.jobs.runner import JobAlreadyActive, enqueue_job
from apps.jobs.serializers import JobRunSerializer


class CoinViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CoinSerializer
    lookup_field = "coingecko_id"

    def get_queryset(self):
        qs = Coin.objects.all()
        in_top = self.request.query_params.get("in_top100")
        eligible = self.request.query_params.get("eligible")
        search = self.request.query_params.get("search")
        if in_top in {"true", "1"}:
            qs = qs.filter(in_top100=True)
        elif in_top in {"false", "0"}:
            qs = qs.filter(in_top100=False)
        if eligible in {"true", "1"}:
            qs = qs.filter(analysis_eligible=True)
        elif eligible in {"false", "0"}:
            qs = qs.filter(analysis_eligible=False)
        if search:
            qs = qs.filter(symbol__icontains=search) | qs.filter(name__icontains=search)
        return qs.order_by("rank", "symbol")

    @action(detail=True, methods=["get"])
    def snapshots(self, request, coingecko_id=None):
        coin = self.get_object()
        limit = min(int(request.query_params.get("limit", 24)), 120)
        rows = market_snapshots(coin.symbol, limit=limit)
        return Response({"symbol": coin.symbol, "snapshots": rows})

    @action(detail=False, methods=["post"])
    def add(self, request):
        serializer = CoinAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            coin = ensure_manual_coin(
                serializer.validated_data["symbol"],
                serializer.validated_data.get("name") or "",
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            job = enqueue_job(
                "daily_analysis",
                trigger=JobRun.Trigger.MANUAL,
                params={"symbols": [coin.symbol]},
            )
        except JobAlreadyActive as exc:
            # The coin is stored either way; only its solo analysis waits.
            log_decision(
                "coins",
                f"{coin.symbol} added manually; solo analysis not queued: {exc.message}",
                severity="warning",
                coin=coin,
                job_run_id=exc.active.id,
                payload={"symbol": coin.symbol, "active_job_id": str(exc.active.id)},
            )
            return Response(
                {
                    "coin": CoinSerializer(coin).data,
                    "job": None,
                    "detail": f"{coin.symbol} was added. {exc.message} "
                    "Then run its analysis from the coin page or the job console.",
                    "active_job_id": str(exc.active.id),
                },
                status=status.HTTP_202_ACCEPTED,
            )
        log_decision(
            "coins",
            f"{coin.symbol} added manually; solo analysis queued.",
            coin=coin,
            job_run_id=job.id,
            payload={
                "symbol": coin.symbol,
                "binance_symbol": coin.binance_symbol,
                "job_id": str(job.id),
            },
        )
        return Response(
            {"coin": CoinSerializer(coin).data, "job": JobRunSerializer(job).data},
            status=status.HTTP_202_ACCEPTED,
        )
