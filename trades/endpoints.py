import re

from django.urls import URLPattern, URLResolver, get_resolver
from django.urls.resolvers import RoutePattern
from rest_framework.permissions import AllowAny

ROUTE_PARAM = re.compile(r"<(?:\w+:)?(\w+)>")
REGEX_PARAM = re.compile(r"\(\?P<(\w+)>[^)]*\)")
SKIP_METHODS = {"options", "head"}


def _pattern_text(pattern):
    if isinstance(pattern, RoutePattern):
        return ROUTE_PARAM.sub(r"{\1}", str(pattern))
    text = REGEX_PARAM.sub(r"{\1}", pattern.regex.pattern)
    return text.replace("^", "").replace("$", "").replace("\\.", ".")


def _walk(patterns, prefix=""):
    for entry in patterns:
        if isinstance(entry, URLResolver):
            yield from _walk(entry.url_patterns, prefix + _pattern_text(entry.pattern))
        elif isinstance(entry, URLPattern):
            yield prefix + _pattern_text(entry.pattern), entry


def _first_line(*sources):
    for source in sources:
        doc = getattr(source, "__doc__", None)
        if doc and doc.strip():
            return doc.strip().splitlines()[0].strip()
    return ""


def collect():
    """Every /api/ endpoint, read from the real URL configuration."""
    rows = []
    for path, entry in _walk(get_resolver().url_patterns):
        if not path.startswith("api/") or "{format}" in path or entry.name == "api-root":
            continue
        callback = entry.callback
        cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
        if cls is None:
            continue
        allowed = {m.lower() for m in cls.http_method_names} - SKIP_METHODS
        actions = getattr(callback, "actions", None)
        if actions:  # a viewset route: {"get": "list", "post": "create"}
            handlers = {method: getattr(cls, action, None) for method, action in actions.items()}
        else:
            handlers = {m: getattr(cls, m, None) for m in allowed if hasattr(cls, m)}
        permissions = getattr(cls, "permission_classes", None) or []
        public = not permissions or any(
            isinstance(p, type) and issubclass(p, AllowAny) for p in permissions
        )
        for method, handler in handlers.items():
            if method not in allowed:
                continue
            rows.append({
                "method": method.upper(),
                "path": "/" + path,
                "view": cls.__name__,
                "name": entry.name,
                "auth": "public" if public else "authenticated",
                "throttle": getattr(cls, "throttle_scope", None),
                "summary": _first_line(handler, cls),
            })
    return sorted(rows, key=lambda r: (r["path"], r["method"]))