"""Endereço residencial: o próprio usuário no app (me/address/...) e a aprovação pelo gestor (address-reviews/)."""
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from core import address_review, self_address
from core.geocoding import GeocodingUnavailable, lookup_cep
from core.models import Address
from core.permissions import IsManager


class AddressLookupThrottle(UserRateThrottle):
    """Cada conferência consulta a Geocoding API (paga) ou o ViaCEP."""

    scope = "address_lookup"


class PreviewInput(serializers.Serializer):
    mode = serializers.ChoiceField(choices=["gps", "typed"])
    # mode=gps
    latitude = serializers.FloatField(required=False, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, min_value=-180, max_value=180)
    accuracy = serializers.FloatField(required=False, min_value=0)
    is_mock = serializers.BooleanField(required=False, default=False)
    # mode=typed
    postal_code = serializers.CharField(required=False, max_length=10)
    street = serializers.CharField(required=False, max_length=100)
    number = serializers.CharField(required=False, max_length=20)
    neighborhood = serializers.CharField(required=False, max_length=100, allow_blank=True, default="")
    city = serializers.CharField(required=False, max_length=50)
    state = serializers.CharField(required=False, max_length=50)

    def validate(self, attrs):
        needed = ("latitude", "longitude", "accuracy") if attrs["mode"] == "gps" else ("postal_code", "street", "number", "city", "state")
        missing = [field for field in needed if attrs.get(field) in (None, "")]
        if missing:
            raise serializers.ValidationError({field: "Obrigatório." for field in missing})
        return attrs


class SaveInput(serializers.Serializer):
    source = serializers.ChoiceField(choices=["gps", "typed"])
    street = serializers.CharField(max_length=100)
    number = serializers.CharField(max_length=20)
    complement = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    neighborhood = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    city = serializers.CharField(max_length=50)
    state = serializers.CharField(max_length=50)
    postal_code = serializers.CharField(max_length=10)
    # Posição final do alfinete (depois de arrastar no mapa).
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    # Leitura original do GPS (source=gps): o alfinete não pode se afastar dela.
    gps_latitude = serializers.FloatField(required=False, allow_null=True, min_value=-90, max_value=90)
    gps_longitude = serializers.FloatField(required=False, allow_null=True, min_value=-180, max_value=180)
    gps_accuracy = serializers.FloatField(required=False, allow_null=True, min_value=0)
    gps_is_mock = serializers.BooleanField(required=False, default=False)


def _rejected(exc):
    code = status.HTTP_429_TOO_MANY_REQUESTS if isinstance(exc, self_address.TooManyChanges) else status.HTTP_400_BAD_REQUEST
    return Response({"detail": str(exc)}, status=code)


class MyAddressView(APIView):
    """GET → endereço atual e ponto mais próximo; PUT → grava o endereço confirmado no app."""

    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        return [AddressLookupThrottle()] if self.request.method == "PUT" else super().get_throttles()

    def get(self, request):
        return Response(self_address.current_address(request.user.person))

    def put(self, request):
        data = SaveInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            return Response(self_address.save(request.user.person, data.validated_data))
        except self_address.AddressRejected as exc:
            return _rejected(exc)
        except GeocodingUnavailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class MyAddressPreviewView(APIView):
    """POST → confere a localização atual ou o endereço digitado, sem gravar."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [AddressLookupThrottle]

    def post(self, request):
        data = PreviewInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            return Response(self_address.preview(request.user.person, data.validated_data))
        except self_address.AddressRejected as exc:
            return _rejected(exc)
        except GeocodingUnavailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class CepLookupView(APIView):
    """GET me/address/cep/<cep>/ → rua, bairro, cidade e UF do CEP (para preencher o formulário)."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [AddressLookupThrottle]

    def get(self, request, cep):
        try:
            found = lookup_cep(cep)
        except GeocodingUnavailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if found is None:
            return Response({"detail": "CEP não encontrado."}, status=status.HTTP_404_NOT_FOUND)
        return Response(found)


class ApproveInput(serializers.Serializer):
    route_id = serializers.IntegerField(required=False, allow_null=True)
    update_current_stops = serializers.BooleanField(required=False, default=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")


class RejectInput(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


class AddressReviewViewSet(viewsets.GenericViewSet):
    """
    Endereços enviados pelos colaboradores no app (gestor, escopo da própria empresa).

    GET address-reviews/?status=pending|reviewed → pedidos com antes/depois, rotas atuais e sugestões;
    POST address-reviews/{id}/approve/ {route_id?, update_current_stops?, note?};
    POST address-reviews/{id}/reject/ {reason}.
    """

    permission_classes = [IsAuthenticated, IsManager]
    pagination_class = None

    def get_queryset(self):
        return address_review.review_queryset(self.request.user.person.company)

    def list(self, request):
        qs = self.get_queryset()
        if request.query_params.get("status") == "reviewed":
            qs = qs.exclude(review_status=Address.Review.PENDING)[:50]
        else:
            qs = qs.filter(review_status=Address.Review.PENDING)
        return Response([address_review.request_payload(address) for address in qs])

    def retrieve(self, request, pk=None):
        return Response(address_review.request_payload(self.get_object()))

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        data = ApproveInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            return Response(address_review.approve(self.get_object(), request.user.person, **data.validated_data))
        except address_review.ReviewError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        data = RejectInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            return Response(address_review.reject(self.get_object(), request.user.person, data.validated_data["reason"]))
        except address_review.ReviewError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
