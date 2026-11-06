from django.contrib import admin, messages
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from .access_passwords import generate_initial_access_password
from .boarding import record_boarding
from .models import (
    AccessAccount,
    Address,
    BoardingRecord,
    Company,
    EtaSample,
    MeetingPoint,
    Notification,
    Person,
    PlanningConversation,
    PushDevice,
    Route,
    RouteEvent,
    RoutePassenger,
    RouteStop,
    Schedule,
    StopArrival,
    Vehicle,
    VehicleLocation,
)

admin.site.site_header = "Rota Certa"
admin.site.site_title = "Rota Certa · Admin"
admin.site.index_title = "Administração"

TIMESTAMPS = ("created_at", "updated_at")


def admin_link(obj, label=None):
    """Link para a página de edição de `obj` no admin (ou "-" se vazio)."""
    if obj is None:
        return "-"
    url = reverse(f"admin:{obj._meta.app_label}_{obj._meta.model_name}_change", args=[obj.pk])
    return format_html('<a href="{}">{}</a>', url, label or obj)


def changelist_link(model, label, **filters):
    url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
    query = "&".join(f"{key}={value}" for key, value in filters.items())
    return format_html('<a href="{}?{}">{}</a>', url, query, label)


class BaseAdmin(admin.ModelAdmin):
    """Padrões de todo cadastro: datas só leitura, filtro/ações de ativo-inativo, paginação."""

    readonly_fields = TIMESTAMPS
    list_per_page = 25
    actions = ("activate", "deactivate")

    def get_list_filter(self, request):
        filters = tuple(super().get_list_filter(request))
        return filters if "is_active" in filters else (*filters, "is_active")

    @admin.action(description="Ativar selecionados")
    def activate(self, request, queryset):
        updated = queryset.update(is_active=True, updated_at=timezone.now())
        self.message_user(request, f"{updated} registro(s) ativado(s).", messages.SUCCESS)

    @admin.action(description="Desativar selecionados")
    def deactivate(self, request, queryset):
        updated = queryset.update(is_active=False, updated_at=timezone.now())
        self.message_user(request, f"{updated} registro(s) desativado(s).", messages.SUCCESS)


STATUS_FIELDSET = ("Status", {"fields": ("is_active", *TIMESTAMPS)})


# ====================== CADASTROS ======================


@admin.register(Company)
class CompanyAdmin(BaseAdmin):
    list_display = ("name", "effective_email_domain", "people_count", "routes_count", "vehicles_count", "is_active")
    search_fields = ("name", "email_domain")
    fieldsets = (
        ("Empresa", {"fields": ("name", "email_domain")}),
        STATUS_FIELDSET,
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _people=Count("people", distinct=True),
            _routes=Count("routes", distinct=True),
            _vehicles=Count("vehicles", distinct=True),
        )

    @admin.display(description="domínio dos e-mails")
    def effective_email_domain(self, obj):
        # Vazio no cadastro = derivado do nome; mostra o que de fato vai nos e-mails.
        return obj.get_email_domain()

    @admin.display(description="pessoas", ordering="_people")
    def people_count(self, obj):
        return changelist_link(Person, obj._people, company__id__exact=obj.pk)

    @admin.display(description="rotas", ordering="_routes")
    def routes_count(self, obj):
        return changelist_link(Route, obj._routes, company__id__exact=obj.pk)

    @admin.display(description="veículos", ordering="_vehicles")
    def vehicles_count(self, obj):
        return changelist_link(Vehicle, obj._vehicles, company__id__exact=obj.pk)


class AccessAccountInline(admin.StackedInline):
    """A conta nasce sozinha ao salvar a pessoa (core.signals); aqui só se ajusta perfil/estado."""

    model = AccessAccount
    can_delete = False
    extra = 0
    max_num = 1
    verbose_name_plural = "conta de acesso"
    fields = ("email", "role", "must_change_password", "is_active", "last_login", "blocked_until")
    readonly_fields = ("email", "last_login", "blocked_until")

    def has_add_permission(self, request, obj=None):
        return False


class PersonAddressInline(admin.TabularInline):
    model = Address
    fk_name = "person"
    extra = 0
    fields = ("street", "number", "city", "state", "zip_code", "latitude", "longitude", "is_active")
    verbose_name_plural = "endereços da pessoa"


@admin.register(Person)
class PersonAdmin(BaseAdmin):
    list_display = ("name", "company", "type", "registration", "access_email", "access_role", "is_active")
    list_filter = ("type", "access_account__role", "company")
    search_fields = ("name", "phone", "employee_code", "license_number", "access_account__email")
    list_select_related = ("company", "access_account")
    autocomplete_fields = ("company",)
    inlines = (AccessAccountInline, PersonAddressInline)
    fieldsets = (
        ("Identificação", {"fields": ("company", "name", "phone", "type")}),
        (
            "Documentos",
            {
                "fields": ("employee_code", "license_number"),
                "description": "Funcionário usa matrícula; motorista usa CNH. "
                "O e-mail de acesso é gerado a partir dela + domínio da empresa.",
            },
        ),
        STATUS_FIELDSET,
    )

    @admin.display(description="matrícula / CNH")
    def registration(self, obj):
        return obj.employee_code or obj.license_number or "-"

    @admin.display(description="e-mail de acesso", ordering="access_account__email")
    def access_email(self, obj):
        account = getattr(obj, "access_account", None)
        return admin_link(account, account.email) if account else "-"

    @admin.display(description="perfil", ordering="access_account__role")
    def access_role(self, obj):
        account = getattr(obj, "access_account", None)
        return account.get_role_display() if account else "-"


class LockStatusFilter(admin.SimpleListFilter):
    title = "bloqueio"
    parameter_name = "blocked"

    def lookups(self, request, model_admin):
        return (("yes", "Bloqueadas agora"), ("no", "Liberadas"))

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == "yes":
            return queryset.filter(blocked_until__gt=now)
        if self.value() == "no":
            return queryset.exclude(blocked_until__gt=now)
        return queryset


@admin.register(AccessAccount)
class AccessAccountAdmin(BaseAdmin):
    """Login do app/painel. O perfil (role) define o que a pessoa acessa."""

    list_display = ("email", "person_link", "company", "role", "must_change_password", "lock_status", "last_login", "is_active")
    list_filter = ("role", "must_change_password", LockStatusFilter, "person__company")
    search_fields = ("email", "person__name", "person__employee_code", "person__license_number")
    list_select_related = ("person", "person__company")
    date_hierarchy = "last_login"
    actions = ("unlock", "reset_to_initial_password", "require_password_change", "activate", "deactivate")
    # O e-mail é derivado da pessoa (core.signals) e seria sobrescrito na próxima sincronização;
    # a senha só muda pelas ações (nunca exibida nem editada aqui).
    readonly_fields = (
        "person",
        "email",
        "last_login",
        "failed_login_attempts",
        "blocked_until",
        "password_reset_requested_at",
        "password_reset_expires_at",
        "password_reset_code_used",
        *TIMESTAMPS,
    )
    fieldsets = (
        ("Conta", {"fields": ("person", "email", "role")}),
        ("Senha", {"fields": ("must_change_password",)}),
        ("Segurança", {"fields": ("last_login", "failed_login_attempts", "blocked_until")}),
        (
            "Redefinição de senha",
            {"fields": ("password_reset_requested_at", "password_reset_expires_at", "password_reset_code_used")},
        ),
        STATUS_FIELDSET,
    )

    def has_add_permission(self, request):
        # Sem senha definida a conta seria inutilizável; ela nasce ao cadastrar a Person.
        return False

    @admin.display(description="pessoa", ordering="person__name")
    def person_link(self, obj):
        return admin_link(obj.person, obj.person.name)

    @admin.display(description="empresa", ordering="person__company__name")
    def company(self, obj):
        return obj.person.company

    @admin.display(description="bloqueio")
    def lock_status(self, obj):
        if obj.blocked_until and obj.blocked_until > timezone.now():
            local = timezone.localtime(obj.blocked_until)
            return format_html('<span class="badge text-bg-danger">até {}</span>', local.strftime("%d/%m %H:%M"))
        if obj.failed_login_attempts:
            return format_html('<span class="badge text-bg-warning">{} falha(s)</span>', obj.failed_login_attempts)
        return format_html('<span class="badge text-bg-success">{}</span>', "ok")

    @admin.action(description="Desbloquear (zera tentativas de login)")
    def unlock(self, request, queryset):
        updated = queryset.update(failed_login_attempts=0, blocked_until=None, updated_at=timezone.now())
        self.message_user(request, f"{updated} conta(s) desbloqueada(s).", messages.SUCCESS)

    @admin.action(description="Redefinir para a senha inicial e exigir troca")
    def reset_to_initial_password(self, request, queryset):
        total = 0
        for account in queryset.select_related("person"):
            account.set_password(generate_initial_access_password(account.person))
            account.must_change_password = True
            account.failed_login_attempts = 0
            account.blocked_until = None
            account.clear_password_reset()
            account.save()
            total += 1
        self.message_user(
            request,
            f"{total} senha(s) redefinida(s) para matrícula/CNH + 4 últimos dígitos do telefone. "
            "A troca será exigida no próximo login.",
            messages.SUCCESS,
        )

    @admin.action(description="Exigir troca de senha no próximo login")
    def require_password_change(self, request, queryset):
        updated = queryset.update(must_change_password=True, updated_at=timezone.now())
        self.message_user(request, f"{updated} conta(s) marcada(s) para trocar a senha.", messages.SUCCESS)


@admin.register(Address)
class AddressAdmin(BaseAdmin):
    list_display = ("__str__", "state", "zip_code", "owner", "map_link", "is_active")
    list_filter = ("state", "city", "company")
    search_fields = ("street", "number", "city", "zip_code", "company__name", "person__name")
    list_select_related = ("company", "person")
    autocomplete_fields = ("company", "person")
    fieldsets = (
        ("Pertence a", {"fields": ("company", "person"), "description": "Preencha a empresa ou a pessoa."}),
        ("Endereço", {"fields": ("street", "number", "city", "state", "zip_code")}),
        ("Localização", {"fields": ("latitude", "longitude")}),
        STATUS_FIELDSET,
    )

    @admin.display(description="pertence a")
    def owner(self, obj):
        return obj.person or obj.company or "-"

    @admin.display(description="mapa")
    def map_link(self, obj):
        return maps_link(obj.latitude, obj.longitude)


def maps_link(latitude, longitude):
    if latitude is None or longitude is None:
        return "-"
    return format_html(
        '<a href="https://www.google.com/maps?q={},{}" target="_blank" rel="noopener">'
        '<i class="fas fa-map-marked-alt"></i> ver</a>',
        latitude,
        longitude,
    )


@admin.register(Schedule)
class ScheduleAdmin(BaseAdmin):
    list_display = ("time", "weekdays_display", "company", "is_active")
    list_filter = ("company",)
    search_fields = ("company__name",)
    list_select_related = ("company",)
    autocomplete_fields = ("company",)
    ordering = ("company__name", "time")
    fieldsets = (("Horário", {"fields": ("company", "time", "weekdays")}), STATUS_FIELDSET)

    @admin.display(description="dias da semana")
    def weekdays_display(self, obj):
        return obj.weekdays_label()


# ====================== OPERAÇÃO ======================


@admin.register(Vehicle)
class VehicleAdmin(BaseAdmin):
    list_display = ("plate_number", "model", "capacity", "company", "driver", "is_active")
    list_filter = ("company",)
    search_fields = ("plate_number", "model", "driver__name", "company__name")
    list_select_related = ("company", "driver")
    autocomplete_fields = ("company", "driver")
    fieldsets = (
        ("Veículo", {"fields": ("plate_number", "model", "capacity")}),
        ("Atribuição", {"fields": ("company", "driver")}),
        STATUS_FIELDSET,
    )


@admin.register(MeetingPoint)
class MeetingPointAdmin(BaseAdmin):
    list_display = ("name", "company", "address", "is_active")
    list_filter = ("company",)
    search_fields = ("name", "company__name", "address__street", "address__city")
    list_select_related = ("company", "address")
    autocomplete_fields = ("company", "address")
    fieldsets = (("Ponto de encontro", {"fields": ("company", "name", "address")}), STATUS_FIELDSET)


class RouteStopInline(admin.TabularInline):
    model = RouteStop
    extra = 0
    fields = ("sequence", "meeting_point", "is_active")
    autocomplete_fields = ("meeting_point",)
    ordering = ("sequence",)


class RoutePassengerInline(admin.TabularInline):
    model = RoutePassenger
    extra = 0
    fields = ("person", "route_stop", "boarding_status", "is_active")
    autocomplete_fields = ("person", "route_stop")


@admin.register(Route)
class RouteAdmin(BaseAdmin):
    list_display = ("name", "company", "schedule", "service_date", "vehicle", "driver", "stops_count", "passengers_count", "events_link", "is_active")
    list_filter = ("company", "schedule", "service_date")
    search_fields = ("name", "company__name", "vehicle__plate_number", "vehicle__driver__name")
    list_select_related = ("company", "schedule", "vehicle", "vehicle__driver")
    autocomplete_fields = ("company", "vehicle")
    inlines = (RouteStopInline, RoutePassengerInline)
    fieldsets = (("Rota", {"fields": ("company", "name", "schedule", "service_date", "vehicle")}), STATUS_FIELDSET)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _stops=Count("stops", distinct=True),
            _passengers=Count("route_passengers", distinct=True),
        )

    @admin.display(description="motorista", ordering="vehicle__driver__name")
    def driver(self, obj):
        return obj.vehicle.driver if obj.vehicle and obj.vehicle.driver else "-"

    @admin.display(description="paradas", ordering="_stops")
    def stops_count(self, obj):
        return obj._stops

    @admin.display(description="passageiros", ordering="_passengers")
    def passengers_count(self, obj):
        return obj._passengers

    @admin.display(description="eventos")
    def events_link(self, obj):
        return changelist_link(RouteEvent, "ver eventos", route__id__exact=obj.pk)


@admin.register(RouteStop)
class RouteStopAdmin(BaseAdmin):
    list_display = ("route", "sequence", "meeting_point", "is_active")
    list_filter = ("route__company", "route")
    search_fields = ("route__name", "meeting_point__name")
    list_select_related = ("route", "meeting_point")
    autocomplete_fields = ("route", "meeting_point")
    ordering = ("route__name", "sequence")
    fieldsets = (("Parada", {"fields": ("route", "sequence", "meeting_point")}), STATUS_FIELDSET)


@admin.register(RoutePassenger)
class RoutePassengerAdmin(BaseAdmin):
    list_display = ("person", "route", "route_stop", "boarding_status", "is_active")
    list_filter = ("boarding_status", "route__company", "route")
    list_editable = ("boarding_status",)
    search_fields = ("person__name", "person__employee_code", "route__name")
    list_select_related = ("person", "route", "route_stop", "route_stop__route", "route_stop__meeting_point")
    autocomplete_fields = ("person", "route", "route_stop")
    fieldsets = (("Passageiro", {"fields": ("route", "person", "route_stop", "boarding_status")}), STATUS_FIELDSET)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if "boarding_status" in form.changed_data:
            record_boarding(obj)  # marcado pelo admin: sem pessoa


@admin.register(RouteEvent)
class RouteEventAdmin(BaseAdmin):
    list_display = ("timestamp", "route", "event_type", "short_description", "map_link", "is_active")
    list_filter = ("event_type", "route__company", "route")
    search_fields = ("route__name", "description")
    list_select_related = ("route",)
    autocomplete_fields = ("route",)
    date_hierarchy = "timestamp"
    ordering = ("-timestamp",)
    readonly_fields = ("timestamp", *TIMESTAMPS)
    fieldsets = (
        ("Evento", {"fields": ("route", "event_type", "description", "timestamp")}),
        ("Localização", {"fields": ("latitude", "longitude")}),
        STATUS_FIELDSET,
    )

    @admin.display(description="descrição")
    def short_description(self, obj):
        text = obj.description or ""
        return text if len(text) <= 60 else f"{text[:57]}..."

    @admin.display(description="mapa")
    def map_link(self, obj):
        return maps_link(obj.latitude, obj.longitude)


# ====================== TEMPO REAL (app mobile) ======================


class GeneratedByAppAdmin(admin.ModelAdmin):
    """Registros gerados pelo app (GPS, avisos): só consulta no admin."""

    list_per_page = 25

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(VehicleLocation)
class VehicleLocationAdmin(GeneratedByAppAdmin):
    list_display = ("route", "recorded_at", "speed", "accuracy", "map_link")
    list_select_related = ("route",)
    search_fields = ("route__name",)
    ordering = ("-recorded_at",)

    @admin.display(description="mapa")
    def map_link(self, obj):
        return maps_link(obj.latitude, obj.longitude)


@admin.register(BoardingRecord)
class BoardingRecordAdmin(GeneratedByAppAdmin):
    list_display = ("service_date", "person", "route", "status", "marked_by", "updated_at")
    list_filter = ("status", "service_date", "route__company")
    list_select_related = ("person", "route", "marked_by")
    search_fields = ("person__name", "person__employee_code", "route__name")
    date_hierarchy = "service_date"
    ordering = ("-service_date", "-updated_at")


@admin.register(EtaSample)
class EtaSampleAdmin(GeneratedByAppAdmin):
    """Previsões guardadas durante as rotas (teste em campo: manage.py relatorio_eta)."""

    list_display = ("service_date", "recorded_at", "passenger", "eta_seconds", "distance_meters", "speed")
    list_filter = ("service_date", "passenger__route")
    list_select_related = ("passenger__person", "passenger__route")
    ordering = ("-recorded_at",)


@admin.register(StopArrival)
class StopArrivalAdmin(GeneratedByAppAdmin):
    list_display = ("service_date", "arrived_at", "passenger")
    list_filter = ("service_date", "passenger__route")
    list_select_related = ("passenger__person", "passenger__route")
    ordering = ("-arrived_at",)


@admin.register(Notification)
class NotificationAdmin(GeneratedByAppAdmin):
    list_display = ("created_at", "recipient", "kind", "title", "route", "read_at")
    list_filter = ("kind", "route__company")
    list_select_related = ("recipient", "route")
    search_fields = ("recipient__name", "title", "message")
    date_hierarchy = "created_at"


@admin.register(PushDevice)
class PushDeviceAdmin(admin.ModelAdmin):
    list_display = ("person", "platform", "is_active", "last_seen_at", "created_at")
    list_filter = ("platform", "is_active")
    search_fields = ("person__name", "token")
    raw_id_fields = ("person",)
    readonly_fields = ("created_at",)


@admin.register(PlanningConversation)
class PlanningConversationAdmin(GeneratedByAppAdmin):
    list_display = ("id", "company", "created_by", "status", "input_tokens", "output_tokens", "updated_at")
    list_filter = ("status", "company")
    list_select_related = ("company", "created_by")
    search_fields = ("created_by__name",)
    date_hierarchy = "created_at"
