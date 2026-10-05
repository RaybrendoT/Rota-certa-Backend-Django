from rest_framework import permissions

from .models import AccessAccount


def _role(request):
    return getattr(request.user, "role", None)


class IsManager(permissions.BasePermission):
    """Apenas contas com perfil MANAGER (gestor)."""

    message = "Apenas gestores podem acessar este recurso."

    def has_permission(self, request, view):
        return _role(request) == AccessAccount.Role.MANAGER


class IsGerente(permissions.BasePermission):
    """Apenas contas com perfil GERENTE (cadastro de empresas e de gestores)."""

    message = "Apenas gerentes podem acessar este recurso."

    def has_permission(self, request, view):
        return _role(request) == AccessAccount.Role.GERENTE


class IsManagerOrGerente(permissions.BasePermission):
    """Gestor (escopo da própria empresa) ou gerente (cadastra pessoas em qualquer empresa)."""

    message = "Apenas gestores e gerentes podem acessar este recurso."

    def has_permission(self, request, view):
        return _role(request) in (
            AccessAccount.Role.MANAGER,
            AccessAccount.Role.GERENTE,
        )


class IsDriver(permissions.BasePermission):
    """Apenas contas com perfil DRIVER (motorista)."""

    message = "Apenas motoristas podem acessar este recurso."

    def has_permission(self, request, view):
        return _role(request) == AccessAccount.Role.DRIVER


class IsManagerOrDriver(permissions.BasePermission):
    """Gestor (acesso total) ou motorista (o viewset restringe ao que é dele)."""

    message = "Apenas gestores e motoristas podem acessar este recurso."

    def has_permission(self, request, view):
        return _role(request) in (
            AccessAccount.Role.MANAGER,
            AccessAccount.Role.DRIVER,
        )


def is_driver(request) -> bool:
    return _role(request) == AccessAccount.Role.DRIVER


def is_manager(request) -> bool:
    return _role(request) == AccessAccount.Role.MANAGER


def route_belongs_to_driver(route, person) -> bool:
    """A rota é do motorista quando o veículo atribuído a ela é dele."""
    return bool(route.vehicle_id and route.vehicle.driver_id == person.id)
