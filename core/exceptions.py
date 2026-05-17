from rest_framework.views import exception_handler
from rest_framework.response import Response


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        response.data = {
            "success": False,
            "message": _flatten(response.data),
            "status": response.status_code,
        }
    return response


def _flatten(data):
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return " | ".join(str(i) for i in data)
    if isinstance(data, dict):
        parts = []
        for k, v in data.items():
            parts.append(f"{k}: {_flatten(v)}")
        return " | ".join(parts)
    return str(data)
