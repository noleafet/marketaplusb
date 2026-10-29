from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from ..auth import ApiKeyAuthentication, HasReadScope
from ..errors import NotFoundProblem, problem_exception_handler
from ..serializers import iso
from ..throttling import ClientRateThrottle


def envelope(data, *, meta: dict | None = None, completeness: dict | None = None) -> Response:
    return Response(
        {
            "data": data,
            "meta": {"api_version": "1", "generated_at": iso(timezone.now()), **(meta or {})},
            "completeness": completeness or {},
        }
    )


class V1View(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [HasReadScope]
    throttle_classes = [ClientRateThrottle]

    def get_exception_handler(self):
        return problem_exception_handler

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        rate = getattr(request, "rate_limit", None)
        if rate is not None:
            limit, remaining, reset = rate
            response["X-RateLimit-Limit"] = str(limit)
            response["X-RateLimit-Remaining"] = str(remaining)
            response["X-RateLimit-Reset"] = str(reset)
        return response


class NotFoundView(V1View):
    """Catch-all so unknown /api/v1/ paths return problem+json 404."""

    def get(self, request, *args, **kwargs):
        raise NotFoundProblem("No such endpoint.")
