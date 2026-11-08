import contextvars
import logging
import re
import time
import uuid

request_id_var = contextvars.ContextVar("request_id", default="-")
user_id_var = contextvars.ContextVar("user_id", default="-")

SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
logger = logging.getLogger("config.request")
_installed = False


def install_log_fields():
    """Make every log record carry the current request id and user id."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.request_id = request_id_var.get()
        record.user_id = user_id_var.get()
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


def set_user(user_id):
    user_id_var.set(str(user_id))


class RequestLogMiddleware:
    """Give each request an id, send it back in a header, and log one line per request (never the query string)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        supplied = request.META.get("HTTP_X_REQUEST_ID", "")
        request_id = supplied if SAFE_ID.match(supplied) else uuid.uuid4().hex
        request_id_var.set(request_id)
        user_id_var.set("-")  # worker threads are reused: start every request clean
        started = time.monotonic()
        response = self.get_response(request)
        response["X-Request-ID"] = request_id
        millis = int((time.monotonic() - started) * 1000)
        level = logging.DEBUG if request.path == "/api/health/" else logging.INFO  # health checks would flood the log
        logger.log(level, "%s %s %s %dms", request.method, request.path, response.status_code, millis)
        return response