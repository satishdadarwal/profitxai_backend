# apps/predictions/views.py

import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import permissions
from django.utils import timezone

logger = logging.getLogger(__name__)


class PredictionListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.predictions.models import DailyPrediction
        from datetime import date

        symbol = request.query_params.get("symbol")
        limit  = int(request.query_params.get("limit", 10))

        qs = DailyPrediction.objects.all()
        if symbol:
            qs = qs.filter(symbol__icontains=symbol)
        qs = qs[:limit]

        data = []
        for p in qs:
            data.append({
                "id":               str(p.id),
                "symbol":           p.symbol,
                "prediction_date":  str(p.prediction_date),
                "bias":             p.bias,
                "confidence":       p.confidence,
                "final_score":      p.final_score,
                "confluence_score": p.confluence_score,
                "global_score":     p.global_score,
                "news_sentiment":   p.news_sentiment,
                "entry_zone_high":  p.entry_zone_high,
                "entry_zone_low":   p.entry_zone_low,
                "stop_loss":        p.stop_loss,
                "target_1":         p.target_1,
                "target_2":         p.target_2,
                "target_3":         p.target_3,
                "key_levels":       p.key_levels[:10],
                "top_news":         p.top_news[:3],
                "trade_plan":       p.trade_plan,
                "mtf_analysis":     p.mtf_analysis,
                "summary":          p.summary,
                "was_correct":      p.was_correct,
                "actual_move":      p.actual_move,
                "generated_at":     p.generated_at.isoformat(),
            })

        return Response({"predictions": data, "count": len(data)})


class PredictionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, symbol):
        from apps.predictions.models import DailyPrediction
        from datetime import date

        prediction_date = request.query_params.get("date")
        if prediction_date:
            from datetime import datetime
            target_date = datetime.strptime(prediction_date, "%Y-%m-%d").date()
        else:
            target_date = date.today()

        pred = DailyPrediction.objects.filter(
            symbol__icontains=symbol
        ).order_by("-prediction_date").first()

        if not pred:
            return Response({"error": "Prediction not found"}, status=404)

        return Response({
            "id":               str(pred.id),
            "symbol":           pred.symbol,
            "prediction_date":  str(pred.prediction_date),
            "bias":             pred.bias,
            "confidence":       pred.confidence,
            "final_score":      pred.final_score,
            "confluence_score": pred.confluence_score,
            "global_score":     pred.global_score,
            "news_sentiment":   pred.news_sentiment,
            "entry_zone_high":  pred.entry_zone_high,
            "entry_zone_low":   pred.entry_zone_low,
            "stop_loss":        pred.stop_loss,
            "target_1":         pred.target_1,
            "target_2":         pred.target_2,
            "target_3":         pred.target_3,
            "key_levels":       pred.key_levels,
            "top_news":         pred.top_news,
            "trade_plan":       pred.trade_plan,
            "mtf_analysis":     pred.mtf_analysis,
            "global_cues":      pred.global_cues,
            "summary":          pred.summary,
            "was_correct":      pred.was_correct,
            "actual_move":      pred.actual_move,
            "generated_at":     pred.generated_at.isoformat(),
        })


class GeneratePredictionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        symbol = request.data.get("symbol", "NSE:NIFTY50-INDEX")
        force  = request.data.get("force", False)

        try:
            from apps.predictions.engine import generate_prediction
            prediction = generate_prediction(
                symbol=symbol,
                user=request.user,
                force_regenerate=force,
            )

            if prediction:
                return Response({
                    "success": True,
                    "symbol":  prediction.symbol,
                    "bias":    prediction.bias,
                    "score":   prediction.final_score,
                    "date":    str(prediction.prediction_date),
                    "summary": prediction.summary,
                })

            # ✅ None returned = candle data unavailable, not a server crash
            logger.warning("generate_prediction returned None | symbol=%s", symbol)
            return Response(
                {
                    "success": False,
                    "error": (
                        f"Market data unavailable for {symbol}. "
                        "For crypto: check Delta Exchange connection. "
                        "For equity: check Fyers broker connection."
                    ),
                },
                status=400,
            )

        except Exception as e:
            logger.error("Generate prediction failed | %s | %s", symbol, e, exc_info=True)
            return Response({"success": False, "error": str(e)}, status=500)


class GlobalCuesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.predictions.models import GlobalCueSnapshot

        latest = GlobalCueSnapshot.objects.order_by("-date").first()
        if not latest:
            return Response({"error": "No global cues available"}, status=404)

        return Response({
            "date":             str(latest.date),
            "global_score":     latest.global_score,
            "sp500":            {"close": latest.sp500_close, "chg_pct": latest.sp500_chg_pct},
            "dow":              {"close": latest.dow_close,   "chg_pct": latest.dow_chg_pct},
            "nasdaq":           {"close": latest.nasdaq_close,"chg_pct": latest.nasdaq_chg_pct},
            "nikkei":           {"close": latest.nikkei_close,"chg_pct": latest.nikkei_chg_pct},
            "crude_oil":        {"close": latest.crude_oil,   "chg_pct": latest.crude_chg_pct},
            "gold":             {"close": latest.gold,        "chg_pct": latest.gold_chg_pct},
            "vix_india":        latest.vix_india,
            "vix_us":           latest.vix_us,
            "dxy":              {"close": latest.dxy,         "chg_pct": latest.dxy_chg_pct},
            "fii_net":          latest.fii_net,
            "dii_net":          latest.dii_net,
            "fetched_at":       latest.fetched_at.isoformat(),
        })


class PredictionAccuracyView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.predictions.models import DailyPrediction
        from django.db.models import Count, Q

        symbol = request.query_params.get("symbol")
        qs = DailyPrediction.objects.filter(was_correct__isnull=False)
        if symbol:
            qs = qs.filter(symbol__icontains=symbol)

        total   = qs.count()
        correct = qs.filter(was_correct=True).count()
        accuracy = round(correct / total * 100, 1) if total > 0 else 0

        by_confidence = {}
        for conf in ["high", "medium", "low"]:
            conf_qs      = qs.filter(confidence=conf)
            conf_total   = conf_qs.count()
            conf_correct = conf_qs.filter(was_correct=True).count()
            by_confidence[conf] = {
                "total":    conf_total,
                "correct":  conf_correct,
                "accuracy": round(conf_correct / conf_total * 100, 1) if conf_total > 0 else 0,
            }

        return Response({
            "total":         total,
            "correct":       correct,
            "accuracy_pct":  accuracy,
            "by_confidence": by_confidence,
        })

class HourlyPredictionListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.predictions.models import HourlyPrediction
        symbol = request.query_params.get("symbol")
        qs = HourlyPrediction.objects.all().order_by("-prediction_hour")[:48]
        if symbol:
            qs = HourlyPrediction.objects.filter(symbol__iexact=symbol).order_by("-prediction_hour")[:48]
        data = list(qs.values(
            "id", "symbol", "prediction_hour", "bias",
            "confidence_pct", "confluence_score",
            "entry_zone_high", "entry_zone_low",
            "stop_loss", "target_1", "target_2",
            "summary", "ict_breakdown", "trade_plan",
        ))
        return Response(data)


class HourlyAccuracyView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.predictions.models import HourlyPrediction
        from django.db.models import Count, Q
        symbol = request.query_params.get("symbol")
        qs = HourlyPrediction.objects.all()
        if symbol:
            qs = qs.filter(symbol__iexact=symbol)
        total = qs.count()
        correct = qs.filter(was_correct=True).count()
        accuracy = round((correct / total) * 100, 1) if total else 0
        return Response({"total": total, "correct": correct, "accuracy_pct": accuracy})
