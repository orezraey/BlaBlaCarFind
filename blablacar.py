# -*- coding: utf-8 -*-
"""Cliente HTTP para busca de viagens no BlaBlaCar Brasil.

Usa a mesma API interna (edge) que o site consome. Nao precisa de browser:
o unico requisito e um cliente HTTP capaz de imitar o fingerprint TLS do Chrome
(curl_cffi), porque o dominio fica atras do DataDome.

Fluxo:
  1. GET numa pagina do site -> extrai o applicationToken (client_credentials
     anonimo que o servidor SSR injeta no HTML).
  2. GET https://edge.blablacar.com.br/... com esse Bearer -> JSON limpo.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import quote

from curl_cffi import requests

APP = "https://www.blablacar.com.br"
EDGE = "https://edge.blablacar.com.br"

# Pagina qualquer do site serve para colher o token; /search e a mais estavel.
_TOKEN_PAGE = (
    APP + "/search?fn=S%C3%A3o%20Paulo&fc=-23.5505200%2C-46.6333090"
    "&tn=Rio%20de%20Janeiro&tc=-22.9068470%2C-43.1728510&db=2030-01-01&seats=1"
)

_TOKEN_TTL = 20 * 60  # o token nao declara expiracao; renovamos por seguranca


class BlaBlaCarError(RuntimeError):
    pass


def _strip_js_undefined(text: str) -> str:
    """Converte `undefined` (literal JS) em `null`, ignorando o que esta em string.

    Os blobs `window["__INFRASTRUCTURE__*"]` sao objetos JS, nao JSON valido.
    """
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                out.append(text[i : i + 2])
                i += 2
                continue
            if ch == '"':
                in_str = False
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "u" and text.startswith("undefined", i):
            nxt = text[i + 9] if i + 9 < n else ""
            if not (nxt.isalnum() or nxt in "_$"):
                out.append("null")
                i += 9
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _window_object(html: str, name: str) -> Any:
    marker = re.search(r"window" + re.escape('["' + name + '"]') + r"\s*=\s*", html)
    if not marker:
        raise BlaBlaCarError(f"window[{name!r}] nao encontrado no HTML")
    obj, _ = json.JSONDecoder().raw_decode(_strip_js_undefined(html[marker.end() :]))
    return obj


@dataclass
class Place:
    place_id: str
    name: str
    latitude: float
    longitude: float

    @property
    def coord(self) -> str:
        return f"{self.latitude},{self.longitude}"


@dataclass
class Trip:
    """Viagem normalizada. `raw` guarda o payload completo da API."""

    trip_id: str
    source: str  # CARPOOLING | PRO_PARTNER (onibus) | ...
    supply: str  # CARPOOL | BUS | TRAIN
    price: str
    price_value: float | None
    departure_time: str
    departure_place: str
    arrival_time: str
    arrival_place: str
    duration: str
    provider: str
    seat_class: str = ""  # "Convencional", "Leito", ... (so onibus)
    original_price: str = ""  # preco cheio, quando ha promocao
    discount: str = ""  # ex "-15%"
    amenities: list[str] = field(default_factory=list)
    overnight: bool = False
    has_connection: bool = False
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def multimodal_id(self) -> dict:
        return self.raw.get("multimodal_id") or {}

    @property
    def natural_key(self) -> str:
        """Identidade estavel da viagem, para deduplicar entre execucoes.

        NAO use `trip_id`: ele e um blob cifrado que muda a cada sessao (token +
        visitor-id novos geram ids diferentes para a mesma carona). Verificado.
        O preco fica de fora de proposito -- promocao nao faz virar viagem nova.
        """
        parts = [
            self.departure_time,
            self.arrival_time,
            self.departure_place,
            self.arrival_place,
            self.provider,
            self.duration,
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]

    @property
    def url(self) -> str:
        mm = self.multimodal_id
        parts = [f"source={mm.get('source', '')}", f"id={quote(str(mm.get('id', '')), safe='')}"]
        if mm.get("pro_partner_id"):
            parts.append(f"pro_partner_id={mm['pro_partner_id']}")
        parts.append("requested_seats=1")
        return f"{APP}/trip?" + "&".join(parts)


# Ordem do melhor para o pior; usado para ranquear/filtrar motorista.
CANCELLATION_ORDER = {"NEVER": 0, "RARELY": 1, "SOMETIMES": 2, "OFTEN": 3, "ALWAYS": 4}


@dataclass
class Driver:
    """Sinais de confianca do motorista. So existem em /ride/v3, nao na busca."""

    name: str = ""
    profile_id: str = ""
    rating: float | None = None
    rating_label: str = ""
    cancellation_code: str = ""  # NEVER | RARELY | SOMETIMES | ... | "" se sem historico
    cancellation_label: str = ""
    is_super_driver: bool = False
    is_verified: bool = False
    message: str = ""
    vehicle: str = ""
    avatar: str = ""

    @property
    def cancellation_rank(self) -> int:
        """Menor e melhor. Sem historico cai junto de RARELY, para nao premiar nem punir."""
        return CANCELLATION_ORDER.get(self.cancellation_code, 1)


@dataclass
class RideDetails:
    trip_id: str
    driver: Driver
    approval_mode: str = ""  # MANUAL exige o motorista aprovar; AUTOMATIC nao
    co2: str = ""
    raw: dict = field(repr=False, default_factory=dict)


class BlaBlaCar:
    def __init__(self, locale: str = "pt-BR", currency: str = "BRL") -> None:
        self.locale = locale
        self.currency = currency
        # impersonate="chrome" e o que faz o DataDome liberar; sem isso -> 403.
        self._s = requests.Session(impersonate="chrome")
        self._visitor_id = str(uuid.uuid4())
        self._token: str | None = None
        self._token_at = 0.0

    # ---------------------------------------------------------------- token

    @property
    def token(self) -> str:
        if self._token is None or time.time() - self._token_at > _TOKEN_TTL:
            self._refresh_token()
        assert self._token is not None
        return self._token

    def _refresh_token(self) -> None:
        r = self._s.get(_TOKEN_PAGE, timeout=45)
        if r.status_code != 200:
            raise BlaBlaCarError(f"pagina de token retornou HTTP {r.status_code}")
        auth = _window_object(r.text, "__INFRASTRUCTURE__authentication")
        try:
            self._token = auth["authentication"]["applicationToken"]["token"]["accessToken"]
        except (KeyError, TypeError) as exc:
            raise BlaBlaCarError(f"applicationToken ausente: {exc}") from exc
        self._token_at = time.time()

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.token}",
            "accept": "application/json",
            "accept-language": self.locale,
            "x-locale": self.locale,
            "x-currency": self.currency,
            "x-client": "SPA|1.0.0",
            "x-visitor-id": self._visitor_id,
            "x-correlation-id": str(uuid.uuid4()),
            "origin": APP,
            "referer": APP + "/",
        }

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        for attempt in (1, 2):
            r = self._s.get(EDGE + path, params=params, headers=self._headers(), timeout=45)
            if r.status_code == 401 and attempt == 1:
                self._token = None  # token expirou: renova e tenta de novo
                continue
            if r.status_code != 200:
                raise BlaBlaCarError(f"{path} -> HTTP {r.status_code}: {r.text[:200]}")
            return r.json()
        raise BlaBlaCarError(f"{path}: falha apos renovar token")

    # -------------------------------------------------------------- lugares

    def geocode(self, address: str) -> Place:
        d = self._get("/geocode/single", {"address": address, "locale": self.locale})
        p = d["place"]
        return Place(d["place_id"], p.get("name") or address, p["latitude"], p["longitude"])

    def suggestions(self, query: str) -> list[dict]:
        return self._get("/location/suggestions", {"query": query})

    # --------------------------------------------------------------- busca

    def search_raw(
        self,
        origin: str | Place,
        destination: str | Place,
        date: str,
        seats: int = 1,
        supply: str = "ALL",
        filters: dict[str, str] | None = None,
        max_pages: int = 20,
    ) -> Iterator[dict]:
        """Itera as paginas cruas de `/trip/search/v9`, seguindo o cursor."""
        src = origin if isinstance(origin, Place) else self.geocode(origin)
        dst = destination if isinstance(destination, Place) else self.geocode(destination)

        params: dict[str, Any] = {
            "departure_date": date,
            "requested_seats": str(seats),
            "supply": supply,
        }
        # Preferimos place_id: e a resolucao de lugar do proprio BlaBlaCar, sem
        # ambiguidade de "qual Sao Paulo". Em teste controlado devolve a mesma
        # quantidade que a coordenada, que fica de fallback.
        # A API recusa `from_address` junto de place_id ("should not be provided").
        if src.place_id and dst.place_id:
            params["from_place_id"] = src.place_id
            params["to_place_id"] = dst.place_id
        else:
            params["f[fc]"] = src.coord
            params["f[tc]"] = dst.coord
            params["from_address"] = src.name
            params["to_address"] = dst.name
        for key, value in (filters or {}).items():
            params[f"f[{key}]"] = value

        cursor: str | None = None
        search_uuid: str | None = None
        for _ in range(max_pages):
            page_params = dict(params)
            if cursor:
                page_params["from_cursor"] = cursor
            if search_uuid:
                page_params["search_uuid"] = search_uuid

            page = self._get("/trip/search/v9", page_params)
            yield page

            search_uuid = search_uuid or page.get("search_uuid")
            results = page.get("results_content", {}).get("results", {})
            nxt = (results.get("pagination") or {}).get("next_cursor")
            if not nxt or nxt == cursor:
                return
            cursor = nxt

    def ride_details(self, trip: Trip | dict, seats: int = 1) -> RideDetails:
        """Detalhe da carona -- unica fonte de rating e taxa de cancelamento.

        Custa 1 request por carona, entao chame so para viagens novas.
        """
        mm = trip.multimodal_id if isinstance(trip, Trip) else trip
        params: dict[str, Any] = {
            "source": mm.get("source", "CARPOOLING"),
            "id": mm.get("id"),
            "requested_seats": str(seats),
        }
        if mm.get("pro_partner_id"):
            params["partner_id"] = mm["pro_partner_id"]
        return _parse_ride(self._get("/ride/v3", params))

    def search(self, *args: Any, **kwargs: Any) -> list[Trip]:
        """Busca completa (todas as paginas), normalizada e deduplicada."""
        trips: list[Trip] = []
        seen: set[str] = set()
        for page in self.search_raw(*args, **kwargs):
            items = page.get("results_content", {}).get("results", {}).get("search_items", [])
            for item in items:
                trip = _normalize(item)
                if trip and trip.trip_id not in seen:
                    seen.add(trip.trip_id)
                    trips.append(trip)
        return trips


def _dig(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _join_tokens(tokens: Any) -> str:
    """Junta `tokenized_price` ([{token_type, token}, ...]) num texto."""
    if not isinstance(tokens, list):
        return ""
    return "".join(str(t.get("token", "")) for t in tokens).replace("\xa0", " ").strip()


def _to_float(price: str) -> float | None:
    digits = re.sub(r"[^\d,.]", "", price or "")
    if not digits:
        return None
    # formato pt-BR: 1.234,56 -> 1234.56
    try:
        return float(digits.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _profile_name(profile: dict) -> str | None:
    """O perfil vem como {"driver": {...}} (carona) ou {"carrier": {...}} (onibus)."""
    for kind in ("driver", "carrier", "passenger"):
        name = _dig(profile, kind, "name")
        if name:
            return name
    return profile.get("name")


def _normalize(item: dict) -> Trip | None:
    trip = item.get("trip")
    if not isinstance(trip, dict):
        return None

    mm = trip.get("multimodal_id") or _dig(
        trip, "action", "open_ride_details", "parameters", "multimodal_id"
    ) or {}
    trip_id = mm.get("id")
    if not trip_id:
        return None

    details = trip.get("travel_details") or {}
    itin = details.get("itinerary") or {}
    dep, arr = itin.get("departure") or {}, itin.get("arrival") or {}

    price_obj = trip.get("price") or {}
    price = _join_tokens(price_obj.get("tokenized_price"))
    discount_obj = price_obj.get("discount") or {}

    provider = details.get("provider") or {}
    names = [n for n in (_profile_name(p) for p in provider.get("profiles") or []) if n]
    provider_name = ", ".join(names) or (provider.get("extra_providers_label") or "")

    return Trip(
        trip_id=trip_id,
        source=mm.get("source") or "",
        supply=_dig(provider, "supply", "type") or "",
        price=price,
        price_value=_to_float(price),
        departure_time=dep.get("time") or "",
        departure_place=dep.get("primary_location") or "",
        arrival_time=arr.get("time") or "",
        arrival_place=arr.get("primary_location") or "",
        duration=_dig(itin, "total_duration", "label") or "",
        provider=provider_name,
        seat_class=price_obj.get("prefix") or "",
        original_price=_join_tokens(discount_obj.get("tokenized_original_price")),
        discount=discount_obj.get("value") or "",
        amenities=[a.get("label") for a in details.get("amenities") or [] if a.get("label")],
        overnight=bool(itin.get("is_overnight")),
        has_connection=bool(itin.get("has_connection")),
        raw=trip,
    )


def _parse_ride(payload: dict) -> RideDetails:
    driver = Driver()

    # Bloco de tracking: rating, superdriver e verificacao ja vem prontos aqui.
    braze = _dig(payload, "tracking", "on_load", "braze", "data") or {}
    driver.name = braze.get("RideOwnerName") or ""
    driver.rating_label = str(braze.get("RideOwnerRating") or "")
    driver.rating = _to_float(driver.rating_label)
    driver.is_super_driver = bool(braze.get("IsSuperDriver"))
    driver.is_verified = bool(braze.get("IsDriverIdentityVerified"))
    driver.avatar = braze.get("RideOwnerPictureURL") or ""

    profile: dict = {}
    for condition in payload.get("trip_conditions") or []:
        if "driver" in condition:
            profile = condition["driver"].get("profile") or {}
            for pref in condition["driver"].get("preferences") or []:
                if pref.get("type") == "VEHICLE":
                    driver.vehicle = pref.get("label") or ""
            break

    driver.name = driver.name or profile.get("display_name") or ""
    driver.profile_id = _dig(profile, "profile_action", "id") or ""

    # info[] e heterogeneo: cada item tem exatamente uma chave identificando o tipo.
    for item in profile.get("info") or []:
        if "cancellation_rate" in item:
            rate = item["cancellation_rate"] or {}
            driver.cancellation_code = rate.get("code") or ""
            driver.cancellation_label = rate.get("label") or ""
        elif "driver_message" in item:
            driver.message = (item["driver_message"] or {}).get("message") or ""

    co2 = ""
    for condition in payload.get("trip_conditions") or []:
        if "co2_emissions" in condition:
            co2 = (condition["co2_emissions"] or {}).get("label") or ""

    return RideDetails(
        trip_id=_dig(payload, "multimodal_id", "id") or "",
        driver=driver,
        approval_mode=_dig(payload, "call_to_action", "action", "book", "approval_mode") or "",
        co2=co2,
        raw=payload,
    )


def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Busca viagens no BlaBlaCar Brasil")
    ap.add_argument("origem")
    ap.add_argument("destino")
    ap.add_argument("data", help="YYYY-MM-DD")
    ap.add_argument("--seats", type=int, default=1)
    ap.add_argument("--supply", default="ALL", choices=["ALL", "CARPOOLING", "BUS", "TRAIN"])
    ap.add_argument("--json", action="store_true", help="imprime o JSON normalizado")
    args = ap.parse_args()

    bbc = BlaBlaCar()
    t0 = time.time()
    trips = bbc.search(args.origem, args.destino, args.data, seats=args.seats, supply=args.supply)
    elapsed = time.time() - t0

    if args.json:
        payload = [{k: v for k, v in t.__dict__.items() if k != "raw"} for t in trips]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(f"{len(trips)} viagens | {args.origem} -> {args.destino} | {args.data} | {elapsed:.1f}s")
    for t in sorted(trips, key=lambda x: x.price_value or 1e9):
        kind = "carona" if t.source == "CARPOOLING" else "onibus"
        promo = f" ({t.discount} de {t.original_price})" if t.discount else ""
        print(
            f"  {t.departure_time}-{t.arrival_time} ({t.duration:>5}) {t.price:>11} {kind:6} "
            f"{t.provider[:22]:22} {t.seat_class[:16]:16} {t.departure_place[:16]:16}->{t.arrival_place[:16]:16}{promo}"
        )


if __name__ == "__main__":
    _main()
