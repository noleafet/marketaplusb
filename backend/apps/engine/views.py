from django.db.models import Count
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.coins.models import Coin
from apps.engine.analysis import latest_analysis
from apps.engine.models import AnalysisRun, CoinAnalysis, DecisionLog, Report
from apps.engine.serializers import (
    AnalysisRunSerializer,
    CoinAnalysisCompactSerializer,
    CoinAnalysisSerializer,
    DecisionLogSerializer,
    ReportSerializer,
)
from apps.jobs.models import JobRun
from apps.jobs.runner import enqueue_job
from apps.jobs.serializers import JobRunSerializer
from apps.trading.execution import live_status
from apps.trading.models import PaperSession


def _limit(request, default=200, maximum=1000):
    try:
        return max(1, min(maximum, int(request.query_params.get("limit", default))))
    except (TypeError, ValueError):
        return default


class AnalysisRunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AnalysisRunSerializer
    lookup_field = "id"

    def get_queryset(self):
        queryset = AnalysisRun.objects.all()
        state = self.request.query_params.get("status")
        trigger = self.request.query_params.get("trigger")
        if state:
            queryset = queryset.filter(status=state)
        if trigger:
            queryset = queryset.filter(trigger=trigger)
        return queryset

    @action(detail=False, methods=["get"])
    def latest(self, request):
        run = AnalysisRun.objects.order_by("-started_at").first()
        if run is None:
            return Response({"detail": "No analysis runs yet."}, status=status.HTTP_404_NOT_FOUND)
        return Response(AnalysisRunSerializer(run).data)

    @action(detail=True, methods=["get"])
    def results(self, request, id=None):
        run = self.get_object()
        queryset = run.coin_results.select_related("coin").order_by("rank", "-score")
        compact = request.query_params.get("compact", "true") in {"1", "true", "yes"}
        serializer_class = CoinAnalysisCompactSerializer if compact else CoinAnalysisSerializer
        data = serializer_class(queryset[: _limit(request, 200)], many=True).data
        return Response({"analysis": str(run.id), "count": len(data), "results": data})

    @action(detail=False, methods=["post"])
    def trigger(self, request):
        params = request.data.get("params") or {}
        if not isinstance(params, dict):
            return Response(
                {"detail": "params must be an object"}, status=status.HTTP_400_BAD_REQUEST
            )
        job = enqueue_job(
            "daily_analysis",
            trigger=JobRun.Trigger.MANUAL,
            params=params,
        )
        return Response(JobRunSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class CoinAnalysisViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = "id"

    def get_serializer_class(self):
        compact = self.request.query_params.get("compact", "true") in {"1", "true", "yes"}
        return CoinAnalysisCompactSerializer if compact else CoinAnalysisSerializer

    def get_queryset(self):
        queryset = CoinAnalysis.objects.select_related("coin", "analysis")
        analysis_id = self.request.query_params.get("analysis")
        if analysis_id in (None, "", "latest"):
            run = latest_analysis() or AnalysisRun.objects.order_by("-started_at").first()
            if run is None:
                return queryset.none()
            queryset = queryset.filter(analysis=run)
        elif analysis_id:
            queryset = queryset.filter(analysis_id=analysis_id)
        suitability = self.request.query_params.get("suitability")
        if suitability:
            queryset = queryset.filter(suitability__in=suitability.split(","))
        eligible = self.request.query_params.get("eligible")
        if eligible in {"true", "1"}:
            queryset = queryset.filter(eligible=True)
        elif eligible in {"false", "0"}:
            queryset = queryset.filter(eligible=False)
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(coin__symbol__icontains=search) | queryset.filter(
                coin__name__icontains=search
            )
        return queryset.order_by("rank", "-score")

    @action(detail=False, methods=["get"])
    def by_coin(self, request):
        symbol = (request.query_params.get("symbol") or "").upper()
        if not symbol:
            return Response(
                {"detail": "symbol query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = (
            CoinAnalysis.objects.select_related("coin", "analysis")
            .filter(coin__symbol=symbol)
            .order_by("-created_at")
        )
        limit = _limit(request, 30)
        serializer = CoinAnalysisSerializer(queryset[:limit], many=True)
        return Response({"symbol": symbol, "results": serializer.data})


class ReportViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ReportSerializer
    lookup_field = "id"

    def get_queryset(self):
        queryset = Report.objects.all()
        kind = self.request.query_params.get("kind")
        analysis_id = self.request.query_params.get("analysis")
        if kind:
            queryset = queryset.filter(kind=kind)
        if analysis_id in ("latest", ""):
            run = latest_analysis()
            if run:
                queryset = queryset.filter(analysis=run)
        elif analysis_id:
            queryset = queryset.filter(analysis_id=analysis_id)
        return queryset.order_by("-created_at")

    @action(detail=False, methods=["get"])
    def latest(self, request):
        result = {}
        for kind in Report.Kind.values:
            report = Report.objects.filter(kind=kind).order_by("-created_at").first()
            result[kind] = ReportSerializer(report).data if report else None
        return Response(result)


class DecisionLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = DecisionLogSerializer
    lookup_field = "id"

    def get_queryset(self):
        queryset = DecisionLog.objects.select_related("coin", "analysis")
        category = self.request.query_params.get("category")
        severity = self.request.query_params.get("severity")
        analysis_id = self.request.query_params.get("analysis")
        if category:
            queryset = queryset.filter(category=category)
        if severity:
            queryset = queryset.filter(severity=severity)
        if analysis_id:
            queryset = queryset.filter(analysis_id=analysis_id)
        return queryset.order_by("-created_at")


class DashboardSummaryView(viewsets.ViewSet):
    def list(self, request):
        run = latest_analysis()
        results = (
            run.coin_results.select_related("coin").order_by("rank", "-score")[:10]
            if run
            else CoinAnalysis.objects.none()
        )
        suitability = {
            row["suitability"]: row["total"]
            for row in CoinAnalysis.objects.filter(analysis=run).values("suitability").annotate(
                total=Count("id")
            )
        } if run else {}
        all_sessions = list(PaperSession.objects.values("status", "equity", "capital", "promotion_ready"))
        active_sessions = [s for s in all_sessions if s["status"] == PaperSession.Status.ACTIVE]
        total_capital = sum(s["capital"] for s in all_sessions)
        total_equity = sum(s["equity"] for s in all_sessions)
        returns = [
            s["equity"] / s["capital"] - 1.0 for s in all_sessions if s["capital"] > 0
        ]
        promotion_ready = sum(1 for s in all_sessions if s["promotion_ready"])
        jobs = JobRun.objects.all()
        recent_jobs = jobs[:8]
        job_counts = {
            row["status"]: row["total"] for row in jobs.values("status").annotate(total=Count("id"))
        }
        reports = Report.objects.order_by("-created_at")[:5]
        return Response(
            {
                "generated_at": timezone.now(),
                "engine": {
                    "latest_analysis": AnalysisRunSerializer(run).data if run else None,
                    "results": CoinAnalysisCompactSerializer(results, many=True).data,
                    "suitability_counts": suitability,
                },
                "universe": {
                    "coins": Coin.objects.count(),
                    "top100": Coin.objects.filter(in_top100=True).count(),
                    "eligible": Coin.objects.filter(
                        in_top100=True, analysis_eligible=True
                    ).count(),
                },
                "paper": {
                    "sessions": len(all_sessions),
                    "active": len(active_sessions),
                    "promotion_ready": promotion_ready,
                    "equity": round(total_equity, 2),
                    "capital": round(total_capital, 2),
                    "avg_return": round(sum(returns) / len(returns), 6) if returns else 0.0,
                },
                "jobs": {
                    "status_counts": job_counts,
                    "queued": job_counts.get(JobRun.Status.QUEUED, 0),
                    "running": job_counts.get(JobRun.Status.RUNNING, 0),
                    "recent": JobRunSerializer(recent_jobs, many=True).data,
                },
                "reports": ReportSerializer(reports, many=True).data,
                "live": live_status(),
            }
        )
