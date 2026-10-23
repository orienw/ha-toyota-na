"""Authentication failures shared by the client and integration."""


class AuthError(Exception):
    """An authentication operation failed."""


class NotLoggedIn(AuthError):
    """A request requires a signed-in account."""


class TokenExpired(AuthError):
    """The current credentials must be refreshed or replaced."""


class LoginError(AuthError):
    """Toyota rejected a sign-in attempt."""
