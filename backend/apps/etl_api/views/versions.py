from apps.engine.models import ConfigVersion, StrategyVersion

from ..errors import NotFoundProblem
from ..pagination import page_meta, paginate_queryset
from ..params import parse_query
from ..serializers import config_version_out, strategy_version_out
from .base import V1View, envelope


class StrategyVersionListView(V1View):
    def get(self, request):
        query = parse_query(request)
        page = paginate_queryset(
            StrategyVersion.objects.all(),
            endpoint="strategy-versions",
            key="first_seen_at",
            query=query,
        )
        unknown = [v.id for v in page.rows if not v.code_sha]
        return envelope(
            [strategy_version_out(v) for v in page.rows],
            meta=page_meta(page, query),
            completeness={"code_sha_unknown_ids": unknown},
        )


class StrategyVersionDetailView(V1View):
    def get(self, request, pk):
        try:
            v = StrategyVersion.objects.get(pk=pk)
        except StrategyVersion.DoesNotExist:
            raise NotFoundProblem("No such strategy version.")
        return envelope(strategy_version_out(v), completeness={"code_sha_known": bool(v.code_sha)})


class ConfigVersionListView(V1View):
    def get(self, request):
        query = parse_query(request)
        qs = ConfigVersion.objects.all()
        if query.symbols:
            qs = qs.filter(symbol__in=query.symbols)
        page = paginate_queryset(qs, endpoint="config-versions", key="first_seen_at", query=query)
        return envelope([config_version_out(v) for v in page.rows], meta=page_meta(page, query))


class ConfigVersionDetailView(V1View):
    def get(self, request, pk):
        try:
            v = ConfigVersion.objects.get(pk=pk)
        except ConfigVersion.DoesNotExist:
            raise NotFoundProblem("No such config version.")
        return envelope(config_version_out(v))
