# apps/options/prediction_views.py
# API views for options prediction lookup and refresh

from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.options.options_prediction import generate_options_prediction
from apps.options.models import OptionsPrediction
from apps.options.serializers import OptionsPredictionSerializer


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def latest_options_prediction(request, symbol: str):
    try:
        prediction = OptionsPrediction.objects.filter(symbol__name__iexact=symbol).order_by("-created_at").first()
        if not prediction:
            return Response({"detail": "No prediction found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = OptionsPredictionSerializer(prediction)
        return Response(serializer.data)
    except Exception as e:
        return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def refresh_options_prediction(request, symbol: str):
    user = request.user
    prediction = generate_options_prediction(symbol_name=symbol, user=user)
    if not prediction:
        return Response({"detail": "Prediction generation failed."}, status=status.HTTP_400_BAD_REQUEST)

    serializer = OptionsPredictionSerializer(prediction)
    return Response(serializer.data, status=status.HTTP_201_CREATED)
