# apps/predictions/tasks.py

import logging
from celery import shared_task
from django.contrib.auth import get_user_model

User = get_user_model()
logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = [
    "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX",
    "NSE:FINNIFTY-INDEX",
    "BSE:SENSEX-INDEX",
]


@shared_task(name="predictions.generate_eod_predictions", queue="default")
def generate_eod_predictions():
    """
    Run every day at 3:45 PM IST after market close.
    Generates next day predictions for all default symbols.
    """
    from apps.predictions.engine import generate_prediction

    user = User.objects.filter(is_staff=True).first()
    if not user:
        user = User.objects.first()
    if not user:
        logger.error("No user found for prediction generation")
        return

    results = []
    for symbol in DEFAULT_SYMBOLS:
        try:
            prediction = generate_prediction(symbol=symbol, user=user)
            if prediction:
                results.append({
                    "symbol": symbol,
                    "bias": prediction.bias,
                    "score": prediction.final_score,
                    "date": str(prediction.prediction_date),
                })
                logger.info("Prediction generated | %s | %s | %.0f",
                    symbol, prediction.bias, prediction.final_score)
        except Exception as e:
            logger.error("Prediction failed | %s | %s", symbol, e)

    logger.info("EOD predictions complete | %d symbols", len(results))
    return results


@shared_task(name="predictions.generate_single_prediction", queue="default")
def generate_single_prediction(symbol: str, user_id: str):
    """Generate prediction for a single symbol on demand."""
    from apps.predictions.engine import generate_prediction

    try:
        user = User.objects.get(pk=user_id)
        prediction = generate_prediction(
            symbol=symbol, user=user, force_regenerate=True
        )
        if prediction:
            return {
                "symbol": symbol,
                "bias": prediction.bias,
                "score": prediction.final_score,
                "date": str(prediction.prediction_date),
            }
    except Exception as e:
        logger.error("Single prediction failed | %s | %s", symbol, e)
    return None


@shared_task(name="predictions.update_prediction_outcomes", queue="default")
def update_prediction_outcomes():
    """
    Run every day at 3:35 PM to check if yesterday predictions were correct.
    """
    from datetime import date, timedelta
    from apps.predictions.models import DailyPrediction
    from apps.market.models import Asset

    yesterday = date.today() - timedelta(days=1)
    predictions = DailyPrediction.objects.filter(
        prediction_date=date.today(),
        was_correct=None,
    )

    for pred in predictions:
        try:
            asset = Asset.objects.filter(symbol__icontains=pred.symbol).first()
            if not asset or not asset.last_price:
                continue

            current_price = float(asset.last_price)
            entry_mid = (pred.entry_zone_high + pred.entry_zone_low) / 2 if pred.entry_zone_high and pred.entry_zone_low else None

            if entry_mid is None:
                continue

            if pred.bias == "bullish":
                was_correct = current_price >= pred.target_1 if pred.target_1 else None
                actual_move = round(((current_price - entry_mid) / entry_mid) * 100, 2)
            elif pred.bias == "bearish":
                was_correct = current_price <= pred.target_1 if pred.target_1 else None
                actual_move = round(((entry_mid - current_price) / entry_mid) * 100, 2)
            else:
                continue

            pred.actual_move = actual_move
            pred.was_correct = was_correct
            pred.save(update_fields=["actual_move", "was_correct"])
            logger.info("Outcome updated | %s | correct=%s | move=%.2f%%",
                pred.symbol, was_correct, actual_move)

        except Exception as e:
            logger.error("Outcome update failed | %s | %s", pred.symbol, e)
