from datetime import date, datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..battery import METER_CHARGER, effective_meter
from ..active_sessions import clear_report, list_active
from ..auth import get_current_user
from ..consumption import compute_vehicle_consumptions
from ..database import get_db
from ..fees import load_allocation
from ..geocode import reverse_geocode
from ..sync import record_deletion
from .locations import match_location

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

# Obergrenze fuer ein AUSDRUECKLICH angefordertes `limit` (siehe list_sessions).
# Kein Default - wer nichts angibt, bekommt alles. Der Wert begrenzt nur, wie
# gross eine einzelne angeforderte Seite sein darf.
MAX_PAGE_SIZE = 5000


def attach_consumption(db: Session, user_id: str, sessions: list[models.ChargingSession]) -> None:
    """Reichert die uebergebenen (bereits geladenen) Sessions in-place mit
    consumption_kwh_per_100km/consumption_method an. Laedt dafuer pro
    betroffenem Fahrzeug die komplette Session-Historie, da die Berechnung
    Vorgaenger und Vollladungs-Intervalle ueber ALLE Sessions braucht - nicht
    nur die evtl. gefilterte/limitierte Ergebnismenge (siehe consumption.py).
    user_id-Filter ist hier defensiv (Vehicle-FK macht Fremdzugriff ueber
    vehicle_id strukturell ohnehin unmoeglich), aber macht die Grenze explizit."""
    vehicle_ids = {s.vehicle_id for s in sessions}
    for vehicle_id in vehicle_ids:
        vehicle = db.get(models.Vehicle, vehicle_id)
        full_history = (
            db.query(models.ChargingSession)
            .filter(
                models.ChargingSession.vehicle_id == vehicle_id,
                models.ChargingSession.user_id == user_id,
            )
            .order_by(models.ChargingSession.start_time)
            .all()
        )
        capacity = vehicle.battery_capacity_kwh if vehicle else None
        results = compute_vehicle_consumptions(full_history, capacity)
        for s in sessions:
            if s.vehicle_id != vehicle_id:
                continue
            result = results.get(s.id)
            s.consumption_kwh_per_100km = result.value if result else None
            s.consumption_method = result.method if result else None

    # Anteil an Grundgebuehren (fees.py) - aus demselben Grund hier und nicht
    # gespeichert wie der Verbrauch: er haengt an ALLEN Vorgaengen der
    # Periode, nicht nur an der (evtl. gefilterten) Ergebnismenge.
    shares = load_allocation(db, user_id).shares if sessions else {}
    for s in sessions:
        s.fee_share = shares.get(s.id)

    # Messort der kWh (battery.py) - wie der Verbrauch nur Antwort: haengt am
    # Anbieter, dessen Einstellung sich jederzeit aendern kann.
    provider_meters = {
        p.id: p.energy_meter
        for p in db.query(models.Provider).filter(models.Provider.user_id == user_id).all()
    } if sessions else {}
    for s in sessions:
        s.energy_meter_effective = effective_meter(s, provider_meters)


def normalize_energy_meter(session: models.ChargingSession, db: Session) -> None:
    """Speichert den Messort nur als AUSNAHME: entspricht er dem Anbieter (bzw.
    ohne Anbieter der Ladesaeule), bleibt die Spalte leer. So wirkt eine
    spaetere Aenderung am Anbieter auf alle Vorgaenge zurueck, die ihm nur
    folgen - auch wenn ein Client den vorbefuellten Wert einfach mitschickt."""
    if session.energy_meter is None:
        return
    default = METER_CHARGER
    if session.provider_id:
        provider = db.get(models.Provider, session.provider_id)
        if provider is not None and provider.energy_meter:
            default = provider.energy_meter
    if session.energy_meter == default:
        session.energy_meter = None


def resolve_location(session: models.ChargingSession, user_id: str, db: Session) -> None:
    """Matcht einen bekannten Ladeort (nur unter den eigenen) per GPS, sonst
    Fallback auf Reverse-Geocoding."""
    if session.latitude is None or session.longitude is None:
        return
    matched = match_location(db, user_id, session.latitude, session.longitude)
    if matched:
        session.location_id = matched.id
    else:
        session.geocoded_place = reverse_geocode(session.latitude, session.longitude)


def estimate_energy_kwh(session: models.ChargingSession, vehicle: models.Vehicle) -> None:
    """Fuellt energy_kwh aus dem SoC-Delta, falls kein echter Wert vorliegt."""
    if session.energy_kwh is not None:
        return
    if (
        session.soc_start is not None
        and session.soc_end is not None
        and vehicle.battery_capacity_kwh
    ):
        delta_pct = max(session.soc_end - session.soc_start, 0)
        session.energy_kwh = round(vehicle.battery_capacity_kwh * delta_pct / 100, 2)
        session.energy_is_estimated = True


def apply_provider_price(session: models.ChargingSession, db: Session) -> None:
    """Schlaegt Anbieter+Preis anhand des gematchten Ladeorts vor, wenn noch nichts gesetzt ist."""
    if session.provider_id or not session.location_id:
        return
    location = db.get(models.ChargingLocation, session.location_id)
    if not location or not location.default_provider_id:
        return
    provider = db.get(models.Provider, location.default_provider_id)
    if not provider:
        return
    session.provider_id = provider.id
    price = (
        provider.last_price_dc_per_kwh
        if session.charging_type == models.ChargingType.DC
        else provider.last_price_ac_per_kwh
    )
    if price is not None:
        session.price_per_kwh = price
        if session.energy_kwh:
            session.price_total = round(price * session.energy_kwh, 2)


def clamp_grid_energy(session: models.ChargingSession) -> None:
    """Kappt den Netzbezug auf die geladene Energie. Zwei Zaehler (Wallbox und
    Netzanschluss bzw. die Aufteilung in Home Assistant) driften gegeneinander,
    ein Netzanteil ueber 100 % ist aber in keiner Auswertung sinnvoll.
    Nur gegen ECHTE kWh: eine Schaetzung aus dem SoC-Delta liegt ohne
    Ladeverluste naturgemaess unter dem, was an der Wallbox ankam - dagegen
    zu kappen, hiesse eine Messung an einer Schaetzung zu verbiegen."""
    if (
        session.grid_energy_kwh is None
        or session.energy_kwh is None
        or session.energy_is_estimated
    ):
        return
    if session.grid_energy_kwh > session.energy_kwh:
        session.grid_energy_kwh = session.energy_kwh


def sync_provider_last_price(session: models.ChargingSession, db: Session) -> None:
    """Merkt sich den zuletzt gezahlten Preis beim Anbieter (Preis-Gedaechtnis)."""
    if not session.provider_id or session.price_per_kwh is None:
        return
    # Mit gemessenem Netzanteil (Heimladung mit PV/Hausakku) ist der Preis je
    # kWh ein Mischwert aus Netzstrom und kostenlosem Sonnenstrom - z.B.
    # 0,08 EUR/kWh bei 75 % Sonne. Als "letzter Preis" des Anbieters wuerde er
    # jede spaetere Ladung ohne Messung (Nacht, Winter, manuell erfasst) viel
    # zu billig vorschlagen. Deshalb bleibt das Preis-Gedaechtnis bei solchen
    # Vorgaengen unangetastet - auch beim spaeteren Pruefen/Speichern ueber
    # update_session(), nicht nur beim Push selbst. Kam alles aus dem Netz,
    # ist der Wert ein echter (ggf. ueber den Tarif gemittelter) Netzpreis und
    # darf ins Gedaechtnis.
    if (
        session.grid_energy_kwh is not None
        and session.energy_kwh is not None
        and session.grid_energy_kwh < session.energy_kwh - 1e-6
    ):
        return
    provider = db.get(models.Provider, session.provider_id)
    if not provider:
        return
    if session.charging_type == models.ChargingType.DC:
        provider.last_price_dc_per_kwh = session.price_per_kwh
    else:
        provider.last_price_ac_per_kwh = session.price_per_kwh


def charge_local_now() -> datetime:
    """Naive Ortszeit, wie start_time in der DB liegt."""
    return datetime.now()


def _get_owned_vehicle(db: Session, user: models.User, vehicle_id: str) -> models.Vehicle:
    vehicle = (
        db.query(models.Vehicle)
        .filter(models.Vehicle.id == vehicle_id, models.Vehicle.user_id == user.id)
        .first()
    )
    if not vehicle:
        raise HTTPException(404, "Vehicle nicht gefunden")
    return vehicle


@router.get("", response_model=list[schemas.SessionOut])
def list_sessions(
    vehicle_id: str | None = None,
    needs_review: bool | None = None,
    # Datumsfilter (inklusive) fuer Web-UI/App - siehe attach_consumption(): die
    # Verbrauchsberechnung selbst laedt unabhaengig davon immer die VOLLSTAENDIGE
    # Fahrzeug-Historie, ist also von diesem Filter nicht betroffen.
    start_date: date | None = None,
    end_date: date | None = None,
    # OHNE Angabe: vollstaendige Ergebnismenge. Frueher stand hier `limit=200` als
    # Default - und KEIN Client hat je einen Wert mitgeschickt (Web-UI, iOS,
    # Android). Wer von Spritmonitor mehrere hundert Vorgaenge importiert hatte,
    # sah ueberall nur die 200 neuesten, ohne Hinweis und ohne Moeglichkeit, an
    # die aelteren zu kommen; die Apps spiegelten sie entsprechend nie. Dass die
    # Statistik (serverseitig aggregiert, siehe routers/stats.py) trotzdem die
    # vollstaendigen Zahlen zeigte, machte die Luecke noch unauffaelliger.
    # Unbegrenzt ist hier vertretbar, weil attach_consumption() unten ohnehin die
    # KOMPLETTE Fahrzeughistorie laedt (die Verbrauchskette braucht sie) - ein
    # Limit auf der Ergebnismenge sparte also noch nie die eigentliche Arbeit.
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.ChargingSession).filter(models.ChargingSession.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.ChargingSession.vehicle_id == vehicle_id)
    if needs_review is not None:
        q = q.filter(models.ChargingSession.needs_review == needs_review)
    if start_date:
        q = q.filter(models.ChargingSession.start_time >= datetime.combine(start_date, time.min))
    if end_date:
        q = q.filter(models.ChargingSession.start_time <= datetime.combine(end_date, time.max))
    q = q.order_by(models.ChargingSession.start_time.desc())
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    sessions = q.all()
    attach_consumption(db, user.id, sessions)
    return sessions


# --- Laufende Ladevorgaenge (siehe active_sessions.py) ----------------------
# Vor den "/{session_id}"-Routen registriert, sonst faengt DELETE
# "/{session_id}" den Pfad "/active" ab.

@router.get("/active", response_model=list[schemas.ActiveSessionOut])
def list_active_sessions(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    return list_active(db, user)


def _vehicle_by_external_id(db: Session, user: models.User, external_id: str) -> models.Vehicle:
    vehicle = (
        db.query(models.Vehicle)
        .filter(models.Vehicle.external_id == external_id, models.Vehicle.user_id == user.id)
        .first()
    )
    if not vehicle:
        raise HTTPException(404, f"Kein Vehicle mit external_id '{external_id}' fuer diesen Nutzer")
    return vehicle


@router.post("/active", status_code=204)
def report_active_session(
    payload: schemas.ActiveSessionPush,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Home Assistant meldet Ladebeginn bzw. einen Zwischenstand. Felder, die
    nicht mitkommen, bleiben beim Zwischenstand erhalten - nur ein neuer
    Ladebeginn (andere start_time) beginnt von vorn."""
    vehicle = _vehicle_by_external_id(db, user, payload.vehicle_external_id)
    start = payload.start_time
    if start is not None and start.tzinfo is not None:
        # Wie beim Push am Ladeende: die Wanduhrzeit zaehlt (Ortszeit).
        start = start.replace(tzinfo=None)
    report = (
        db.query(models.ActiveChargeReport)
        .filter(models.ActiveChargeReport.vehicle_id == vehicle.id)
        .first()
    )
    if report is None or (start is not None and start != report.start_time):
        if report is not None:
            db.delete(report)
            db.flush()
        report = models.ActiveChargeReport(
            user_id=user.id,
            vehicle_id=vehicle.id,
            start_time=start or charge_local_now(),
        )
        db.add(report)
    for field in ("soc_start", "soc_current", "energy_kwh", "odometer_km", "outside_temp_c"):
        value = getattr(payload, field)
        if value is not None:
            setattr(report, field, value)
    if payload.charging_type is not None:
        report.charging_type = payload.charging_type.value
    if report.soc_current is None:
        report.soc_current = report.soc_start
    report.updated_at = datetime.utcnow()
    db.commit()


@router.delete("/active", status_code=204)
def clear_active_session(
    vehicle_external_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Laufenden Vorgang ohne Push verwerfen (abgebrochen, nie geladen)."""
    vehicle = _vehicle_by_external_id(db, user, vehicle_external_id)
    clear_report(db, user.id, vehicle.id)
    db.commit()


@router.post("", response_model=schemas.SessionOut, status_code=201)
def create_session(
    payload: schemas.SessionCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    vehicle = _get_owned_vehicle(db, user, payload.vehicle_id)

    session = models.ChargingSession(
        **payload.model_dump(), source=models.SessionSource.MANUAL, user_id=user.id
    )
    # Wer eine Temperatur schickt, ohne ihre Herkunft zu nennen, hat sie von
    # Hand eingetragen - kein Client muss das Feld dafuer kennen.
    if session.outside_temp_c is not None and session.outside_temp_source is None:
        session.outside_temp_source = models.TemperatureSource.MANUAL
    if not session.location_id:
        resolve_location(session, user.id, db)
    normalize_energy_meter(session, db)
    estimate_energy_kwh(session, vehicle)
    clamp_grid_energy(session)
    if session.price_total and session.energy_kwh and not session.price_per_kwh:
        session.price_per_kwh = round(session.price_total / session.energy_kwh, 4)
    elif session.price_per_kwh and session.energy_kwh and not session.price_total:
        session.price_total = round(session.price_per_kwh * session.energy_kwh, 2)

    sync_provider_last_price(session, db)

    db.add(session)
    db.commit()
    db.refresh(session)
    attach_consumption(db, user.id, [session])
    return session


@router.patch("/{session_id}", response_model=schemas.SessionOut)
def update_session(
    session_id: str,
    payload: schemas.SessionUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    session = (
        db.query(models.ChargingSession)
        .filter(models.ChargingSession.id == session_id, models.ChargingSession.user_id == user.id)
        .first()
    )
    if not session:
        raise HTTPException(404, "Ladevorgang nicht gefunden")

    fields = payload.model_dump(exclude_unset=True)
    # Pflichtspalten: `null` heisst hier "nicht aendern", nicht "leeren".
    for required in ("vehicle_id", "source"):
        if required in fields and fields[required] is None:
            del fields[required]
    if "vehicle_id" in fields:
        _get_owned_vehicle(db, user, fields["vehicle_id"])
    if fields.get("external_session_id") == "":
        fields["external_session_id"] = None
    previous_energy = session.energy_kwh
    previous_temp = session.outside_temp_c
    previous_total = session.price_total
    for field, value in fields.items():
        setattr(session, field, value)

    # Herkunft der Temperatur nachziehen. Dieselbe Vorsicht wie beim
    # energy_is_estimated-Flag weiter unten: Web-UI und Apps schicken
    # outside_temp_c bei JEDEM Speichern mit, ein blosses Oeffnen und Speichern
    # darf einen vom Wetterdienst geholten Wert also nicht zu "von Hand"
    # umetikettieren. Nur eine tatsaechliche Wertaenderung zaehlt.
    if "outside_temp_source" not in fields and "outside_temp_c" in fields:
        if session.outside_temp_c is None:
            session.outside_temp_source = None
        elif previous_temp is None or abs(session.outside_temp_c - previous_temp) > 1e-6:
            session.outside_temp_source = models.TemperatureSource.MANUAL

    # Korrigiert der Nutzer eine geschaetzte Energiemenge (z.B. abgelesen aus der
    # App des Ladeanbieters), ist der Wert nicht mehr geschaetzt - sonst bliebe die
    # Session dauerhaft als "(geschätzt)" bzw. Verbrauchsmethode `estimated_energy`
    # markiert. Web-UI und Apps senden energy_kwh bei JEDEM Speichern mit, deshalb
    # zaehlt nur eine tatsaechliche Wertaenderung als Korrektur - reines Oeffnen und
    # Speichern (z.B. zum Abhaken von needs_review) laesst das Flag unangetastet.
    # Ein explizit mitgeschicktes energy_is_estimated hat Vorrang.
    if "energy_is_estimated" not in fields and session.energy_is_estimated:
        if session.energy_kwh is None:
            # Wert geleert - es gibt nichts mehr, das geschaetzt sein koennte.
            # estimate_energy_kwh() setzt das Flag gleich unten neu, falls es aus
            # dem SoC-Delta erneut rechnen kann.
            session.energy_is_estimated = False
        elif previous_energy is None or abs(session.energy_kwh - previous_energy) > 1e-6:
            session.energy_is_estimated = False

    # Auch bei einem Anbieterwechsel ohne mitgeschickten Messort: eine
    # Ausnahme, die dem neuen Anbieter entspricht, ist keine mehr.
    normalize_energy_meter(session, db)

    vehicle = db.get(models.Vehicle, session.vehicle_id)
    estimate_energy_kwh(session, vehicle)
    # Auch wenn nur energy_kwh nach unten korrigiert wurde.
    clamp_grid_energy(session)
    # Ein geaenderter Gesamtpreis gewinnt gegen den kWh-Preis: die Apps
    # schicken bei jedem Speichern BEIDE Felder mit (der kWh-Preis also
    # unveraendert, wenn nur der Gesamtpreis eingetippt wurde). Ohne diese
    # Unterscheidung rechnete der alte kWh-Preis den neuen Gesamtpreis sofort
    # wieder zurueck - der Gesamtpreis liess sich gar nicht festlegen.
    total_changed = "price_total" in fields and _value_changed(session.price_total, previous_total)
    if (
        session.price_total
        and session.energy_kwh
        and (not session.price_per_kwh or total_changed)
    ):
        session.price_per_kwh = round(session.price_total / session.energy_kwh, 4)
    elif session.price_per_kwh and session.energy_kwh:
        session.price_total = round(session.price_per_kwh * session.energy_kwh, 2)

    sync_provider_last_price(session, db)

    db.commit()
    db.refresh(session)
    attach_consumption(db, user.id, [session])
    return session


def _value_changed(new: float | None, old: float | None) -> bool:
    if new is None or old is None:
        return new is not old
    return abs(new - old) > 1e-6


@router.delete("/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    session = (
        db.query(models.ChargingSession)
        .filter(models.ChargingSession.id == session_id, models.ChargingSession.user_id == user.id)
        .first()
    )
    if not session:
        raise HTTPException(404, "Ladevorgang nicht gefunden")
    # Grabstein VOR dem Loeschen, im selben Commit - siehe sync.record_deletion().
    record_deletion(db, user.id, models.SyncEntityType.SESSION, session.id)
    db.delete(session)
    db.commit()


@router.post("/auto", response_model=schemas.SessionOut, status_code=201)
def push_auto_session(
    payload: schemas.AutoSessionPush,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Wird von der Home-Assistant-Automation nach Ladeende aufgerufen. Der
    Bearer-Token bestimmt den Nutzer - das Fahrzeug muss unter DESSEN Konto
    angelegt sein (typischerweise der Admin-Account, der die echten Fahrzeuge
    besitzt, nicht ein separates leeres HA-Konto - siehe CLAUDE.md)."""
    vehicle = (
        db.query(models.Vehicle)
        .filter(
            models.Vehicle.external_id == payload.vehicle_external_id,
            models.Vehicle.user_id == user.id,
        )
        .first()
    )
    if not vehicle:
        raise HTTPException(
            404,
            f"Kein Vehicle mit external_id '{payload.vehicle_external_id}' fuer diesen Nutzer",
        )

    # Duplikat-Schutz: gleiche external_session_id nicht zweimal anlegen
    existing = (
        db.query(models.ChargingSession)
        .filter(
            models.ChargingSession.external_session_id == payload.external_session_id,
            models.ChargingSession.user_id == user.id,
        )
        .first()
    )
    # Der laufende Vorgang ist hiermit beendet (auch bei einer Dublette).
    clear_report(db, user.id, vehicle.id)

    if existing:
        db.commit()
        attach_consumption(db, user.id, [existing])
        return existing

    session = models.ChargingSession(
        vehicle_id=vehicle.id,
        user_id=user.id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        charging_type=payload.charging_type,
        soc_start=payload.soc_start,
        soc_end=payload.soc_end,
        odometer_km=payload.odometer_km,
        outside_temp_c=payload.outside_temp_c,
        # Home Assistant liest den Wert am Fahrzeug- oder Wettersensor des
        # Nutzers aus, nicht bei einem fremden Dienst - fuer die Auswertung
        # zaehlt er wie ein Fahrzeugwert.
        outside_temp_source=(
            models.TemperatureSource.VEHICLE if payload.outside_temp_c is not None else None
        ),
        latitude=payload.latitude,
        longitude=payload.longitude,
        energy_kwh=payload.energy_kwh,
        # Messort nur zusammen mit einer echten kWh-Angabe - eine Schaetzung
        # (estimate_energy_kwh) ist an keinem Ort gemessen.
        energy_meter=payload.energy_meter if payload.energy_kwh is not None else None,
        grid_energy_kwh=payload.grid_energy_kwh,
        external_session_id=payload.external_session_id,
        source=models.SessionSource.AUTOMATIC,
        needs_review=True,
    )

    resolve_location(session, user.id, db)

    estimate_energy_kwh(session, vehicle)
    clamp_grid_energy(session)
    # Laeuft auch bei gemessenem Preis: hier wird ueber den Ladeort der
    # Anbieter gesetzt, und der wird gebraucht (Messort, Zuhause/Arbeit,
    # Grundgebuehren). Nur der vorgeschlagene Preis wird gleich ersetzt.
    apply_provider_price(session, db)
    # Erst NACH apply_provider_price: der Anbieter steht erst dann fest, und
    # gegen ihn wird der Messort normalisiert.
    normalize_energy_meter(session, db)

    # Gemessene Kosten aus Home Assistant (PV/Hausakku, zeitabhaengiger Tarif)
    # schlagen das Preis-Gedaechtnis des Anbieters: "letzter AC-Preis x kWh"
    # unterstellt, dass jede kWh aus dem Netz kam und gleich viel kostete -
    # bei Sonnenstrom ist beides falsch. Der Preis je kWh wird daraus
    # abgeleitet (Mischpreis inkl. Sonnenanteil) und ist ohne kWh unbekannt.
    # sync_provider_last_price() laeuft hier bewusst nicht - wie bei jedem
    # automatischen Push, und fuer einen Mischpreis erst recht nicht (siehe
    # dort).
    if payload.price_total is not None:
        session.price_total = round(payload.price_total, 2)
        session.price_per_kwh = (
            round(payload.price_total / session.energy_kwh, 4) if session.energy_kwh else None
        )

    # Hat ein Poller (MyŠkoda, VW-Datenportal, Renault, BMW) denselben Vorgang
    # schon angelegt, wird er ergaenzt statt doppelt angelegt. Ein erneuter
    # Push (Warteschlange in Lademonitor-HA) landet ebenfalls hier und aendert
    # beim zweiten Mal nichts mehr.
    from ..session_merge import merge_or_none

    match, _changes = merge_or_none(db, session, "Home Assistant")
    if match is not None:
        db.commit()
        db.refresh(match)
        attach_consumption(db, user.id, [match])
        return match

    db.add(session)
    db.commit()
    db.refresh(session)
    attach_consumption(db, user.id, [session])
    return session
