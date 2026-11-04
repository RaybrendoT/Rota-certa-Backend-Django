import anthropic
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from core.models import PlanningConversation, Schedule
from core.pickup_points import demand_map, suggest_pickup_points
from core.permissions import IsManager
from core.planning.agent import AssistantUnavailable, PlanningAgent, transcript
from core.planning.employee_list import MAX_BYTES, EmployeeListError, match_employee_list
from core.planning.planner import Planner, PlanningError


class PlanningMessageThrottle(UserRateThrottle):
    """Cada mensagem pode virar várias chamadas pagas ao Claude."""

    scope = "planning_message"


class MessageInputSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=20000, trim_whitespace=True)


def conversation_payload(conversation):
    draft = None
    if conversation.draft:
        try:
            draft = Planner(conversation).describe()
        except PlanningError as exc:
            draft = {"error": str(exc)}
    return {
        "id": conversation.id,
        "status": conversation.status,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "approved_at": conversation.approved_at,
        "messages": transcript(conversation),
        "plan": draft,
        "routes": [{"id": r.id, "name": r.name} for r in conversation.routes.order_by("id")],
        "usage": {"input_tokens": conversation.input_tokens, "output_tokens": conversation.output_tokens},
    }


class PlanningConversationViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet
):
    """
    Planejamento de rotas avulsas com o assistente (gestor, escopo da própria empresa).

    POST planning/ → nova conversa; POST planning/{id}/messages/ {"text"} → resposta do
    assistente e plano atual; POST planning/{id}/approve/ → grava as rotas do dia e avisa
    motoristas e passageiros.
    """

    permission_classes = [IsAuthenticated, IsManager]
    pagination_class = None
    filter_backends = []

    def get_queryset(self):
        return PlanningConversation.objects.filter(company_id=self.request.user.person.company_id)

    def list(self, request):
        items = [
            {"id": c.id, "status": c.status, "updated_at": c.updated_at, "has_plan": bool(c.draft)}
            for c in self.get_queryset()[:50]
        ]
        return Response(items)

    def retrieve(self, request, pk=None):
        return Response(conversation_payload(self.get_object()))

    def create(self, request):
        person = request.user.person
        conversation = PlanningConversation.objects.create(created_by=person, company_id=person.company_id)
        return Response(conversation_payload(conversation), status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], throttle_classes=[PlanningMessageThrottle])
    def messages(self, request, pk=None):
        conversation = self.get_object()
        if conversation.status != PlanningConversation.Status.DRAFT:
            return Response(
                {"detail": "Este plano já foi aprovado; abra um novo planejamento."},
                status=status.HTTP_409_CONFLICT,
            )
        data = MessageInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            reply = PlanningAgent(conversation).reply(data.validated_data["text"])
        except AssistantUnavailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except anthropic.RateLimitError:
            return Response(
                {"detail": "Assistente sobrecarregado no momento; tente de novo em instantes."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except anthropic.APIError:
            return Response(
                {"detail": "Falha ao falar com o assistente; tente de novo."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        payload = conversation_payload(conversation)
        payload["reply"] = reply
        return Response(payload)

    @action(detail=False, methods=["post"], url_path="employee-list", parser_classes=[MultiPartParser])
    def employee_list(self, request):
        """POST planning/employee-list/ (multipart "file", .csv ou .xlsx) → linhas conferidas com o cadastro."""
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "Envie o arquivo no campo \"file\"."}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > MAX_BYTES:
            return Response({"detail": "Arquivo maior que 2 MB."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = match_employee_list(request.user.person.company, upload.name, upload.read())
        except EmployeeListError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        conversation = self.get_object()
        try:
            Planner(conversation).approve()
        except PlanningError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(conversation_payload(conversation))


class PickupSuggestionsQuery(serializers.Serializer):
    walk_limit = serializers.IntegerField(min_value=200, max_value=5000, default=1000)
    min_people = serializers.IntegerField(min_value=2, max_value=200, default=5)


class PickupSuggestionsView(APIView):
    """GET pickup-suggestions/?walk_limit=1000&min_people=5 — novos pontos de embarque sugeridos (H3)."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        query = PickupSuggestionsQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(suggest_pickup_points(request.user.person.company, **query.validated_data))


class DemandMapQuery(serializers.Serializer):
    schedule = serializers.IntegerField(required=False)
    resolution = serializers.IntegerField(min_value=6, max_value=10, default=8)


class DemandMapView(APIView):
    """GET demand-map/?schedule=<id>&resolution=8 — colaboradores por hexágono H3 (mapa de calor)."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        query = DemandMapQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        company = request.user.person.company
        schedule_id = query.validated_data.get("schedule")
        if schedule_id is not None and not Schedule.objects.filter(pk=schedule_id, company=company).exists():
            return Response({"schedule": ["Turno não encontrado nesta empresa."]}, status=status.HTTP_400_BAD_REQUEST)
        cells = demand_map(company, schedule_id=schedule_id, resolution=query.validated_data["resolution"])
        return Response({"resolution": query.validated_data["resolution"], "cells": cells})
