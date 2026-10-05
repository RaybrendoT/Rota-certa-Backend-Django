# api/routers.py
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .auth_views import (
    AccessAccountTokenRefreshView,
    AccessAccountTokenVerifyView,
    ChangePasswordView,
    LoginView,
    MeView,
    PasswordResetCompleteView,
    PasswordResetRequestView,
    PasswordResetVerifyView,
)
from .viewsets import (
    CompanyViewSet,
    PersonViewSet,
    RouteViewSet,
    RouteStopViewSet,
    ScheduleViewSet,
    AddressViewSet,
    MeetingPointViewSet,
    DriverViewSet,
    EmployeeViewSet,
    RoutePassengerViewSet,
    RouteEventViewSet,
    VehicleViewSet,
)

router = DefaultRouter()
router.register(r'companies', CompanyViewSet,basename='company')
router.register(r'people', PersonViewSet, basename='person')
router.register(r'routes', RouteViewSet, basename='route')
router.register(r'route-stops', RouteStopViewSet, basename='routestop')
router.register(r'schedules', ScheduleViewSet,basename='schedule')
router.register(r'addresses', AddressViewSet, basename='address')
router.register(r'meeting-points', MeetingPointViewSet,basename='meetingpoint')
router.register(r'drivers', DriverViewSet, basename='driver')
router.register(r'employees', EmployeeViewSet,basename='employee')
router.register(r'route-passengers', RoutePassengerViewSet,basename='routepassenger')
router.register(r'route-events', RouteEventViewSet,basename='event')
router.register(r"vehicles", VehicleViewSet,basename='vehicle')

urlpatterns = [
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/refresh/", AccessAccountTokenRefreshView.as_view(), name="auth-refresh"),
    path("auth/verify/", AccessAccountTokenVerifyView.as_view(), name="auth-verify"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
    path(
        "auth/change-password/",
        ChangePasswordView.as_view(),
        name="auth-change-password",
    ),
    path(
        "auth/password-reset/request/",
        PasswordResetRequestView.as_view(),
        name="auth-password-reset-request",
    ),
    path(
        "auth/password-reset/verify/",
        PasswordResetVerifyView.as_view(),
        name="auth-password-reset-verify",
    ),
    path(
        "auth/password-reset/reset/",
        PasswordResetCompleteView.as_view(),
        name="auth-password-reset-reset",
    ),
    path("", include(router.urls)),
]