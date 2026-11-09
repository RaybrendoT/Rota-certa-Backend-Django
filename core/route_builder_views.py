"""
API do stepper de criação/edição manual de rotas (gestor, escopo da própria empresa).

GET  route-builder/options/                      turnos, colaboradores e pontos de encontro
GET  route-builder/vehicles/?schedule=&service_date=&route=   ônibus, motorista e conflito
POST route-builder/preview/   {..., "route": id?}  paradas, horários e avisos, sem gravar
POST route-builder/                                cria a rota
GET  route-builder/{id}/                           rota no formato do stepper, com a prévia
PUT  route-builder/{id}/                           atualiza a rota
"""
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core import route_builder
from core.models import Route
from core.permissions import IsManager


class StopInput(serializers.Serializer):
    meeting_point = serializers.IntegerField()
    passengers = serializers.ListField(child=serializers.IntegerField(), max_length=200)


class RouteInput(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    kind = serializers.ChoiceField(choices=["fixed", "extra"], default="fixed")
    schedule = serializers.IntegerField()
    service_date = serializers.DateField(required=False, allow_null=True, default=None)
    vehicle = serializers.IntegerField(required=False, allow_null=True, default=None)
    passengers = serializers.ListField(child=serializers.IntegerField(), max_length=500, default=list)
    stops = StopInput(many=True, required=False, allow_null=True, default=None)
    optimize = serializers.BooleanField(default=False)
    route = serializers.IntegerField(required=False, allow_null=True, default=None)


class VehiclesQuery(serializers.Serializer):
    schedule = serializers.IntegerField()
    service_date = serializers.DateField(required=False, allow_null=True, default=None)
    route = serializers.IntegerField(required=False, allow_null=True, default=None)


class RouteBuilderViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, IsManager]

    def _company(self, request):
        return request.user.person.company

    def _route(self, request, pk):
        if pk is None:
            return None
        return get_object_or_404(Route, pk=pk, company=self._company(request), is_active=True)

    def _input(self, request):
        serializer = RouteInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def _run(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs), None
        except route_builder.BuilderError as exc:
            return None, Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"])
    def options(self, request):
        return Response(route_builder.options(self._company(request)))

    @action(detail=False, methods=["get"])
    def vehicles(self, request):
        query = VehiclesQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        result, error = self._run(
            route_builder.vehicles, self._company(request), schedule_id=params["schedule"],
            service_date=params["service_date"], route=self._route(request, params["route"]),
        )
        return error or Response(result)

    @action(detail=False, methods=["post"])
    def preview(self, request):
        data = self._input(request)
        result, error = self._run(route_builder.preview, self._company(request), data, self._route(request, data["route"]))
        return error or Response(result)

    def create(self, request):
        return self._save(request, None, status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        return Response(route_builder.load(self._route(request, pk)))

    def update(self, request, pk=None):
        return self._save(request, self._route(request, pk), status.HTTP_200_OK)

    def _save(self, request, route, ok_status):
        data = self._input(request)
        try:
            saved, result = route_builder.save(self._company(request), data, route)
        except route_builder.BuilderError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except route_builder.BuilderInvalid as exc:
            return Response(
                {"detail": "Corrija os erros antes de salvar.", "preview": exc.result},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"id": saved.id, "preview": result}, status=ok_status)
