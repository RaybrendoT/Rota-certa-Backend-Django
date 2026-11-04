"""Geocodificação (Google Geocoding API) e consulta de CEP (ViaCEP)."""
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"
VIACEP_URL = "https://viacep.com.br/ws/{cep}/json/"
# Resultados que apontam para o imóvel (ou um trecho numerado da rua); os demais são só a rua ou o bairro.
EXACT_LOCATION_TYPES = {"ROOFTOP", "RANGE_INTERPOLATED"}


def reverse_geocode(lat, lng):
    """{"street", "number", "sublocality", "city", "state", "postal_code", "formatted"} mais próximo, ou None."""
    result = _first_result({"latlng": f"{lat},{lng}", "result_type": "street_address|route"})
    return _parse(result) if result else None


def geocode_address(street, number, neighborhood, city, state, postal_code):
    """
    Endereço digitado → {"latitude", "longitude", "exact", ...partes} do melhor resultado, ou None.
    `exact` é False quando o Google só achou a rua ou o bairro (sem o número).
    """
    address = ", ".join(part for part in (f"{street}, {number}", neighborhood, f"{city} - {state}", postal_code) if part)
    result = _first_result({"address": address, "components": "country:BR"})
    if not result:
        return None
    location = result["geometry"]["location"]
    return {
        **_parse(result),
        "latitude": location["lat"],
        "longitude": location["lng"],
        "exact": result["geometry"].get("location_type") in EXACT_LOCATION_TYPES,
    }


def lookup_cep(cep):
    """CEP (8 dígitos) → {"postal_code", "street", "neighborhood", "city", "state"}, ou None se não existir."""
    digits = only_digits(cep)
    if len(digits) != 8:
        return None
    try:
        with urllib.request.urlopen(VIACEP_URL.format(cep=digits), timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        logger.warning("Consulta de CEP falhou para %s", digits, exc_info=True)
        raise GeocodingUnavailable("Não foi possível consultar o CEP agora.")
    if data.get("erro"):
        return None
    return {
        "postal_code": f"{digits[:5]}-{digits[5:]}",
        "street": data.get("logradouro", ""),
        "neighborhood": data.get("bairro", ""),
        "city": data.get("localidade", ""),
        "state": data.get("uf", ""),
    }


class GeocodingUnavailable(Exception):
    """Serviço externo fora do ar ou sem chave; a mensagem vai para o usuário."""


def only_digits(text) -> str:
    return re.sub(r"\D", "", str(text or ""))


def _first_result(params):
    if not settings.GOOGLE_MAPS_API_KEY:
        raise GeocodingUnavailable("Localização de endereços indisponível (GOOGLE_MAPS_API_KEY).")
    query = urllib.parse.urlencode({**params, "language": "pt-BR", "key": settings.GOOGLE_MAPS_API_KEY})
    try:
        with urllib.request.urlopen(f"{GEOCODING_URL}?{query}", timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        logger.warning("Geocodificação falhou para %s", params, exc_info=True)
        raise GeocodingUnavailable("Não foi possível localizar o endereço agora; tente de novo.")
    if data.get("status") not in ("OK", "ZERO_RESULTS"):
        logger.warning("Geocoding API respondeu %s: %s", data.get("status"), data.get("error_message"))
        raise GeocodingUnavailable("Não foi possível localizar o endereço agora; tente de novo.")
    results = data.get("results") or []
    return results[0] if results else None


def _parse(result):
    components = {kind: c for c in result.get("address_components", []) for kind in c["types"]}

    def long_name(*kinds):
        return next((components[k]["long_name"] for k in kinds if k in components), "")

    return {
        "street": long_name("route"),
        "number": long_name("street_number"),
        "sublocality": long_name("sublocality_level_1", "sublocality"),
        "city": long_name("administrative_area_level_2", "locality"),
        "state": components["administrative_area_level_1"]["short_name"] if "administrative_area_level_1" in components else "",
        "postal_code": long_name("postal_code"),
        "formatted": result.get("formatted_address", ""),
    }
