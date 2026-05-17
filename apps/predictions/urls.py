# apps/predictions/urls.py

from django.urls import path
from .views import (
    PredictionListView,
    PredictionDetailView,
    GeneratePredictionView,
    GlobalCuesView,
    PredictionAccuracyView,
)

urlpatterns = [
    path("",              PredictionListView.as_view(),      name="prediction-list"),
    path("generate/",     GeneratePredictionView.as_view(),  name="generate-prediction"),
    path("global-cues/",  GlobalCuesView.as_view(),          name="global-cues"),
    path("accuracy/",     PredictionAccuracyView.as_view(),  name="prediction-accuracy"),
    path("<str:symbol>/", PredictionDetailView.as_view(),    name="prediction-detail"),
]
