import re

from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def normalize_email(email):
    if not email:
        return ""
    return str(email).strip().lower()


def derive_email_domain_from_company_name(name: str, max_length: int = 24) -> str:
    """
    Gera um domínio estilo samsung.com a partir do nome da empresa: acumula as
    palavras do nome (em minúsculas, sem acento/pontuação) enquanto couberem em
    `max_length`. Nomes curtos ("Coca-Cola") saem inteiros ("cocacola.com");
    nomes com várias palavras param de acumular antes de estourar o limite —
    nunca corta uma palavra no meio.
    """
    name = (name or "").strip()
    if not name:
        return "empresa.com"

    slug = ""
    for part in re.split(r"[\s.,_-]+", name):
        piece = re.sub(r"[^a-z0-9]", "", part.lower())
        if not piece:
            continue
        if not slug:
            slug = piece
        elif len(slug) + len(piece) <= max_length:
            slug += piece
        else:
            break

    return f"{slug or 'empresa'}.com"


def normalize_email_domain(domain: str) -> str:
    if not domain:
        return ""
    d = str(domain).strip().lower().lstrip("@")
    return d

class ActiveManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)

class BaseModel(models.Model):
    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField("criado em", default=timezone.now, editable=False)
    updated_at = models.DateTimeField("atualizado em", auto_now=True)
    is_active = models.BooleanField("ativo", default=True)

    objects = models.Manager()
    active = ActiveManager()

    class Meta:
        abstract = True

class Company(BaseModel):
    name = models.CharField("nome", max_length=255)
    email_domain = models.CharField(
        "domínio de e-mail",
        max_length=255,
        blank=True,
        help_text="Domínio dos e-mails automáticos, ex: samsung.com (sem @). "
        "Vazio = derivado do nome da empresa.",
    )

    class Meta:
        verbose_name = "empresa"
        verbose_name_plural = "empresas"

    def clean(self):
        super().clean()
        if self.email_domain:
            self.email_domain = normalize_email_domain(self.email_domain)

    def save(self, *args, **kwargs):
        if self.email_domain:
            self.email_domain = normalize_email_domain(self.email_domain)
        super().save(*args, **kwargs)

    def get_email_domain(self) -> str:
        if self.email_domain:
            return self.email_domain
        return derive_email_domain_from_company_name(self.name)

    def __str__(self):
        return self.name


class Person(BaseModel):
    class PersonType(models.TextChoices):
        DRIVER = "DRIVER", "Motorista"
        EMPLOYEE = "EMPLOYEE", "Funcionário"

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="people", verbose_name="empresa")
    name = models.CharField("nome", max_length=255)
    phone = models.CharField("telefone", max_length=30)

    type = models.CharField(
        "tipo",
        max_length=20,
        choices=PersonType.choices,
        db_index=True,
    )

    license_number = models.CharField("CNH", max_length=100, null=True, blank=True)   # só driver
    employee_code = models.CharField("matrícula", max_length=100, null=True, blank=True)    # só employee
    # Foto do perfil (painel/app): imagem pequena já recortada no cliente, guardada no banco para não
    # depender de armazenamento de arquivos. Ver core/profile_photo.py.
    photo = models.BinaryField("foto", null=True, blank=True, editable=False)
    photo_content_type = models.CharField("tipo da foto", max_length=20, blank=True, default="", editable=False)
    photo_updated_at = models.DateTimeField("foto atualizada em", null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "pessoa"
        verbose_name_plural = "pessoas"

    def __str__(self):
        return self.name

    def clean(self):
        if self.type == self.PersonType.DRIVER:
            if not self.license_number:
                raise ValidationError({"license_number": "Driver precisa de license_number."})
            if self.employee_code:
                raise ValidationError({"employee_code": "Driver não pode ter employee_code."})

        if self.type == self.PersonType.EMPLOYEE:
            if not self.employee_code:
                raise ValidationError({"employee_code": "Employee precisa de employee_code."})
            if self.license_number:
                raise ValidationError({"license_number": "Employee não pode ter license_number."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def get_auto_email_local_part(self):
        if self.type == self.PersonType.EMPLOYEE:
            raw = self.employee_code
        else:
            raw = self.license_number
        if raw is None:
            return None
        local = re.sub(r"\s+", "", str(raw).strip().lower())
        return local or None

    def get_auto_email(self):
        """
        E-mail padrão: {employee_code|license_number}@{domínio da empresa}.
        Domínio = Company.email_domain ou derivado do nome (ex.: Samsung -> samsung.com).
        """
        local = self.get_auto_email_local_part()
        if not local:
            return None
        domain = self.company.get_email_domain()
        return normalize_email(f"{local}@{domain}")


class AccessAccount(BaseModel):
    class Role(models.TextChoices):
        DRIVER = "DRIVER", "Motorista"
        EMPLOYEE = "EMPLOYEE", "Funcionário"
        MANAGER = "MANAGER", "Gestor"
        GERENTE = "GERENTE", "Gerente"

    person = models.OneToOneField(
        Person,
        on_delete=models.CASCADE,
        related_name="access_account",
        verbose_name="pessoa",
    )

    email = models.EmailField("e-mail", unique=True, db_index=True)

    role = models.CharField(
        "perfil",
        max_length=20,
        choices=Role.choices,
        default=Role.EMPLOYEE,
        db_index=True,
        help_text="Perfil de acesso. DRIVER só para pessoa DRIVER; "
        "EMPLOYEE, MANAGER e GERENTE só para pessoa EMPLOYEE.",
    )

    password_hash = models.CharField("hash da senha", max_length=255)

    must_change_password = models.BooleanField("deve trocar a senha", default=True)

    failed_login_attempts = models.IntegerField("tentativas de login com falha", default=0)

    last_login = models.DateTimeField("último login", null=True, blank=True)

    blocked_until = models.DateTimeField("bloqueada até", null=True, blank=True)

    # Redefinição de senha por código (OTP) — sem envio por e-mail
    password_reset_code_hash = models.CharField("hash do código de redefinição", max_length=255, blank=True)
    password_reset_expires_at = models.DateTimeField("código expira em", null=True, blank=True)
    password_reset_requested_at = models.DateTimeField("código solicitado em", null=True, blank=True)
    password_reset_code_used = models.BooleanField("código já usado", default=True)

    class Meta:
        verbose_name = "conta de acesso"
        verbose_name_plural = "contas de acesso"

    @staticmethod
    def default_role_for(person):
        if person.type == Person.PersonType.DRIVER:
            return AccessAccount.Role.DRIVER
        return AccessAccount.Role.EMPLOYEE

    @property
    def is_manager(self) -> bool:
        return self.role == self.Role.MANAGER

    @property
    def is_gerente(self) -> bool:
        return self.role == self.Role.GERENTE

    def validate_role(self):
        """DRIVER ⇔ pessoa DRIVER; EMPLOYEE/MANAGER/GERENTE exigem pessoa EMPLOYEE."""
        if not self.person_id:
            return
        person_is_driver = self.person.type == Person.PersonType.DRIVER
        if (self.role == self.Role.DRIVER) != person_is_driver:
            if person_is_driver:
                message = "Pessoa do tipo DRIVER só pode ter o perfil DRIVER."
            else:
                message = "O perfil DRIVER exige uma pessoa do tipo DRIVER."
            raise ValidationError({"role": message})

    def clean(self):
        super().clean()
        if self.email:
            self.email = normalize_email(self.email)
        self.validate_role()

    def save(self, *args, **kwargs):
        if self.email:
            self.email = normalize_email(self.email)
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "role" in update_fields:
            self.validate_role()
        super().save(*args, **kwargs)

    def set_password(self, raw_password):
        self.password_hash = make_password(raw_password)

    def check_password(self, raw_password):
        return check_password(raw_password, self.password_hash)

    def set_password_reset_code(self, raw_code: str, expires_at):
        self.password_reset_code_hash = make_password(raw_code)
        self.password_reset_expires_at = expires_at
        self.password_reset_requested_at = timezone.now()
        self.password_reset_code_used = False

    def check_password_reset_code(self, raw_code: str) -> bool:
        if (
            self.password_reset_code_used
            or not self.password_reset_code_hash
            or not self.password_reset_expires_at
        ):
            return False
        if timezone.now() > self.password_reset_expires_at:
            return False
        return check_password(raw_code, self.password_reset_code_hash)

    def clear_password_reset(self):
        self.password_reset_code_hash = ""
        self.password_reset_expires_at = None
        self.password_reset_requested_at = None
        self.password_reset_code_used = True

    def __str__(self):
        return self.email


class Route(BaseModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='routes', verbose_name="empresa")
    name = models.CharField("nome", max_length=255)
    vehicle = models.ForeignKey(
        "Vehicle",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="routes",
        verbose_name="veículo",
        help_text="Veículo (e motorista) atribuído à rota — usado pelo app do motorista.",
    )
    schedule = models.ForeignKey(
        "Schedule",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="routes",
        verbose_name="turno",
        help_text="Horário de chegada na empresa que esta rota atende. "
        "Um mesmo motorista pode ter rotas em turnos diferentes.",
    )
    service_date = models.DateField(
        "data (rota avulsa)",
        null=True,
        blank=True,
        db_index=True,
        help_text="Preenchida em rotas extras de um dia só (ex.: planejadas pelo assistente). "
        "Vazia = rota fixa, que se repete nos dias do turno.",
    )
    # Trajeto pelas ruas (Google Routes API), calculado uma vez e refeito só quando as paradas
    # mudam. Ver core/road_path.py.
    road_path = models.JSONField("trajeto pelas ruas", null=True, blank=True)

    class Meta:
        verbose_name = "rota"
        verbose_name_plural = "rotas"

    def __str__(self):
        return self.name


class RouteStop(BaseModel):
    """Parada ordenada na rota (timeline / RouteStopCard)."""

    route = models.ForeignKey(
        Route,
        on_delete=models.CASCADE,
        related_name="stops",
        verbose_name="rota",
    )
    meeting_point = models.ForeignKey(
        "MeetingPoint",
        on_delete=models.CASCADE,
        related_name="route_stops",
        verbose_name="ponto de encontro",
    )
    sequence = models.PositiveSmallIntegerField(
        "ordem",
        help_text="0 = primeiro ponto; ordem crescente até o destino.",
    )

    class Meta:
        verbose_name = "parada"
        verbose_name_plural = "paradas"
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["route", "sequence"],
                name="core_routestop_unique_route_sequence",
            ),
        ]

    def clean(self):
        super().clean()
        if self.meeting_point_id and self.route_id:
            if self.meeting_point.company_id != self.route.company_id:
                raise ValidationError(
                    {"meeting_point": "O ponto de encontro deve ser da mesma empresa da rota."}
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.route.name} #{self.sequence} {self.meeting_point.name}"


WEEKDAY_LABELS = {
    "MON": "Seg", "TUE": "Ter", "WED": "Qua", "THU": "Qui", "FRI": "Sex", "SAT": "Sáb", "SUN": "Dom",
}


class Schedule(BaseModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='schedules', verbose_name="empresa")
    weekdays = models.JSONField("dias da semana", help_text='Lista de dias, ex.: ["MON", "TUE", "WED"].')
    time = models.TimeField("horário")

    class Meta:
        verbose_name = "horário"
        verbose_name_plural = "horários"

    def weekdays_label(self) -> str:
        days = self.weekdays if isinstance(self.weekdays, list) else [self.weekdays]
        return ", ".join(WEEKDAY_LABELS.get(str(day).upper(), str(day)) for day in days)

    def __str__(self):
        return f"{self.time:%H:%M} ({self.weekdays_label()})" if self.time else self.weekdays_label()


class Address(BaseModel):
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name='addresses',
        null=True,
        blank=True,
        verbose_name="empresa",
    )

    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name='addresses',
        null=True,
        blank=True,
        verbose_name="pessoa",
    )

    street = models.CharField("logradouro", max_length=100)
    number = models.CharField("número", max_length=20)
    city = models.CharField("cidade", max_length=50)
    state = models.CharField("estado", max_length=50)
    zip_code = models.CharField("CEP", max_length=10)
    latitude = models.DecimalField("latitude", max_digits=9, decimal_places=6)
    longitude = models.DecimalField("longitude", max_digits=9, decimal_places=6)

    class Source(models.TextChoices):
        PANEL = "panel", "Cadastro no painel"
        GPS = "gps", "Localização do colaborador (GPS)"
        TYPED = "typed", "Digitado pelo colaborador"

    neighborhood = models.CharField("bairro", max_length=100, blank=True, default="")
    complement = models.CharField("complemento", max_length=100, blank=True, default="")
    # Endereço residencial informado pelo próprio colaborador no app: origem, precisão e quem gravou.
    # Ao trocar, o anterior fica inativo (histórico) em vez de ser apagado.
    source = models.CharField("origem", max_length=10, choices=Source.choices, default=Source.PANEL)
    accuracy_m = models.FloatField("precisão do GPS (m)", null=True, blank=True)
    precision = models.CharField("precisão da localização", max_length=20, blank=True, default="")
    created_by = models.ForeignKey(
        "Person", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="informado por"
    )

    class Review(models.TextChoices):
        # Endereço enviado pelo app fica inativo até o gestor aprovar (o anterior continua valendo).
        PENDING = "pending", "Aguardando aprovação"
        APPROVED = "approved", "Aprovado"
        REJECTED = "rejected", "Recusado"

    review_status = models.CharField("aprovação", max_length=10, choices=Review.choices, blank=True, default="")
    reviewed_by = models.ForeignKey(
        "Person", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="revisado por"
    )
    reviewed_at = models.DateTimeField("revisado em", null=True, blank=True)
    review_note = models.CharField("observação da revisão", max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "endereço"
        verbose_name_plural = "endereços"

    def __str__(self):
        return f"{self.street}, {self.number} - {self.city}"


class MeetingPoint(BaseModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='meeting_points', verbose_name="empresa")
    name = models.CharField("nome", max_length=255)
    address = models.ForeignKey(Address, on_delete=models.CASCADE, related_name='meeting_points', verbose_name="endereço")
    # Embarque na porta de casa: criado quando nenhuma parada da rota fica perto do colaborador
    # (core.home_pickup). Não entra nas buscas de "ponto mais próximo" nem no planejamento.
    home_of = models.OneToOneField(
        "Person", on_delete=models.CASCADE, null=True, blank=True,
        related_name="home_pickup_point", verbose_name="casa do colaborador",
    )

    class Meta:
        verbose_name = "ponto de encontro"
        verbose_name_plural = "pontos de encontro"

    def __str__(self):
        return self.name



class RoutePassenger(BaseModel):
    class BoardingStatus(models.TextChoices):
        PENDING = "pending", "Pendente"
        CONFIRMED = "confirmed", "Confirmado"
        ABSENT = "absent", "Ausente"

    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name='route_passengers',
        limit_choices_to={'type': 'EMPLOYEE'},
        db_index= True,
        verbose_name="passageiro",
    )
    route = models.ForeignKey(
        Route,
        on_delete=models.CASCADE,
        related_name='route_passengers',
        db_index= True,
        verbose_name="rota",
    )
    route_stop = models.ForeignKey(
        "RouteStop",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="passengers",
        verbose_name="parada",
        help_text="Parada de embarque/desembarque; opcional para compatibilidade.",
    )
    boarding_status = models.CharField(
        "status de embarque",
        max_length=20,
        choices=BoardingStatus.choices,
        default=BoardingStatus.PENDING,
        db_index=True,
    )

    class Meta:
        verbose_name = "passageiro da rota"
        verbose_name_plural = "passageiros das rotas"
        unique_together = ('person', 'route')
        indexes = [
            models.Index(fields=["person", "route"]),
        ]

    def __str__(self):
        return f"{self.person.name} · {self.route.name}"

    def clean(self):
        super().clean()
        if self.route_stop_id and self.route_id:
            if self.route_stop.route_id != self.route_id:
                raise ValidationError(
                    {"route_stop": "A parada deve pertencer à mesma rota."}
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class BoardingRecord(models.Model):
    """
    Embarque do passageiro num dia: o status com que ele terminou aquela data.

    RoutePassenger.boarding_status guarda só o status atual, sem data; este registro
    (um por passageiro da rota por dia) é a base do resumo do dia e do histórico.
    """

    route_passenger = models.ForeignKey(
        RoutePassenger, on_delete=models.CASCADE, related_name="boarding_records", verbose_name="passageiro da rota",
    )
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="boarding_records", verbose_name="rota")
    person = models.ForeignKey(
        "Person", on_delete=models.CASCADE, related_name="boarding_records", verbose_name="passageiro",
    )
    service_date = models.DateField("data", db_index=True)
    status = models.CharField("status de embarque", max_length=20, choices=RoutePassenger.BoardingStatus.choices)
    marked_by = models.ForeignKey(
        "Person", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="marcado por", help_text="Motorista ou o próprio colaborador; vazio = admin ou importação.",
    )
    created_at = models.DateTimeField("criado em", default=timezone.now, editable=False)
    updated_at = models.DateTimeField("atualizado em", default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "embarque do dia"
        verbose_name_plural = "embarques do dia"
        constraints = [
            models.UniqueConstraint(fields=["route_passenger", "service_date"], name="uniq_boarding_per_day"),
        ]
        indexes = [models.Index(fields=["route", "service_date"])]

    def __str__(self):
        return f"{self.person_id} · {self.route_id} · {self.service_date}: {self.status}"


class RouteEvent(BaseModel):
    EVENT_TYPE_CHOICES = [
        ('departure', 'Partida'),
        ('arrival', 'Chegada'),
        ('stop', 'Parada'),
        ('delay', 'Atraso'),
        ('incident', 'Incidente'),
    ]

    route = models.ForeignKey(
        Route,
        on_delete=models.CASCADE,
        related_name='events',
        verbose_name="rota",
    )

    # Ocorrências são os eventos de atraso e incidente; partida, chegada e parada são registros
    # da viagem. Ver core/occurrences.py para o fluxo (origem, status e detecção automática).
    OCCURRENCE_TYPES = ("delay", "incident")

    class Source(models.TextChoices):
        DRIVER = "driver", "Motorista"
        SYSTEM = "system", "Sistema"
        MANAGER = "manager", "Gestor"

    class Status(models.TextChoices):
        OPEN = "open", "Aberta"
        IN_PROGRESS = "in_progress", "Em andamento"
        RESOLVED = "resolved", "Resolvida"

    class Category(models.TextChoices):
        TRAFFIC = "traffic", "Trânsito"
        MECHANICAL = "mechanical", "Problema mecânico"
        ACCIDENT = "accident", "Acidente"
        WEATHER = "weather", "Chuva / alagamento"
        PASSENGER = "passenger", "Passageiro"
        ROAD = "road", "Via bloqueada / desvio"
        SCHEDULE = "schedule", "Horário (detectado pelo sistema)"
        OTHER = "other", "Outro"

    event_type = models.CharField("tipo", max_length=20, choices=EVENT_TYPE_CHOICES)
    description = models.TextField("descrição", blank=True)
    # Opcionais: ocorrências do sistema e do gestor não têm posição do ônibus.
    latitude = models.DecimalField("latitude", max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField("longitude", max_digits=9, decimal_places=6, null=True, blank=True)
    timestamp = models.DateTimeField("data/hora", auto_now_add=True)

    source = models.CharField("origem", max_length=10, choices=Source.choices, default=Source.DRIVER, db_index=True)
    category = models.CharField("categoria", max_length=20, choices=Category.choices, blank=True)
    status = models.CharField("status", max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True)
    delay_minutes = models.PositiveSmallIntegerField("atraso estimado (min)", null=True, blank=True)
    # Dia da operação (fuso local) a que a ocorrência se refere: filtros e detecção sem duplicar.
    service_date = models.DateField("dia", null=True, blank=True, db_index=True)
    # Regra que gerou a ocorrência do sistema (not_started, overdue, late_arrival); vazio nas demais.
    auto_code = models.CharField("regra automática", max_length=20, blank=True)
    reported_by = models.ForeignKey(
        "Person", on_delete=models.SET_NULL, null=True, blank=True, related_name="reported_events",
        verbose_name="registrada por",
    )
    resolved_at = models.DateTimeField("resolvida em", null=True, blank=True)
    resolved_by = models.ForeignKey(
        "Person", on_delete=models.SET_NULL, null=True, blank=True, related_name="resolved_events",
        verbose_name="resolvida por",
    )
    resolution = models.TextField("como foi resolvida", blank=True)

    class Meta:
        verbose_name = "evento de rota"
        verbose_name_plural = "eventos de rota"
        indexes = [models.Index(fields=["route", "service_date", "event_type"])]

    def __str__(self):
        return f"{self.route.name} - {self.event_type}"

    @property
    def is_occurrence(self):
        return self.event_type in self.OCCURRENCE_TYPES

    def save(self, *args, **kwargs):
        if self.service_date is None:
            from core.dates import local_today

            self.service_date = local_today()
        # Partida, chegada e parada não têm o que resolver.
        if not self.is_occurrence and self.status != self.Status.RESOLVED:
            self.status = self.Status.RESOLVED
        super().save(*args, **kwargs)

class Vehicle(BaseModel):
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name='vehicles',
        null=True,
        blank=True,
        verbose_name="empresa",
    )

    driver = models.ForeignKey(
        Person,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='vehicles',
        limit_choices_to={'type': 'DRIVER'},
        verbose_name="motorista",
    )

    plate_number = models.CharField("placa", max_length=20, unique=True)
    model = models.CharField("modelo", max_length=100)
    capacity = models.PositiveIntegerField("capacidade")

    class Meta:
        verbose_name = "veículo"
        verbose_name_plural = "veículos"

    def __str__(self):
        return self.plate_number

class VehicleLocation(models.Model):
    """Última posição GPS conhecida do veículo em uma rota (uma linha por rota)."""

    route = models.OneToOneField(
        Route,
        on_delete=models.CASCADE,
        related_name="current_location",
        verbose_name="rota",
    )
    latitude = models.DecimalField("latitude", max_digits=9, decimal_places=6)
    longitude = models.DecimalField("longitude", max_digits=9, decimal_places=6)
    heading = models.FloatField("direção (graus)", null=True, blank=True)
    speed = models.FloatField("velocidade (m/s)", null=True, blank=True)
    accuracy = models.FloatField("precisão (m)", null=True, blank=True)
    recorded_at = models.DateTimeField("registrada em", default=timezone.now)
    progress_meters = models.FloatField(
        "progresso no trajeto (m)", null=True, blank=True,
        help_text="Distância já percorrida no trajeto da rota (core/eta.py).",
    )

    class Meta:
        verbose_name = "localização do veículo"
        verbose_name_plural = "localizações dos veículos"

    def __str__(self):
        return f"{self.route.name} @ {self.latitude},{self.longitude}"


class EtaSample(models.Model):
    """
    Previsão de chegada registrada durante a rota, para comparar com a chegada real
    (teste em campo: comando relatorio_eta).
    """

    passenger = models.ForeignKey(
        "RoutePassenger", on_delete=models.CASCADE, related_name="eta_samples", verbose_name="passageiro"
    )
    service_date = models.DateField("dia da viagem", db_index=True)
    recorded_at = models.DateTimeField("registrada em", default=timezone.now)
    eta_seconds = models.PositiveIntegerField("previsão (s)")
    distance_meters = models.PositiveIntegerField("distância restante (m)")
    speed = models.FloatField("velocidade do GPS (m/s)", null=True, blank=True)

    class Meta:
        verbose_name = "previsão de chegada"
        verbose_name_plural = "previsões de chegada"
        ordering = ["recorded_at"]


class StopArrival(models.Model):
    """Momento em que o ônibus chegou ao ponto do passageiro (uma vez por dia)."""

    passenger = models.ForeignKey(
        "RoutePassenger", on_delete=models.CASCADE, related_name="stop_arrivals", verbose_name="passageiro"
    )
    service_date = models.DateField("dia da viagem")
    arrived_at = models.DateTimeField("chegou em")

    class Meta:
        verbose_name = "chegada ao ponto"
        verbose_name_plural = "chegadas aos pontos"
        constraints = [models.UniqueConstraint(fields=["passenger", "service_date"], name="unique_stop_arrival_per_day")]


class Notification(models.Model):
    """Aviso para uma pessoa; também é entregue em tempo real pelo WebSocket."""

    class Kind(models.TextChoices):
        ROUTE_DEPARTURE = "route_departure", "Rota iniciada"
        ROUTE_ARRIVAL = "route_arrival", "Rota finalizada"
        ROUTE_DELAY = "route_delay", "Atraso"
        ROUTE_INCIDENT = "route_incident", "Incidente"
        BUS_APPROACHING = "bus_approaching", "Ônibus chegando"
        BUS_AT_STOP = "bus_at_stop", "Ônibus no ponto"
        BOARDING_CONFIRMED = "boarding_confirmed", "Embarque confirmado"
        BOARDING_ABSENT = "boarding_absent", "Ausência informada"
        ROUTE_ASSIGNED = "route_assigned", "Nova rota"
        ADDRESS_CHANGED = "address_changed", "Endereço para aprovar"
        ADDRESS_APPROVED = "address_approved", "Endereço aprovado"
        ADDRESS_REJECTED = "address_rejected", "Endereço recusado"
        PICKUP_CHANGED = "pickup_changed", "Ponto de embarque alterado"
        ROUTE_UPDATED = "route_updated", "Rota atualizada"

    recipient = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name="destinatário",
    )
    route = models.ForeignKey(
        Route,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        verbose_name="rota",
    )
    kind = models.CharField("tipo", max_length=30, choices=Kind.choices)
    title = models.CharField("título", max_length=120)
    message = models.TextField("mensagem", blank=True)
    data = models.JSONField("dados extras", default=dict, blank=True)
    created_at = models.DateTimeField("criada em", default=timezone.now, db_index=True)
    read_at = models.DateTimeField("lida em", null=True, blank=True)

    class Meta:
        verbose_name = "notificação"
        verbose_name_plural = "notificações"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["recipient", "read_at"])]

    def __str__(self):
        return f"{self.recipient.name}: {self.title}"


class PlanningConversation(models.Model):
    """
    Conversa do gestor com o assistente de planejamento de rotas avulsas.

    `messages` guarda o histórico no formato da API do Claude (inclusive chamadas e resultados
    de ferramentas) para retomar a conversa; `draft` é o plano em rascunho, que só vira rotas
    quando o gestor aprova (a IA nunca grava rotas sozinha).
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Em planejamento"
        APPROVED = "approved", "Aprovado"

    created_by = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="planning_conversations",
        verbose_name="gestor",
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="planning_conversations",
        verbose_name="empresa",
    )
    status = models.CharField("situação", max_length=20, choices=Status.choices, default=Status.DRAFT)
    messages = models.JSONField("histórico", default=list, blank=True)
    draft = models.JSONField("plano em rascunho", null=True, blank=True)
    routes = models.ManyToManyField(
        Route, blank=True, related_name="planning_conversations", verbose_name="rotas criadas"
    )
    input_tokens = models.PositiveIntegerField("tokens de entrada", default=0)
    output_tokens = models.PositiveIntegerField("tokens de saída", default=0)
    created_at = models.DateTimeField("criada em", auto_now_add=True)
    updated_at = models.DateTimeField("atualizada em", auto_now=True)
    approved_at = models.DateTimeField("aprovada em", null=True, blank=True)

    class Meta:
        verbose_name = "planejamento com assistente"
        verbose_name_plural = "planejamentos com assistente"
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return f"Planejamento #{self.pk} ({self.get_status_display()})"


class PushDevice(models.Model):
    """
    Aparelho que recebe notificações push (token do Firebase Cloud Messaging).
    O app registra o token depois do login e quando o Firebase o renova, e o remove no logout.
    Tokens que o FCM diz que não valem mais são desativados no primeiro envio que falhar.
    """

    class Platform(models.TextChoices):
        ANDROID = "android", "Android"
        IOS = "ios", "iOS"
        WEB = "web", "Web"

    person = models.ForeignKey(
        Person, on_delete=models.CASCADE, related_name="push_devices", verbose_name="pessoa",
    )
    token = models.CharField("token FCM", max_length=512, unique=True)
    platform = models.CharField("plataforma", max_length=20, choices=Platform.choices, default=Platform.ANDROID)
    is_active = models.BooleanField("ativo", default=True, db_index=True)
    created_at = models.DateTimeField("criado em", default=timezone.now, editable=False)
    last_seen_at = models.DateTimeField("último registro", default=timezone.now)
    deactivated_reason = models.CharField("motivo da desativação", max_length=255, blank=True)

    class Meta:
        verbose_name = "aparelho (push)"
        verbose_name_plural = "aparelhos (push)"

    def __str__(self):
        return f"{self.person.name} — {self.get_platform_display()} ({'ativo' if self.is_active else 'inativo'})"
