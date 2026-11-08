from rest_framework_simplejwt.authentication import JWTAuthentication

from config.observability import set_user


class TrackedJWTAuthentication(JWTAuthentication):
    """The normal token check, plus a note of the user id for the log lines."""

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            set_user(result[0].pk)
        return result