from rest_framework.exceptions import PermissionDenied


class EmailNotVerified(PermissionDenied):
    """Signing in is refused until the user has opened the link in their verification email."""

    def __init__(self):
        super().__init__({
            "detail": "Please verify your email address first. Check your inbox for the link, or ask for a new one.",
            "code": "email_not_verified",
        })
        