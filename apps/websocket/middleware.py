# apps/websocket/middleware.py

from channels.middleware import BaseMiddleware


class JWTAuthMiddleware(BaseMiddleware):
    async def __call__(self, scope, receive, send):
        from urllib.parse import parse_qs

        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import AnonymousUser

        from channels.db import database_sync_to_async
        from rest_framework_simplejwt.exceptions import TokenError
        from rest_framework_simplejwt.tokens import AccessToken

        qs = parse_qs(scope.get("query_string", b"").decode())
        token = qs.get("token", [None])[0]

        if token:
            try:
                validated = AccessToken(token)
                User = get_user_model()
                user = await database_sync_to_async(User.objects.get)(
                    pk=validated["user_id"]
                )
                scope["user"] = user
            except (TokenError, Exception):
                scope["user"] = AnonymousUser()
        else:
            scope["user"] = AnonymousUser()

        return await super().__call__(scope, receive, send)


def JWTAuthMiddlewareStack(inner):
    return JWTAuthMiddleware(inner)
