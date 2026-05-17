# apps/websocket/auth.py

import logging
from urllib.parse import parse_qs

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser

from asgiref.sync import sync_to_async
from channels.middleware import BaseMiddleware
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import AccessToken

User = get_user_model()
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
#  JWTAuthMiddleware
#
#  WebSocket handshake pe JWT token validate karta hai.
#  Token do jagah se accept karta hai:
#    1. Query string  →  ws://host/ws/market/?token=<JWT>
#    2. Subprotocol   →  Sec-WebSocket-Protocol: access_token, <JWT>
#       (kuch frontend libraries headers ki jagah subprotocol use karti hain)
#
#  Token valid ho  →  scope["user"] = <User instance>
#  Token invalid/missing  →  scope["user"] = AnonymousUser()
# ─────────────────────────────────────────────────────────────────
class JWTAuthMiddleware(BaseMiddleware):

    async def __call__(self, scope, receive, send):
        token_str = self._extract_token(scope)

        if token_str:
            scope["user"] = await self._authenticate(token_str)
        else:
            scope["user"] = AnonymousUser()

        return await super().__call__(scope, receive, send)

    # ── Token extraction ────────────────────────────────────────

    def _extract_token(self, scope) -> str | None:
        """
        Priority order:
          1. ?token=... query param
          2. Sec-WebSocket-Protocol header
        """
        # 1. Query string
        query_string = scope.get("query_string", b"").decode("utf-8")
        params = parse_qs(query_string)
        token_list = params.get("token")
        if token_list:
            return token_list[0]

        # 2. WebSocket subprotocol header
        #    e.g. ["access_token", "<JWT>"]
        headers = dict(scope.get("headers", []))
        subprotocol_header = headers.get(b"sec-websocket-protocol", b"").decode("utf-8")
        if subprotocol_header:
            parts = [p.strip() for p in subprotocol_header.split(",")]
            # Expect: "access_token, <actual_token>"
            if len(parts) == 2 and parts[0] == "access_token":
                return parts[1]

        return None

    # ── Token validation ────────────────────────────────────────

    async def _authenticate(self, token_str: str):
        """
        Token validate karke User object return karta hai.
        Koi bhi error hone par AnonymousUser return hota hai.
        """
        try:
            access_token = AccessToken(token_str)
            user_id = access_token["user_id"]
            user = await self._get_user(user_id)
            logger.debug("JWT auth success | user_id=%s", user_id)
            return user
        except (TokenError, InvalidToken) as exc:
            logger.warning("JWT auth failed | reason=%s", exc)
            return AnonymousUser()
        except Exception as exc:
            logger.error("JWT auth unexpected error | %s", exc)
            return AnonymousUser()

    @sync_to_async
    def _get_user(self, user_id):
        """
        ORM call sync hai, isliye sync_to_async wrap kiya.
        Inactive user ko bhi AnonymousUser treat karo.
        """
        try:
            user = User.objects.get(pk=user_id, is_active=True)
            return user
        except User.DoesNotExist:
            return AnonymousUser()


# ─────────────────────────────────────────────────────────────────
#  Convenience wrapper — asgi.py / routing.py mein use karo
# ─────────────────────────────────────────────────────────────────
def JWTAuthMiddlewareStack(inner):
    """
    Usage in config/asgi.py:

        from apps.websocket.auth import JWTAuthMiddlewareStack

        application = ProtocolTypeRouter({
            "http": get_asgi_application(),
            "websocket": JWTAuthMiddlewareStack(
                URLRouter(websocket_urlpatterns)
            ),
        })
    """
    return JWTAuthMiddleware(inner)
