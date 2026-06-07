# apps/backtest/optimizer_views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
import datetime
import logging

logger = logging.getLogger(__name__)


class OptimizerRunView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """New optimizer run start karo."""
        from apps.backtest.models import OptimizerRun
        from apps.backtest.tasks import run_optimizer_task
        from apps.backtest.optimizer import generate_grid, STRATEGY_PARAM_GRIDS

        d = request.data

        strategy_name = d.get("strategy_name")
        if not strategy_name:
            return Response({"error": "strategy_name required"}, status=400)

        symbol = d.get("symbol")
        if not symbol:
            return Response({"error": "symbol required"}, status=400)

        start_date = d.get("start_date")
        end_date   = d.get("end_date")
        if not start_date or not end_date:
            return Response({"error": "start_date and end_date required"}, status=400)

        param_ranges = d.get("param_ranges", {})
        objective    = d.get("objective", "balanced")
        train_ratio  = float(d.get("train_ratio", 0.7))
        capital      = float(d.get("initial_capital", 100000))
        timeframe    = str(d.get("timeframe", "15"))

        # Preview combinations
        grid = generate_grid(strategy_name, param_ranges)
        if len(grid) > 500:
            return Response({
                "error": f"Too many combinations: {len(grid)}. Param ranges kam karo (max 500)."
            }, status=400)

        run = OptimizerRun.objects.create(
            user=request.user,
            strategy_name=strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            initial_capital=capital,
            param_ranges=param_ranges,
            objective=objective,
            train_ratio=train_ratio,
            total_combinations=len(grid),
        )

        task = run_optimizer_task.apply_async(
            args=[str(run.id)],
            queue="strategies",
        )
        run.celery_task_id = task.id
        run.save(update_fields=["celery_task_id"])

        return Response({
            "id": str(run.id),
            "status": "queued",
            "total_combinations": len(grid),
            "estimated_minutes": round(len(grid) * 2 / 60, 1),
        }, status=201)

    def get(self, request):
        """User ke recent optimizer runs."""
        from apps.backtest.models import OptimizerRun
        runs = OptimizerRun.objects.filter(user=request.user)[:10]
        return Response({
            "runs": [_serialize_run(r) for r in runs]
        })


class OptimizerDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, run_id):
        """Single optimizer run ka detail + results."""
        from apps.backtest.models import OptimizerRun
        try:
            run = OptimizerRun.objects.get(id=run_id, user=request.user)
        except OptimizerRun.DoesNotExist:
            return Response({"error": "Not found"}, status=404)

        data = _serialize_run(run)
        if run.all_results:
            data["top_results"] = run.all_results[:20]
        return Response(data)


class OptimizerParamGridView(APIView):
    """Strategy ke default param grid preview karo."""
    permission_classes = [IsAuthenticated]

    def get(self, request, strategy_name):
        from apps.backtest.optimizer import STRATEGY_PARAM_GRIDS, generate_grid
        grid_def = STRATEGY_PARAM_GRIDS.get(
            strategy_name,
            STRATEGY_PARAM_GRIDS["default"]
        )
        combos = generate_grid(strategy_name, {})
        return Response({
            "strategy": strategy_name,
            "param_grid": grid_def,
            "total_combinations": len(combos),
        })


def _serialize_run(run) -> dict:
    return {
        "id":                   str(run.id),
        "strategy_name":        run.strategy_name,
        "symbol":               run.symbol,
        "timeframe":            run.timeframe,
        "start_date":           str(run.start_date),
        "end_date":             str(run.end_date),
        "objective":            run.objective,
        "status":               run.status,
        "progress":             run.progress,
        "total_combinations":   run.total_combinations,
        "completed_combinations": run.completed_combinations,
        "best_params":          run.best_params,
        "best_score":           run.best_score,
        "error_message":        run.error_message,
        "created_at":           run.created_at.isoformat(),
        "completed_at":         run.completed_at.isoformat() if run.completed_at else None,
    }
