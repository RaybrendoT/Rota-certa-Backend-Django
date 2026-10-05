import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenViewBase, TokenVerifyView

from .auth_serializers import (
    AccessAccountTokenRefreshSerializer,
    ChangePasswordSerializer,
    LoginSerializer,
    PasswordResetCompleteSerializer,
    PasswordResetRequestSerializer,
    PasswordResetVerifySerializer,
    PersonBriefSerializer,
)
from .password_reset import resolve_access_account_for_reset
from .tokens import AccessAccountRefreshToken


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, *args, **kwargs):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        return Response(
            {
                "access_token": data["access_token"],
                "refresh_token": data["refresh_token"],
                "must_change_password": data["must_change_password"],
                "person": PersonBriefSerializer(instance=data["person"]).data,
            },
            status=status.HTTP_200_OK,
        )


class AccessAccountTokenRefreshView(TokenViewBase):
    permission_classes = [AllowAny]
    authentication_classes = []
    serializer_class = AccessAccountTokenRefreshSerializer


class AccessAccountTokenVerifyView(TokenVerifyView):
    """POST {"token": "<access ou refresh>"} — resposta 200 com corpo vazio se válido."""

    permission_classes = [AllowAny]
    authentication_classes = []


class MeView(APIView):
    """Perfil da sessão atual (mesmo formato de `person` do login), lido do banco."""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        account = request.user.access_account
        return Response(
            {
                "must_change_password": account.must_change_password,
                "person": PersonBriefSerializer(instance=account.person).data,
            },
            status=status.HTTP_200_OK,
        )


class ChangePasswordView(APIView):
    """
    Autenticado via Bearer. Atualiza a senha, zera must_change_password e
    devolve novo par de tokens (claims atualizados no cliente).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = ChangePasswordSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        account = request.user.access_account
        account.set_password(serializer.validated_data["new_password"])
        account.must_change_password = False
        account.save(
            update_fields=["password_hash", "must_change_password", "updated_at"]
        )
        refresh = AccessAccountRefreshToken.for_access_account(account)
        return Response(
            {
                "access_token": str(refresh.access_token),
                "refresh_token": str(refresh),
                "must_change_password": account.must_change_password,
                "person": PersonBriefSerializer(instance=account.person).data,
            },
            status=status.HTTP_200_OK,
        )


def _otp_digits():
    length = int(getattr(settings, "PASSWORD_RESET_CODE_LENGTH", 6))
    length = max(4, min(length, 12))
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def _resolve_from_validated(data):
    return resolve_access_account_for_reset(
        email=(data.get("email") or "").strip() or None,
        phone=(data.get("phone") or "").strip() or None,
    )


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, *args, **kwargs):
        ser = PasswordResetRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        account = _resolve_from_validated(ser.validated_data)
        body = {"status": "ok"}
        expose = getattr(
            settings,
            "PASSWORD_RESET_RETURN_CODE_IN_RESPONSE",
            getattr(settings, "DEBUG", False),
        )
        if not account:
            return Response(body, status=status.HTTP_200_OK)
        ttl = int(getattr(settings, "PASSWORD_RESET_CODE_TTL_MINUTES", 15))
        expires = timezone.now() + timedelta(minutes=ttl)
        code = _otp_digits()
        account.set_password_reset_code(code, expires)
        account.save(
            update_fields=[
                "password_reset_code_hash",
                "password_reset_expires_at",
                "password_reset_requested_at",
                "password_reset_code_used",
                "updated_at",
            ]
        )
        if expose:
            body["verificationCode"] = code
        return Response(body, status=status.HTTP_200_OK)


class PasswordResetVerifyView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, *args, **kwargs):
        ser = PasswordResetVerifySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        account = _resolve_from_validated(ser.validated_data)
        code = ser.validated_data["code"].strip()
        if not account or not account.check_password_reset_code(code):
            return Response(
                {"valid": False, "detail": "Código inválido ou expirado."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"valid": True}, status=status.HTTP_200_OK)


class PasswordResetCompleteView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, *args, **kwargs):
        ser = PasswordResetCompleteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        account = _resolve_from_validated(ser.validated_data)
        code = ser.validated_data["code"].strip()
        new_password = ser.validated_data["new_password"]
        if not account or not account.check_password_reset_code(code):
            return Response(
                {"detail": "Código inválido ou expirado."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        account.set_password(new_password)
        account.clear_password_reset()
        account.failed_login_attempts = 0
        account.blocked_until = None
        account.must_change_password = getattr(
            settings, "PASSWORD_RESET_FORCE_MUST_CHANGE_PASSWORD", False
        )
        account.save(
            update_fields=[
                "password_hash",
                "password_reset_code_hash",
                "password_reset_expires_at",
                "password_reset_requested_at",
                "password_reset_code_used",
                "failed_login_attempts",
                "blocked_until",
                "must_change_password",
                "updated_at",
            ]
        )
        return Response({"status": "ok"}, status=status.HTTP_200_OK)
