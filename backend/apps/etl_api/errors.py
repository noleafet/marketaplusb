from django.http import Http404
from rest_framework import exceptions
from rest_framework.response import Response

TYPE_BASE = "https://marketaplusb/errors/"
CONTENT_TYPE = "application/problem+json"


class ApiProblem(exceptions.APIException):
    slug = "internal_error"
    title = "Error"
    status_code = 500

    def __init__(self, detail=None, errors=None):
        super().__init__(detail if detail is not None else self.title)
        self.errors = errors


class InvalidParameter(ApiProblem):
    slug, title, status_code = "invalid_parameter", "Invalid parameter", 400


class WindowTooLarge(ApiProblem):
    slug, title, status_code = "window_too_large", "Window too large", 400


class Unauthenticated(ApiProblem):
    slug, title, status_code = "unauthenticated", "Authentication required", 401


class RevokedKey(ApiProblem):
    slug, title, status_code = "revoked_key", "API key revoked", 403


class InsufficientScope(ApiProblem):
    slug, title, status_code = "insufficient_scope", "Insufficient scope", 403


class NotFoundProblem(ApiProblem):
    slug, title, status_code = "not_found", "Not found", 404


class CursorExpired(ApiProblem):
    slug, title, status_code = "cursor_expired", "Cursor expired", 410


class RateLimited(ApiProblem):
    slug, title, status_code = "rate_limited", "Rate limit exceeded", 429

    def __init__(self, retry_after: int, detail=None):
        super().__init__(detail or f"Retry after {retry_after} seconds.")
        self.retry_after = retry_after


def _problem(request, slug, title, status, detail, errors=None, headers=None):
    body = {
        "type": TYPE_BASE + slug,
        "title": title,
        "status": status,
        "detail": str(detail),
        "instance": request.path if request is not None else "",
    }
    if errors is not None:
        body["errors"] = errors
    return Response(body, status=status, headers=headers, content_type=CONTENT_TYPE)


def problem_exception_handler(exc, context):
    request = context.get("request")
    if isinstance(exc, ApiProblem):
        headers = {}
        if isinstance(exc, RateLimited):
            headers["Retry-After"] = str(exc.retry_after)
        if isinstance(exc, Unauthenticated):
            headers["WWW-Authenticate"] = "Bearer"
        return _problem(
            request, exc.slug, exc.title, exc.status_code, exc.detail, exc.errors, headers
        )
    if isinstance(exc, exceptions.MethodNotAllowed):
        return _problem(request, "method_not_allowed", "Method not allowed", 405, exc.detail)
    if isinstance(exc, (Http404, exceptions.NotFound)):
        return _problem(request, "not_found", "Not found", 404, "Resource not found.")
    if isinstance(exc, exceptions.ValidationError):
        return _problem(
            request,
            "invalid_parameter",
            "Invalid parameter",
            400,
            "One or more parameters are invalid.",
            errors=exc.detail,
        )
    if isinstance(exc, exceptions.Throttled):
        wait = int(exc.wait or 1)
        return _problem(
            request,
            "rate_limited",
            "Rate limit exceeded",
            429,
            exc.detail,
            headers={"Retry-After": str(wait)},
        )
    if isinstance(exc, (exceptions.NotAuthenticated, exceptions.AuthenticationFailed)):
        return _problem(
            request,
            "unauthenticated",
            "Authentication required",
            401,
            exc.detail,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if isinstance(exc, exceptions.PermissionDenied):
        return _problem(request, "insufficient_scope", "Insufficient scope", 403, exc.detail)
    if isinstance(exc, exceptions.APIException):
        return _problem(
            request, "invalid_request", "Invalid request", exc.status_code, exc.detail
        )
    return None
