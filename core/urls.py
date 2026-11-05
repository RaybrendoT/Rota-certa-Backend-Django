# api/routers.py
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .address_views import AddressReviewViewSet, CepLookupView, MyAddressPreviewView, MyAddressView
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
from .dashboard import (
    DashboardAlertsView,
    DashboardExportView,
    DashboardFeedView,
    DashboardMapView,
    DashboardSummaryView,
    DashboardTrendsView,
)
from .planning.views import DemandMapView, PickupSuggestionsView, PlanningConversationViewSet
from .push_views import PushDeviceUnregisterView, PushDeviceView
from .views import HealthView
from .viewsets import (
    AddressViewSet,
    CollaboratorRouteViewSet,
    CompanyViewSet,
    DriverViewSet,
    EmployeeViewSet,
    MeetingPointViewSet,
    NotificationViewSet,
    PersonViewSet,
    RouteEventViewSet,
    RoutePassengerViewSet,
    RouteStopViewSet,
    RouteViewSet,
    ScheduleViewSet,
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
router.register(r"me/routes", CollaboratorRouteViewSet, basename='collaborator-route')
router.register(r"notifications", NotificationViewSet, basename='notification')
router.register(r"planning", PlanningConversationViewSet, basename='planning')
router.register(r"address-reviews", AddressReviewViewSet, basename='address-review')

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/refresh/", AccessAccountTokenRefreshView.as_view(), name="auth-refresh"),
    path("auth/verify/", AccessAccountTokenVerifyView.as_view(), name="auth-verify"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
    path("me/devices/", PushDeviceView.as_view(), name="me-devices"),
    path("me/devices/unregister/", PushDeviceUnregisterView.as_view(), name="me-devices-unregister"),
    path("me/address/", MyAddressView.as_view(), name="me-address"),
    path("me/address/preview/", MyAddressPreviewView.as_view(), name="me-address-preview"),
    path("me/address/cep/<str:cep>/", CepLookupView.as_view(), name="me-address-cep"),
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
    path("pickup-suggestions/", PickupSuggestionsView.as_view(), name="pickup-suggestions"),
    path("demand-map/", DemandMapView.as_view(), name="demand-map"),
    path("dashboard/summary/", DashboardSummaryView.as_view(), name="dashboard-summary"),
    path("dashboard/alerts/", DashboardAlertsView.as_view(), name="dashboard-alerts"),
    path("dashboard/feed/", DashboardFeedView.as_view(), name="dashboard-feed"),
    path("dashboard/map/", DashboardMapView.as_view(), name="dashboard-map"),
    path("dashboard/trends/", DashboardTrendsView.as_view(), name="dashboard-trends"),
    path("dashboard/export/", DashboardExportView.as_view(), name="dashboard-export"),
    path("", include(router.urls)),
]
