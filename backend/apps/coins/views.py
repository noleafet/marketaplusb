from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.coins.models import Coin
from apps.coins.serializers import CoinSerializer
from apps.coins.services.timescale import market_snapshots


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
