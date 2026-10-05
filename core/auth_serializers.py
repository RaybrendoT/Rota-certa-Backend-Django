from datetime import timedelta

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings

from .models import AccessAccount, normalize_email
from .tokens import AccessAccountRefreshToken


class PersonBriefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    type = serializers.CharField()
    email = serializers.EmailField(source="access_account.email", read_only=True)
    role = serializers.CharField(source="access_account.role", read_only=True)
    company_id = serializers.IntegerField()
    company_name = serializers.CharField(source="company.name")


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        email = normalize_email(attrs.get("email", ""))
        password = attrs["password"]

        max_attempts = getattr(
            settings, "ACCESS_ACCOUNT_MAX_FAILED_ATTEMPTS", 5
        )
        lockout = getattr(
            settings, "ACCESS_ACCOUNT_LOCKOUT_MINUTES", 30
        )

        try:
            account = AccessAccount.objects.select_related(
                "person",
                "person__company",
            ).get(email=email)
        except AccessAccount.DoesNotExist:
            raise AuthenticationFailed(
                _("Credenciais inválidas."), code="invalid_credentials"
            )

        now = timezone.now()
        if account.blocked_until and account.blocked_until > now:
            raise AuthenticationFailed(
                _("Conta temporariamente bloqueada."), code="account_locked"
            )

        if not account.is_active or not account.person.is_active:
            raise AuthenticationFailed(
                _("Credenciais inválidas."), code="invalid_credentials"
            )

        if not account.check_password(password):
            account.failed_login_attempts += 1
            update_fields = ["failed_login_attempts", "updated_at"]
            if account.failed_login_attempts >= max_attempts:
                account.blocked_until = now + timedelta(minutes=lockout)
                update_fields.append("blocked_until")
            account.save(update_fields=update_fields)
            raise AuthenticationFailed(
                _("Credenciais inválidas."), code="invalid_credentials"
            )

        account.failed_login_attempts = 0
        account.blocked_until = None
        account.last_login = now
        account.save(
            update_fields=[
                "failed_login_attempts",
                "blocked_until",
                "last_login",
                "updated_at",
            ]
        )

        refresh = AccessAccountRefreshToken.for_access_account(account)
        attrs["access_token"] = str(refresh.access_token)
        attrs["refresh_token"] = str(refresh)
        attrs["must_change_password"] = account.must_change_password
        attrs["person"] = account.person
        return attrs


class AccessAccountTokenRefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField(write_only=True)
    access_token = serializers.CharField(read_only=True)

    def validate(self, attrs):
        try:
            refresh = AccessAccountRefreshToken(attrs["refresh"])
        except TokenError as exc:
            raise AuthenticationFailed(
                _("Token de atualização inválido."), code="token_not_valid"
            ) from exc

        try:
            raw_id = refresh[api_settings.USER_ID_CLAIM]
            account_id = int(raw_id)
        except (KeyError, ValueError, TypeError) as exc:
            raise AuthenticationFailed(
                _("Token de atualização inválido."), code="token_not_valid"
            ) from exc

        try:
            account = AccessAccount.objects.select_related(
                "person",
                "person__company",
            ).get(pk=account_id)
        except AccessAccount.DoesNotExist as exc:
            raise AuthenticationFailed(
                _("Conta não encontrada."), code="account_not_found"
            ) from exc

        if not account.is_active or not account.person.is_active:
            raise AuthenticationFailed(
                _("Conta inativa."), code="account_inactive"
            )

        return {"access_token": str(refresh.access_token)}


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_new_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value

    def validate(self, attrs):
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {
                    "new_password": _(
                        "A nova senha deve ser diferente da senha atual."
                    )
                }
            )
        account = self.context["request"].user.access_account
        if not account.check_password(attrs["current_password"]):
            raise serializers.ValidationError(
                {"current_password": _("Senha atual incorreta.")}
            )
        return attrs


def _exactly_one_identifier(attrs) -> str:
    email = (attrs.get("email") or "").strip()
    phone = (attrs.get("phone") or "").strip()
    parts = [
        ("email", bool(email)),
        ("phone", bool(phone)),
    ]
    chosen = [k for k, v in parts if v]
    if len(chosen) != 1:
        raise serializers.ValidationError(
            _("Informe exatamente um identificador: email ou phone.")
        )
    return chosen[0]


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=30)

    def validate(self, attrs):
        _exactly_one_identifier(attrs)
        return attrs


class PasswordResetVerifySerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=30)
    code = serializers.CharField(write_only=True, max_length=32)

    def validate(self, attrs):
        _exactly_one_identifier(attrs)
        return attrs


class PasswordResetCompleteSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=30)
    code = serializers.CharField(write_only=True, max_length=32)
    new_password = serializers.CharField(write_only=True)

    def validate_new_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value

    def validate(self, attrs):
        _exactly_one_identifier(attrs)
        return attrs
