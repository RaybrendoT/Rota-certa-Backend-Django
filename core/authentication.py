from django.utils.translation import gettext_lazy as _
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import Token

from .models import AccessAccount


class AccessAccountPrincipal:
    """Representa a sessão autenticada sem usar o modelo User do Django."""

    is_authenticated = True

    def __init__(self, access_account: AccessAccount):
        self.access_account = access_account
        self.person = access_account.person
        self.role = access_account.role
        self.pk = access_account.pk
        self.id = access_account.pk

    @property
    def is_active(self) -> bool:
        return self.access_account.is_active and self.person.is_active


class AccessAccountJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token: Token) -> AccessAccountPrincipal:
        try:
            raw_id = validated_token[api_settings.USER_ID_CLAIM]
            account_id = int(raw_id)
        except (KeyError, ValueError, TypeError) as exc:
            raise InvalidToken(
                _("Token contained no recognizable account identification")
            ) from exc

        try:
            account = AccessAccount.objects.select_related("person").get(pk=account_id)
        except AccessAccount.DoesNotExist as exc:
            raise AuthenticationFailed(
                _("Account not found"), code="account_not_found"
            ) from exc

        if api_settings.CHECK_USER_IS_ACTIVE and (
            not account.is_active or not account.person.is_active
        ):
            raise AuthenticationFailed(_("Account is inactive"), code="account_inactive")

        return AccessAccountPrincipal(account)
