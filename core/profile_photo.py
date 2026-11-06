"""
Foto do perfil da pessoa logada: PUT /api/me/photo/ (multipart, campo "photo") e DELETE.

O painel já envia a imagem recortada e reduzida (256×256, JPEG); aqui só confere o tamanho e o
tipo real pelo conteúdo (assinatura do arquivo), sem decodificar a imagem. Fica no banco
(Person.photo) e volta como data URL no perfil da sessão (login, auth/me).
"""
import base64

from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

MAX_PHOTO_BYTES = 300 * 1024


def detect_image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def photo_data_url(person) -> str | None:
    if not person.photo or not person.photo_content_type:
        return None
    return f"data:{person.photo_content_type};base64,{base64.b64encode(bytes(person.photo)).decode()}"


class MyPhotoView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def put(self, request):
        from .auth_serializers import PersonBriefSerializer

        upload = request.FILES.get("photo")
        if upload is None:
            return Response({"photo": ["Envie a imagem no campo photo."]}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > MAX_PHOTO_BYTES:
            return Response(
                {"photo": [f"A imagem pode ter no máximo {MAX_PHOTO_BYTES // 1024} KB."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = upload.read()
        content_type = detect_image_type(data)
        if content_type is None:
            return Response({"photo": ["Use uma imagem JPEG, PNG ou WebP."]}, status=status.HTTP_400_BAD_REQUEST)
        person = request.user.person
        person.photo = data
        person.photo_content_type = content_type
        person.photo_updated_at = timezone.now()
        person.save(update_fields=["photo", "photo_content_type", "photo_updated_at", "updated_at"])
        return Response({"person": PersonBriefSerializer(instance=person).data})

    def delete(self, request):
        from .auth_serializers import PersonBriefSerializer

        person = request.user.person
        person.photo = None
        person.photo_content_type = ""
        person.photo_updated_at = timezone.now()
        person.save(update_fields=["photo", "photo_content_type", "photo_updated_at", "updated_at"])
        return Response({"person": PersonBriefSerializer(instance=person).data})
