import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Transaction, Wallet
from .serializers import (
    DepositSerializer,
    TransactionSerializer,
    WalletSerializer,
    WithdrawSerializer,
)
from .services import (
    InsufficientFundsError,
    InvalidAmountError,
    WalletNotFoundError,
    deposit,
    get_balance_summary,
    get_or_create_wallet,
    withdraw,
)

logger = logging.getLogger(__name__)


class WalletListView(APIView):
    """
    GET /api/wallet/
    User ke saare wallets aur balances dikhao.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        summary = get_balance_summary(user=request.user)
        return Response({"wallets": summary})


class WalletDetailView(APIView):
    """
    GET /api/wallet/<currency>/
    Ek specific currency ka wallet dikhao.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, currency):
        wallet = get_or_create_wallet(user=request.user, currency=currency.upper())
        return Response(WalletSerializer(wallet).data)


class DepositView(APIView):
    """
    POST /api/wallet/deposit/
    User ke wallet mein paisa add karo.

    Body:
        amount    : Decimal (required)
        currency  : str     (default USDT)
        reference : str     (optional)
        notes     : str     (optional)
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = DepositSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            txn = deposit(user=request.user, **ser.validated_data)
        except InvalidAmountError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(TransactionSerializer(txn).data, status=status.HTTP_201_CREATED)


class WithdrawView(APIView):
    """
    POST /api/wallet/withdraw/
    User ke wallet se paisa nikalo.

    Body:
        amount    : Decimal (required)
        currency  : str     (default USDT)
        fee       : Decimal (default 0)
        reference : str     (optional)
        notes     : str     (optional)
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = WithdrawSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            txn = withdraw(user=request.user, **ser.validated_data)
        except InvalidAmountError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except InsufficientFundsError as exc:
            return Response(
                {"error": str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED
            )
        except WalletNotFoundError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)

        return Response(TransactionSerializer(txn).data, status=status.HTTP_201_CREATED)


class TransactionListView(APIView):
    """
    GET /api/wallet/transactions/
    User ki transaction history.

    Query params:
        currency : filter by currency (optional)
        tx_type  : filter by type (optional)
        limit    : max results (default 50, max 500)
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Transaction.objects.filter(wallet__user=request.user).select_related(
            "wallet"
        )

        # Optional filters
        if currency := request.query_params.get("currency"):
            qs = qs.filter(wallet__currency=currency.upper())

        if tx_type := request.query_params.get("tx_type"):
            qs = qs.filter(transaction_type=tx_type)

        limit = min(int(request.query_params.get("limit", 50)), 500)
        qs = qs.order_by("-created_at")[:limit]

        return Response(
            {
                "count": qs.count(),
                "transactions": TransactionSerializer(qs, many=True).data,
            }
        )
