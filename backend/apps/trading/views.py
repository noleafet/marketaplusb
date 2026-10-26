from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.engine.timescale import load_equity_points
from apps.jobs.models import JobRun
from apps.jobs.runner import enqueue_job
from apps.jobs.serializers import JobRunSerializer
from apps.trading.execution import live_status, set_kill_switch
from apps.trading.models import (
    LiveApproval,
    LiveOrderAudit,
    LiveTradingState,
    PaperEvaluation,
    PaperFill,
    PaperSession,
)
from apps.trading.paper import (
    evaluate_session,
    pause_session,
    resume_session,
    stop_session,
)
from apps.trading.serializers import (
    ApprovalRequestSerializer,
    ApprovalReviewSerializer,
    KillSwitchSerializer,
    LiveApprovalSerializer,
    LiveOrderAuditSerializer,
    LiveTradingStateSerializer,
    PaperEvaluationSerializer,
    PaperFillSerializer,
    PaperSessionListSerializer,
    PaperSessionSerializer,
)


class PaperSessionViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = "id"

    def get_serializer_class(self):
        if self.action == "retrieve":
            return PaperSessionSerializer
        return PaperSessionListSerializer

    def get_queryset(self):
        queryset = PaperSession.objects.select_related("coin", "analysis")
        state = self.request.query_params.get("status")
        symbol = self.request.query_params.get("symbol")
        promotion = self.request.query_params.get("promotion_ready")
        if state:
            queryset = queryset.filter(status__in=state.split(","))
        if symbol:
            queryset = queryset.filter(coin__symbol=symbol.upper())
        if promotion in {"true", "1"}:
            queryset = queryset.filter(promotion_ready=True)
        elif promotion in {"false", "0"}:
            queryset = queryset.filter(promotion_ready=False)
        return queryset.order_by("-started_at")

    @action(detail=True, methods=["get"])
    def equity(self, request, id=None):
        session = self.get_object()
        try:
            limit = max(1, min(10000, int(request.query_params.get("limit", 5000))))
        except (TypeError, ValueError):
            limit = 5000
        rows = load_equity_points(str(session.id), limit=limit)
        return Response(
            {
                "session": str(session.id),
                "symbol": session.coin.symbol,
                "points": rows,
                "capital": session.capital,
            }
        )

    @action(detail=True, methods=["get"])
    def fills(self, request, id=None):
        session = self.get_object()
        try:
            limit = max(1, min(1000, int(request.query_params.get("limit", 200))))
        except (TypeError, ValueError):
            limit = 200
        queryset = PaperFill.objects.filter(session=session)[:limit]
        return Response(
            {
                "session": str(session.id),
                "symbol": session.coin.symbol,
                "fills": PaperFillSerializer(queryset, many=True).data,
            }
        )

    @action(detail=True, methods=["get"])
    def evaluations(self, request, id=None):
        session = self.get_object()
        queryset = PaperEvaluation.objects.filter(session=session)[:100]
        return Response(PaperEvaluationSerializer(queryset, many=True).data)

    @action(detail=True, methods=["post"])
    def pause(self, request, id=None):
        session = pause_session(self.get_object())
        return Response(PaperSessionListSerializer(session).data)

    @action(detail=True, methods=["post"])
    def resume(self, request, id=None):
        session = resume_session(self.get_object())
        return Response(PaperSessionListSerializer(session).data)

    @action(detail=True, methods=["post"])
    def stop(self, request, id=None):
        reason = str(request.data.get("reason") or "manual_stop")[:255]
        session = stop_session(self.get_object(), reason=reason)
        return Response(PaperSessionListSerializer(session).data)

    @action(detail=True, methods=["post"])
    def evaluate(self, request, id=None):
        session = self.get_object()
        result = evaluate_session(session, force=True)
        return Response(
            {
                "session": str(session.id),
                "promotion_ready": session.promotion_ready,
                "evaluation": result,
            }
        )

    @action(detail=True, methods=["post"])
    def tick(self, request, id=None):
        session = self.get_object()
        job = enqueue_job(
            "paper_tick",
            trigger=JobRun.Trigger.MANUAL,
            params={"session_ids": [str(session.id)]},
        )
        return Response(JobRunSerializer(job).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=["post"], url_path="tick")
    def tick_all(self, request):
        session_ids = request.data.get("session_ids") or None
        if session_ids is not None and not isinstance(session_ids, list):
            raise serializers.ValidationError({"session_ids": "must be a list"})
        job = enqueue_job(
            "paper_tick",
            trigger=JobRun.Trigger.MANUAL,
            params={"session_ids": session_ids} if session_ids else {},
        )
        return Response(JobRunSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class LiveApprovalViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = LiveApprovalSerializer
    lookup_field = "id"

    def get_queryset(self):
        queryset = LiveApproval.objects.select_related("paper_session__coin")
        state = self.request.query_params.get("status")
        if state:
            queryset = queryset.filter(status=state)
        return queryset

    @action(detail=False, methods=["post"])
    def request_approval(self, request):
        serializer = ApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        session = None
        if data.get("paper_session"):
            session = PaperSession.objects.filter(id=data["paper_session"]).first()
            if session is None:
                return Response(
                    {"detail": "paper_session not found"}, status=status.HTTP_404_NOT_FOUND
                )
            if not session.promotion_ready:
                return Response(
                    {
                        "detail": (
                            "Paper session has not passed the profitability and risk "
                            "thresholds; explore approval is not allowed."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )
        else:
            return Response(
                {"detail": "A promotion-ready paper_session is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        approval = LiveApproval.objects.create(
            status=LiveApproval.Status.PENDING,
            requested_by=data.get("requested_by") or "dashboard",
            notes=data.get("notes") or "",
            paper_session=session,
            risk_acknowledged=True,
        )
        return Response(
            LiveApprovalSerializer(approval).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def approve(self, request, id=None):
        approval = self.get_object()
        serializer = ApprovalReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not approval.risk_acknowledged:
            return Response(
                {"detail": "Approval record lacks risk acknowledgement."},
                status=status.HTTP_409_CONFLICT,
            )
        approval.status = LiveApproval.Status.APPROVED
        approval.reviewed_by = serializer.validated_data.get("reviewed_by") or "dashboard"
        approval.reviewed_at = timezone.now()
        if serializer.validated_data.get("notes"):
            approval.notes = serializer.validated_data["notes"]
        approval.save(update_fields=["status", "reviewed_by", "reviewed_at", "notes"])
        state = LiveTradingState.load()
        state.active_approval = approval
        state.save(update_fields=["active_approval", "updated_at"])
        return Response(
            {
                "approval": LiveApprovalSerializer(approval).data,
                "live": live_status(),
            }
        )

    @action(detail=True, methods=["post"])
    def revoke(self, request, id=None):
        approval = self.get_object()
        approval.status = LiveApproval.Status.REVOKED
        approval.reviewed_at = timezone.now()
        approval.save(update_fields=["status", "reviewed_at"])
        state = LiveTradingState.load()
        if state.active_approval_id == approval.id:
            state.active_approval = None
            state.save(update_fields=["active_approval", "updated_at"])
        set_kill_switch(True, "Approval revoked; live trading locked")
        return Response(
            {"approval": LiveApprovalSerializer(approval).data, "live": live_status()}
        )


class LiveStatusView(viewsets.ViewSet):
    def list(self, request):
        state = LiveTradingState.load()
        approvals = LiveApproval.objects.select_related("paper_session__coin")[:20]
        audits = LiveOrderAudit.objects.all()[:50]
        return Response(
            {
                "status": live_status(),
                "state": LiveTradingStateSerializer(state).data,
                "approvals": LiveApprovalSerializer(approvals, many=True).data,
                "audits": LiveOrderAuditSerializer(audits, many=True).data,
                "note": (
                    "Live trading is disabled by default. The dashboard can request and "
                    "review approval records, but no live order endpoint is exposed and "
                    "scheduled jobs never call the live adapter."
                ),
            }
        )

    @action(detail=False, methods=["post"], url_path="kill-switch")
    def kill_switch(self, request):
        serializer = KillSwitchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        state = set_kill_switch(
            serializer.validated_data["engaged"],
            serializer.validated_data.get("reason") or "",
        )
        return Response(
            {"state": LiveTradingStateSerializer(state).data, "live": live_status()}
        )


class LiveAuditViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = LiveOrderAuditSerializer
    lookup_field = "id"

    def get_queryset(self):
        return LiveOrderAudit.objects.all()
