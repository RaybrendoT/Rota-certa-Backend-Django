from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import push
from .models import PushDevice


class PushDeviceInput(serializers.Serializer):
    """Token do Firebase Cloud Messaging do aparelho (FirebaseMessaging.getInstance().token)."""

    token = serializers.CharField(max_length=512, trim_whitespace=True)
    platform = serializers.ChoiceField(choices=PushDevice.Platform.choices, default=PushDevice.Platform.ANDROID)


class PushDeviceView(APIView):
    """
    GET  /api/me/devices/ → {"push_enabled": bool, "devices": n} (o servidor tem o Firebase configurado?)
    POST /api/me/devices/ {"token": "...", "platform": "android"} → registra ou renova o aparelho.
    Chame depois do login e em onNewToken. Um token que estava com outra pessoa passa para a logada.
    Vale para colaborador, motorista e gestor: cada um recebe as próprias notificações.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        person = request.user.person
        return Response({
            "push_enabled": push.is_enabled(),
            "devices": PushDevice.objects.filter(person=person, is_active=True).count(),
        })

    def post(self, request):
        data = PushDeviceInput(data=request.data)
        data.is_valid(raise_exception=True)
        device, created = PushDevice.objects.update_or_create(
            token=data.validated_data["token"],
            defaults={
                "person": request.user.person,
                "platform": data.validated_data["platform"],
                "is_active": True,
                "deactivated_reason": "",
                "last_seen_at": timezone.now(),
            },
        )
        return Response(
            {"id": device.id, "platform": device.platform, "is_active": device.is_active},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class PushDeviceUnregisterView(APIView):
    """
    POST /api/me/devices/unregister/ {"token": "..."} → para de mandar push para o aparelho.
    Chame no logout, antes de apagar o JWT. Responde 204 mesmo se o token não existir.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = str(request.data.get("token") or "").strip()
        if not token:
            return Response({"token": ["Este campo é obrigatório."]}, status=status.HTTP_400_BAD_REQUEST)
        PushDevice.objects.filter(token=token, person=request.user.person).update(
            is_active=False, deactivated_reason="removido pelo app",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
