"""Ladeerkennung aus wiederholt abgefragten Fahrzeugzustaenden.

Gemeinsamer Kern fuer jede Quelle, die nur ZUSTAENDE liefert und keine
fertigen Ladevorgaenge: die MyŠkoda Public API (`myskoda_poller.py`) und das
VW-Datenportal nach EU Data Act (`vw_data_act_poller.py`). Herausgeloest aus
`myskoda_poller.py`, als die zweite Quelle dazukam - die Regeln unten sind
dieselben geblieben, nur die quellenspezifischen Teile (Protokolltabelle,
Bezeichnung in der Notiz, Praefix der `external_session_id`, Zeitfenster der
Rueckdatierung) kommen jetzt ueber `DetectionSource` herein.

Ein "Bericht" (`snapshot`) muss dafuer diese Eigenschaften haben:
`charging_state`, `is_plugged_in`, `soc_percent`, `charge_power_kw`,
`charge_type`, `odometer_km`, `outside_temp_c`, `coordinates`,
`is_in_saved_location`, `captured_at`, `fetched_at`; optional
`charged_energy_kwh` (Energiezaehler des Fahrzeugs, siehe "Gemessene Energie").
Den Zustand haelt die Konfigurationszeile der Quelle ueber
`models.ChargeDetectionStateMixin`.

## Schnitt eines Ladevorgangs

Identisch zum HA-Blueprint, damit alle Quellen dasselbe unter einem
"Ladevorgang" verstehen: Beginn = Uebergang "Kabel nicht verbunden" ->
"Kabel verbunden", Ende = der umgekehrte Uebergang. Die Phasen dazwischen,
in denen gerade kein Strom fliesst (Timer laeuft, Zielladestand erreicht,
Ladung unterbrochen), gehoeren mit zum Vorgang.

## Bekannte Ungenauigkeiten (bewusst so, nicht uebersehen)

- **Der Ladebeginn wird erst beim naechsten Bericht bemerkt.** Der Ladevorgang
  hat dann schon bis zu ein Abfrageintervall lang gelaufen. Bei AC-Laden ist
  das vernachlaessigbar (11 kW * 5 min ~ 1 kWh), bei DC-Schnellladen nicht
  (150 kW * 5 min ~ 12 kWh). Dagegen steht die Rueckdatierung, siehe unten.
- **Ein schlafendes Fahrzeug kann einen ganzen Vorgang verstecken**, beim
  VW-Portal auch ein kurzer Vorgang, der komplett zwischen zwei Datensaetzen
  liegt (ein ZIP enthaelt je Datenfeld nur den letzten Wert, keinen Verlauf).
  Dagegen steht die Nacherkennung (`detect_missed_sessions`) ueber einen
  SoC-Sprung oder, wo vorhanden, den Energiezaehler.
- **Alle Zeitstempel stammen wenn moeglich aus dem Fahrzeug** (MyŠkoda:
  `carCapturedTimestamp`, VW-Portal: `car_captured_time`), nicht aus der
  lokalen Uhr - die Antwort kann ein aelterer Cloud-Stand sein.

## Rueckdatierung des Ladebeginns (`backdated_start`)

Bis 2026-09 war `soc_start` immer der SoC beim ERSTEN Abruf, der "steckt"
meldet - mit der Begruendung, der SoC des vorherigen Abrufs waere bei einer
Fahrt zwischen den Abrufen "falsch in die andere Richtung". Das war nur zur
Haelfte richtig und ist bewusst revidiert:

Fahren SENKT den SoC. Solange nicht geladen wird, ist der SoC monoton
fallend, also gilt immer `soc_echt <= min(open_soc_before, open_soc_start)`.
Der vorherige Wert kann damit nie unter dem echten Startwert liegen - er ist
schlimmstenfalls (lange Fahrt dazwischen) zu wenig korrigiert, nie zu viel.
Der einzige Fall, in dem er wirklich in die falsche Richtung zeigt, ist
`open_soc_before > open_soc_start` (viel gefahren, dann DC gestartet) - und
der ist trivial erkennbar.

Belegt an zwei DC-Vorgaengen im MyŠkoda-Debug-Log (05./06.09.2026): erkannt
wurden 30 %->77 % bzw. 64 %->80 %, tatsaechlich waren es 8 %->77 % und
48 %->80 %. Der jeweils letzte Abruf davor stand punktgenau auf 8 % bzw. 48 % -
beim zweiten, obwohl dazwischen noch 5 km gefahren wurden (der 15 min alte
Wert enthielt die Fahrt bereits). Verschluckt wurden so 17 bzw. 12 kWh, was
ueber `estimate_energy_kwh` direkt auf Energie, Kosten und Verbrauchsstatistik
durchschlaegt.

`soc_start` ist deshalb jetzt `open_soc_before`, aber nur unter zwei
Waechtern - ohne sie waere ein komplett unbemerkter Ladevorgang zwischen den
Abrufen (fahren, woanders laden, heimkommen, einstecken) als Zuwachs DIESES
Vorgangs gezaehlt worden:

1. **Alter.** Nur wenn der vorherige Bericht hoechstens
   `source.backdate_limit_seconds(config)` zurueckliegt (bei MyŠkoda
   standardmaessig das Doppelte des Leerlaufintervalls). Ist er aelter, kann
   dazwischen zu viel passiert sein.
2. **Physik.** Aus `open_max_power_kw` und `vehicle.battery_capacity_kwh`
   ergibt sich, wie viele Prozentpunkte in der Luecke ueberhaupt geladen
   worden sein KOENNEN - weiter zurueck als diese Untergrenze wird nicht
   korrigiert. Ohne hinterlegte Akkukapazitaet greift dieser Waechter nicht,
   dann bleibt nur der Alterswaechter.

Die **Startzeit** wird aus derselben Physik mitgezogen, sonst wuerde die
abgeleitete Durchschnittsleistung unphysikalisch (05.09.: 53 kWh in den
gemessenen 19 min waeren 168 kW avg bei 134 kW Peak). Zurueck geht es um die
Zeit, die das nachgetragene SoC-Delta bei der beobachteten Leistung braucht,
hoechstens aber bis zum vorherigen Bericht.

Was NICHT passiert: eine Korrektur um die Fahrstrecke zwischen den Abrufen
(`odometer`). Beim DC-Vorgang vom 06.09. lag der 15 min alte SoC trotz 5 km
Fahrt exakt richtig - die Fahrt steckte schon drin. Abziehen wuerde
ueberkorrigieren; der Kilometerstand dient nur der Diagnose in der Notiz.

Rueckdatierter Start und Rohwerte stehen weiterhin beide in der Notiz des
Ladevorgangs und im Debug-Log, und die Vorgaenge bleiben `needs_review`.

Liegt GEMESSENE Energie vor (siehe unten), aendert die Rueckdatierung nur
SoC-Start und Startzeit - die kWh kommen aus dem Zaehler und haben den
verschluckten Anfang ohnehin schon mitgezaehlt.

## Gemessene Energie (`charged_energy_kwh`)

Liefert eine Quelle einen Energiezaehler des Fahrzeugs (VW-Portal:
`battery_state_report.charge_energy`), wird er statt der Schaetzung aus dem
SoC-Delta genommen. Ob der Wert je Ladevorgang bei null beginnt oder ein
Gesamtzaehler ist, ist nicht dokumentiert - deshalb werden nur die ZUWAECHSE
aufsummiert (`_add_energy`): steigt der Wert, zaehlt die Differenz; faellt er,
hat der Zaehler neu begonnen und der neue Wert zaehlt ab null. Damit gehen
beide Varianten auf. Ausgangswert ist der letzte Stand VOR dem Einstecken
(`last_energy_raw_kwh`). Gibt es keinen (erster Vorgang nach dem Einrichten),
gilt null, solange der erste Wert ein plausibler Einzelvorgang ist: der Enyaq
meldete im ersten Ladebericht schon 13 kWh (SoC 10 -> 25 %), als Ausgangswert
genommen fehlten sie (19.10.2026). Ein hoeherer erster Wert ist ein
Gesamtzaehler und wird zum Ausgangswert. Der
Messort ist das FAHRZEUG (`energy_meter="vehicle"`), Ladeverluste stecken
also nicht drin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from . import models
from .routers.sessions import (
    apply_provider_price,
    estimate_energy_kwh,
    normalize_energy_meter,
    resolve_location,
)

logger = logging.getLogger(__name__)

#: Gemessene Energie ausserhalb dieses Bereichs gilt als Messfehler (Zaehler-
#: sprung, falsche Einheit) - dann bleibt es bei der Schaetzung aus dem SoC.
PLAUSIBLE_ENERGY_KWH = (0.1, 250.0)

#: Ab diesem Zuwachs des Energiezaehlers legt die Nacherkennung einen Vorgang
#: an, auch wenn der SoC-Sprung unter der Schwelle bleibt. 1 kWh sind rund
#: 5 min AC mit 11 kW - darunter eher Rauschen als ein Ladevorgang.
MISSED_SESSION_MIN_ENERGY_KWH = 1.0

#: Signatur der Protokollfunktion einer Quelle.
LogFn = Callable[..., None]


@dataclass(frozen=True)
class DetectionSource:
    """Was die Ladeerkennung ueber die Quelle wissen muss.

    `log(db, config, event, message, *, level="info", snapshot=None,
    force=False)` schreibt ins Protokoll der Quelle. `id_prefix` bildet die
    `external_session_id` (ohne FIN, siehe `create_session`), `label` steht in
    der Notiz am Ladevorgang. `saved_location_label` nennt die App, in der
    "gespeicherte Orte" gepflegt werden - nur wenn die Quelle so etwas kennt.
    """

    label: str
    id_prefix: str
    log: LogFn
    backdate_limit_seconds: Callable[[Any], int]
    discharging_states: frozenset[str] = field(default_factory=frozenset)
    # Zustaende, in denen sicher Strom in den Akku fliesst. Wurde einer davon
    # waehrend des Steckens gesehen, entsteht der Vorgang auch ohne SoC-Zuwachs,
    # Leistung oder Zaehlerstand (siehe finish_open_session).
    charging_states: frozenset[str] = field(default_factory=frozenset)
    saved_location_label: str | None = None


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def charging_type(value: str | None) -> models.ChargingType | None:
    """"AC"/"DC" -> Enum. Alles andere (u.a. "OFF", das die APIs beim
    Nicht-Laden liefern) wird zu None, statt den Vorgang abzulehnen."""
    if not value:
        return None
    upper = value.upper()
    return models.ChargingType(upper) if upper in ("AC", "DC") else None


def fmt_number(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "unbekannt"
    return f"{value:.{decimals}f}".replace(".", ",")


def fmt_minutes(seconds: int | None) -> str:
    if seconds is None:
        return "unbekannt"
    minutes = round(seconds / 60)
    return f"{minutes} min"


def _snapshot_energy(snapshot: Any) -> float | None:
    return getattr(snapshot, "charged_energy_kwh", None)


def _add_energy(config: Any, raw: float | None) -> None:
    """Zuwachs des Energiezaehlers auf den laufenden Vorgang buchen (siehe
    Modul-Docstring, "Gemessene Energie")."""
    if raw is None:
        return
    previous = config.open_energy_raw_kwh
    if previous is None:
        config.open_energy_raw_kwh = raw
        return
    gained = raw - previous if raw >= previous else raw
    config.open_energy_kwh = round((config.open_energy_kwh or 0.0) + gained, 3)
    config.open_energy_raw_kwh = raw


def measured_energy(config: Any) -> float | None:
    """Geladene kWh des laufenden Vorgangs laut Fahrzeugzaehler, oder None,
    wenn keiner gemeldet wurde bzw. der Wert unplausibel ist."""
    energy = config.open_energy_kwh
    if energy is None:
        return None
    low, high = PLAUSIBLE_ENERGY_KWH
    return round(energy, 2) if low <= energy <= high else None


def backdated_start(
    config: Any, capacity_kwh: float | None, limit_seconds: int
) -> tuple[int | None, datetime | None, str | None]:
    """Korrigierter Ladebeginn: `(soc_start, start_time, Begruendung)`.

    Ohne Korrektur kommen die unveraenderten `open_*`-Werte zurueck und die
    Begruendung ist None. Ausfuehrliche Herleitung im Modul-Docstring,
    Abschnitt "Rueckdatierung des Ladebeginns".
    """
    soc_start = config.open_soc_start
    start_time = config.open_start_time
    before = config.open_soc_before
    gap = config.open_gap_before_seconds

    if not config.backdate_session_start:
        return soc_start, start_time, None
    if before is None or soc_start is None or gap is None or start_time is None:
        return soc_start, start_time, None
    if before >= soc_start:
        # Kein Zuwachs verschluckt. Ein HOEHERER Wert davor heisst: dazwischen
        # wurde gefahren, nicht geladen - dann ist der erkannte Wert der
        # richtige und der vorherige der falsche.
        return soc_start, start_time, None

    if gap > limit_seconds:
        return soc_start, start_time, None

    # Physik-Waechter: mehr als das kann in der Luecke nicht geladen worden
    # sein. Ohne Akkukapazitaet am Fahrzeug nicht berechenbar - dann bleibt es
    # beim reinen Alterswaechter oben.
    power = config.open_max_power_kw or 0.0
    floor = float(before)
    if capacity_kwh and power > 0:
        max_gain_pct = power * (gap / 3600) / capacity_kwh * 100
        floor = max(floor, soc_start - max_gain_pct)
    elif capacity_kwh and power <= 0:
        # Nie Leistung gesehen -> es kann auch nichts verschluckt worden sein.
        return soc_start, start_time, None

    corrected = int(round(floor))
    if corrected >= soc_start:
        return soc_start, start_time, None

    # Startzeit aus derselben Physik mitziehen: sonst wird die aus Energie und
    # Dauer abgeleitete Durchschnittsleistung unphysikalisch. Hoechstens bis
    # zum vorherigen Bericht zurueck - davor stand das Kabel nachweislich nicht.
    offset = float(gap)
    if capacity_kwh and power > 0:
        hours = (soc_start - corrected) / 100 * capacity_kwh / power
        offset = min(offset, hours * 3600)
    # Auf ganze Sekunden, wie alle uebrigen Zeitstempel aus dem Fahrzeug - die
    # Startzeit landet u.a. in der `external_session_id` und in der Anzeige.
    corrected_time = (start_time - timedelta(seconds=offset)).replace(microsecond=0)

    reason = (
        f"Ladebeginn zurückdatiert: Startwert {soc_start} % → {corrected} % "
        f"(letzter Abruf davor vor {fmt_minutes(gap)} mit {before} %), "
        f"Startzeit um {fmt_minutes(int(offset))} vorgezogen."
    )
    return corrected, corrected_time, reason


# Ab so vielen Prozentpunkten Abstand gilt der aus der Energie gerechnete
# Startwert als besser als der erfasste. Kleinere Abweichungen sind Rundung
# (SoC ganzzahlig, Zaehler je kWh) und Nennkapazitaet gegen echte Kapazitaet.
ENERGY_START_TOLERANCE_PCT = 5


def energy_based_start(
    soc_start: int | None,
    soc_end: int | None,
    energy: float | None,
    capacity_kwh: float | None,
) -> tuple[int | None, str | None]:
    """Start-SoC aus gemessener Energie nachrechnen: `(soc_start, Begruendung)`.

    Der Start-SoC ist oft der letzte bekannte Wert VOR dem Einstecken, weil der
    Bericht beim Einstecken keinen eigenen SoC hat (VW-Datenportal). Wurde
    seitdem gefahren, ist er zu hoch - ID.3 vom 20.10.2026: 77 % aus einem
    Bericht von 95 km vorher, gemessen wurden 23 kWh bis 90 %, eingesteckt war
    bei rund 50 %. Der Fahrzeugzaehler misst ab Akku (ohne Ladeverluste), also
    ist `soc_end - energy / capacity` der Startwert. Korrigiert wird nur nach
    UNTEN (ohne Laden faellt der SoC nur) und nur bei deutlichem Abstand.
    Ergibt die Rechnung weniger als 0 %, passen Zaehler und Kapazitaet nicht
    zusammen - dann bleibt der erfasste Wert.
    """
    if soc_start is None or soc_end is None or not energy or not capacity_kwh:
        return soc_start, None
    derived = soc_end - energy / capacity_kwh * 100
    if derived < 0 or soc_start - derived < ENERGY_START_TOLERANCE_PCT:
        return soc_start, None
    corrected = int(round(derived))
    reason = (
        f"Start-SoC aus der gemessenen Energie nachgerechnet: {soc_start} % → {corrected} % "
        f"({fmt_number(energy, 2)} kWh bis {soc_end} % bei {fmt_number(capacity_kwh)} kWh "
        "Akkukapazität). Der erfasste Wert stammte vermutlich aus einem Bericht vor einer Fahrt."
    )
    return corrected, reason


def reset_open_session(config: Any) -> None:
    config.open_start_time = None
    config.open_soc_start = None
    config.open_soc_before = None
    config.open_soc_last = None
    config.open_charging_type = None
    config.open_max_power_kw = None
    config.open_odometer_km = None
    config.open_outside_temp_c = None
    config.open_latitude = None
    config.open_longitude = None
    config.open_poll_count = 0
    config.open_gap_before_seconds = None
    config.open_in_saved_location = None
    config.open_energy_kwh = None
    config.open_energy_raw_kwh = None
    config.open_charging_seen = None


# --------------------------------------------------------------------------
# Ladevorgang anlegen
# --------------------------------------------------------------------------

def utc_to_local(when: datetime) -> datetime:
    """Naive UTC -> naive LOKALZEIT des Servers.

    Die Zustandsmaschine rechnet durchweg in naiver UTC (`captured_at` der
    APIs), `ChargingSession.start_time`/`end_time` liegen aber als lokale Zeit
    in der DB - wie beim HA-Push, BMW CarData und den Formularen. Ohne diese
    Umrechnung stand ein Vorgang aus MySkoda, VW-Datenportal oder Renault in
    Mitteleuropa ein bis zwei Stunden zu frueh in der Liste.
    """
    return when.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def create_session(
    db: Session,
    config: Any,
    source: DetectionSource,
    *,
    start_time: datetime,
    end_time: datetime,
    soc_start: int | None,
    soc_end: int | None,
    charging_type: models.ChargingType | None,
    odometer_km: int | None,
    outside_temp_c: float | None,
    latitude: float | None,
    longitude: float | None,
    notes: str,
    energy_kwh: float | None = None,
) -> models.ChargingSession | None:
    """Legt den Ladevorgang an - mit derselben Nachbearbeitung wie der
    Home-Assistant-Push (`routers/sessions.py::push_auto_session`).

    Gibt None zurueck, wenn zu diesem Start bereits ein Vorgang existiert. Die
    `external_session_id` enthaelt die Startzeit, ist also stabil ueber
    Neustarts hinweg - ein zweimal verarbeiteter Ladevorgang legt keine
    Dublette an.
    """
    vehicle = db.get(models.Vehicle, config.vehicle_id)
    if not vehicle:
        return None

    # Vehicle.id (interne UUID) statt der FIN (siehe crypto.py) - die FIN ist
    # verschluesselt genau WEIL sie personenbezogen ist, waere hier aber
    # wieder im Klartext gelandet: external_session_id braucht per Dubletten-
    # check einen exakten SQL-Gleichheitsvergleich (siehe unten) und kann
    # deshalb selbst nicht verschluesselt werden. Vehicle.id erfuellt densel-
    # ben Zweck (pro Fahrzeug eindeutig) ohne die FIN preiszugeben.
    external_id = f"{source.id_prefix}-{vehicle.id}-{start_time.strftime('%Y%m%dT%H%M%S')}"
    existing = (
        db.query(models.ChargingSession)
        .filter(
            models.ChargingSession.external_session_id == external_id,
            models.ChargingSession.user_id == config.user_id,
        )
        .first()
    )
    if existing:
        return None

    session = models.ChargingSession(
        vehicle_id=vehicle.id,
        user_id=config.user_id,
        start_time=utc_to_local(start_time),
        end_time=utc_to_local(end_time),
        charging_type=charging_type,
        soc_start=soc_start,
        soc_end=soc_end,
        odometer_km=odometer_km,
        outside_temp_c=outside_temp_c,
        # Wert aus der Fahrzeug-API - fuer die Auswertung dieselbe Herkunft wie
        # ein ueber Home Assistant gemeldeter Sensorwert (siehe
        # models.TemperatureSource).
        outside_temp_source=(
            models.TemperatureSource.VEHICLE if outside_temp_c is not None else None
        ),
        latitude=latitude,
        longitude=longitude,
        external_session_id=external_id,
        source=models.SessionSource.AUTOMATIC,
        needs_review=True,
        notes=notes,
    )
    if energy_kwh is not None:
        # Vom Fahrzeug selbst gezaehlt, also ohne Ladeverluste (siehe
        # Modul-Docstring, "Gemessene Energie").
        session.energy_kwh = energy_kwh
        session.energy_is_estimated = False
        session.energy_meter = "vehicle"

    resolve_location(session, config.user_id, db)
    estimate_energy_kwh(session, vehicle)
    apply_provider_price(session, db)
    normalize_energy_meter(session, db)

    db.add(session)
    return session


# --------------------------------------------------------------------------
# Zustandsmaschine
# --------------------------------------------------------------------------

def start_open_session(db: Session, config: Any, snapshot: Any, source: DetectionSource) -> None:
    """Uebergang "nicht verbunden" -> "verbunden"."""
    gap_seconds = None
    if config.last_captured_at:
        gap_seconds = max(0, int((snapshot.captured_at - config.last_captured_at).total_seconds()))

    config.open_start_time = snapshot.captured_at
    # Das VW-Datenportal liefert manche Berichte ohne SoC (nur Einstellungen
    # und Ladezustand). Dann ist der letzte bekannte Wert die beste Angabe -
    # ohne Laden faellt der SoC nur, er liegt also hoechstens zu hoch, wie bei
    # der Rueckdatierung.
    soc_now = snapshot.soc_percent if snapshot.soc_percent is not None else config.last_soc
    config.open_soc_start = soc_now
    config.open_soc_before = config.last_soc
    config.open_soc_last = soc_now
    config.open_charging_type = snapshot.charge_type
    config.open_max_power_kw = snapshot.charge_power_kw
    config.open_odometer_km = snapshot.odometer_km
    # Genau jetzt gemessen - siehe models.ChargingSession.outside_temp_c:
    # der Verbrauch dieses Vorgangs beschreibt die Fahrt DAVOR, und die endet
    # in diesem Moment. Liefert die API keine Temperatur, bleibt es bei None.
    config.open_outside_temp_c = snapshot.outside_temp_c
    config.open_poll_count = 1
    config.open_charging_seen = False
    config.open_gap_before_seconds = gap_seconds
    config.open_in_saved_location = snapshot.is_in_saved_location
    coords = snapshot.coordinates
    if coords:
        config.open_latitude, config.open_longitude = coords
    # Ausgangswert des Energiezaehlers: der letzte Stand vor dem Einstecken,
    # damit auch der Teil zaehlt, den das Abfrageintervall verschluckt hat.
    config.open_energy_kwh = None
    config.open_energy_raw_kwh = config.last_energy_raw_kwh
    first_energy = _snapshot_energy(snapshot)
    if (
        config.open_energy_raw_kwh is None
        and first_energy is not None
        and first_energy <= PLAUSIBLE_ENERGY_KWH[1]
    ):
        # Kein Stand vor dem Einstecken: ein Zaehler je Vorgang begann bei
        # null (siehe Modul-Docstring, "Gemessene Energie").
        config.open_energy_raw_kwh = 0.0
    _add_energy(config, first_energy)

    source.log(
        db,
        config,
        "session_started",
        f"Ladevorgang begonnen (Zustand {snapshot.charging_state}), SoC "
        f"{soc_now if soc_now is not None else 'unbekannt'}%, "
        f"letzter Abruf davor vor {fmt_minutes(gap_seconds)} mit SoC "
        f"{config.open_soc_before if config.open_soc_before is not None else 'unbekannt'}%.",
        snapshot=snapshot,
        force=True,
    )


def update_open_session(db: Session, config: Any, snapshot: Any) -> None:
    """Laufender Vorgang - Maximalwerte und zuletzt bekannte Angaben mitfuehren."""
    config.open_poll_count = (config.open_poll_count or 0) + 1

    if snapshot.soc_percent is not None:
        # max(), nicht einfach ueberschreiben: faellt der SoC waehrend des
        # Steckens (Vorklimatisierung aus der Batterie), soll der erreichte
        # Hoechststand den Ladevorgang beschreiben.
        config.open_soc_last = max(config.open_soc_last or 0, snapshot.soc_percent)

    power = snapshot.charge_power_kw
    if power is not None:
        config.open_max_power_kw = max(config.open_max_power_kw or 0.0, power)

    # Die Lade-Art steht erst fest, sobald wirklich Strom fliesst - beim ersten
    # Abruf direkt nach dem Einstecken melden die APIs oft noch "OFF".
    if charging_type(config.open_charging_type) is None:
        resolved = charging_type(snapshot.charge_type)
        if resolved:
            config.open_charging_type = resolved.value

    if snapshot.odometer_km is not None:
        config.open_odometer_km = snapshot.odometer_km

    # Position nur uebernehmen, solange sie zum Ladeort gehoert: beim Ausstecken
    # kann das Fahrzeug schon wieder unterwegs sein (parking_state IN_MOTION,
    # dann liefert die API ohnehin keine Koordinaten).
    coords = snapshot.coordinates
    if coords:
        config.open_latitude, config.open_longitude = coords

    _add_energy(config, _snapshot_energy(snapshot))


def finish_open_session(
    db: Session, config: Any, snapshot: Any, source: DetectionSource
) -> None:
    """Uebergang "verbunden" -> "nicht verbunden": Ladevorgang abschliessen."""
    # Der Zaehlerstand beim Ausstecken gehoert noch zum Vorgang.
    _add_energy(config, _snapshot_energy(snapshot))

    # Den vom Abfrageintervall verschluckten Anfang nachtragen, bevor irgendwas
    # daraus abgeleitet wird (Dauer, SoC-Delta, external_session_id).
    vehicle = db.get(models.Vehicle, config.vehicle_id)
    soc_start, backdated_time, backdate_reason = backdated_start(
        config,
        vehicle.battery_capacity_kwh if vehicle else None,
        source.backdate_limit_seconds(config),
    )

    start_time = backdated_time or config.open_start_time or snapshot.captured_at
    end_time = snapshot.captured_at
    if end_time <= start_time:
        # Kann passieren, wenn die API einen aelteren Cloud-Stand liefert als
        # beim Ladebeginn - dann ist die lokale Uhr die ehrlichere Angabe.
        end_time = max(snapshot.fetched_at, start_time + timedelta(minutes=1))

    soc_end = config.open_soc_last
    if snapshot.soc_percent is not None:
        soc_end = max(soc_end or 0, snapshot.soc_percent)

    if backdate_reason:
        source.log(db, config, "session_backdated", backdate_reason, snapshot=snapshot)

    max_power = config.open_max_power_kw or 0.0
    energy = measured_energy(config)
    soc_start, energy_reason = energy_based_start(
        soc_start, soc_end, energy, vehicle.battery_capacity_kwh if vehicle else None
    )
    if energy_reason:
        source.log(db, config, "session_backdated", energy_reason, snapshot=snapshot)
    delta = (soc_end - soc_start) if (soc_start is not None and soc_end is not None) else None

    # Eingesteckt, aber nie geladen (Zielladestand war schon erreicht, oder das
    # Kabel steckte nur kurz): kein Ladevorgang, sondern Rauschen. Hat die
    # Quelle dagegen einmal "laedt" gemeldet, entsteht der Vorgang trotzdem -
    # lieber einer mit Luecken (needs_review) als gar keiner. Beim VW-Datenportal
    # fehlen SoC und Leistung in den Teilberichten oft genau waehrend des Ladens.
    if (
        (delta is None or delta <= 0)
        and max_power <= 0
        and energy is None
        and not config.open_charging_seen
    ):
        source.log(
            db,
            config,
            "session_discarded",
            f"Kabel war verbunden, aber es wurde nichts geladen (SoC {soc_start}% -> {soc_end}%, "
            f"max. Leistung {fmt_number(max_power)} kW) - kein Ladevorgang angelegt.",
            level="warning",
            snapshot=snapshot,
            force=True,
        )
        reset_open_session(config)
        return

    notes = detection_note(config, source, delta, backdate_reason, energy)
    if energy_reason:
        notes = f"{notes}\n{energy_reason}"
    session = create_session(
        db,
        config,
        source,
        start_time=start_time,
        end_time=end_time,
        soc_start=soc_start,
        soc_end=soc_end,
        charging_type=charging_type(config.open_charging_type),
        odometer_km=snapshot.odometer_km or config.open_odometer_km,
        # Der beim Einstecken gemerkte Wert hat Vorrang; nur wenn es ihn nicht
        # gibt (Konfiguration aus der Zeit vor dieser Spalte, oder die API hat
        # ihn damals nicht geliefert), der jetzige als Notbehelf.
        outside_temp_c=(
            config.open_outside_temp_c
            if config.open_outside_temp_c is not None
            else snapshot.outside_temp_c
        ),
        latitude=config.open_latitude,
        longitude=config.open_longitude,
        notes=notes,
        energy_kwh=energy,
    )

    duration_min = round((end_time - start_time).total_seconds() / 60)
    if session is None:
        source.log(
            db,
            config,
            "session_duplicate",
            f"Ladevorgang mit diesem Beginn existiert bereits - nicht erneut angelegt "
            f"(SoC {soc_start}% -> {soc_end}%).",
            level="warning",
            snapshot=snapshot,
            force=True,
        )
    else:
        source.log(
            db,
            config,
            "session_finished",
            f"Ladevorgang angelegt: SoC {soc_start}% -> {soc_end}%, Dauer {duration_min} min, "
            f"max. {fmt_number(max_power)} kW, Lade-Art "
            f"{config.open_charging_type or 'unbekannt'}, {config.open_poll_count} Abfragen"
            + (f", {fmt_number(energy, 2)} kWh gemessen." if energy is not None else "."),
            snapshot=snapshot,
            force=True,
        )

    reset_open_session(config)


def detection_note(
    config: Any,
    source: DetectionSource,
    delta: int | None,
    backdate_reason: str | None = None,
    energy: float | None = None,
) -> str:
    """Diagnose-Notiz am Ladevorgang.

    Bewusst am Datensatz selbst und nicht nur im Log: beim Nachbearbeiten
    (needs_review) ist genau das die Information, mit der sich beurteilen
    laesst, wie zuverlaessig die automatisch erfassten Werte sind. Deshalb
    stehen hier immer die ROHWERTE beider Abrufe, auch wenn zurueckdatiert
    wurde - sonst laesst sich die Korrektur nachtraeglich nicht mehr pruefen.
    """
    lines = [f"Automatisch erkannt über {source.label}."]

    if config.open_soc_before is not None and config.open_soc_start is not None:
        missed = config.open_soc_before - config.open_soc_start
        lines.append(
            f"SoC beim letzten Abruf vor dem Einstecken: {config.open_soc_before} % "
            f"(vor {fmt_minutes(config.open_gap_before_seconds)}), "
            f"beim ersten Abruf mit steckendem Kabel: {config.open_soc_start} %."
        )
        if backdate_reason:
            # Nicht aus `missed` neu gerechnet: der Physik-Waechter kann weniger
            # als die volle Differenz nachgetragen haben, `backdate_reason`
            # nennt die tatsaechlich angesetzten Werte.
            lines.append(backdate_reason)
        elif missed < 0:
            lines.append(
                f"Achtung: dazwischen wurden bereits {abs(missed)} Prozentpunkte geladen, "
                "die in diesem Vorgang fehlen (Abfrageintervall)."
            )

    lines.append(
        f"Max. beobachtete Ladeleistung: {fmt_number(config.open_max_power_kw)} kW, "
        f"{config.open_poll_count} Abfragen während des Vorgangs."
    )
    if delta is not None:
        lines.append(f"Erfasstes SoC-Delta: {delta} Prozentpunkte.")
    if energy is not None:
        lines.append(
            f"Geladene Energie laut Fahrzeug: {fmt_number(energy, 2)} kWh "
            "(ohne Ladeverluste)."
        )
    if source.saved_location_label and config.open_in_saved_location is not None:
        lines.append(
            f"Fahrzeug stand beim Einstecken an einem in der {source.saved_location_label} "
            "gespeicherten Ort."
            if config.open_in_saved_location
            else f"Fahrzeug stand beim Einstecken nicht an einem in der "
            f"{source.saved_location_label} gespeicherten Ort."
        )
    return "\n".join(lines)


def missed_energy(config: Any, snapshot: Any) -> float | None:
    """Zuwachs des Energiezaehlers seit dem letzten Bericht, wenn kein Vorgang
    offen ist - dieselbe Regel wie `_add_energy` (faellt der Wert, hat der
    Zaehler neu begonnen und zaehlt ab null). None ohne Zaehler oder
    Ausgangswert."""
    raw = _snapshot_energy(snapshot)
    previous = config.last_energy_raw_kwh
    if raw is None or previous is None:
        return None
    gained = raw - previous if raw >= previous else raw
    return round(gained, 2)


def detect_missed_session(
    db: Session, config: Any, snapshot: Any, source: DetectionSource
) -> bool:
    """Nacherkennung ueber einen SoC-Sprung oder den Energiezaehler.

    Greift nur, wenn zwischen zwei Berichten gar kein "verbunden"-Zustand
    gesehen wurde - typischerweise weil das Fahrzeug geschlafen hat und die
    API zwischenzeitlich einen alten Stand ausgeliefert hat, oder (VW-Portal)
    weil der ganze Vorgang zwischen zwei Datensaetzen lag. Ergebnis ist
    zwangslaeufig grob (Start- und Endzeit sind nur die beiden Berichte), aber
    besser als ein fehlender Vorgang; `needs_review` faengt das ab.

    Zwei Ausloeser:

    - **SoC-Sprung** ab `missed_session_min_soc_delta` Prozentpunkten. Die
      Schwelle muss Rekuperation auf langer Gefaellestrecke ueberstehen, ein
      kurzer AC-Vorgang (10 min bei 11 kW, rund 3 %) bleibt darunter.
    - **Energiezaehler** des Fahrzeugs ab `MISSED_SESSION_MIN_ENERGY_KWH`.
      Rekuperation zaehlt er nicht mit, er taugt deshalb genau fuer die
      kurzen Vorgaenge, die der SoC-Sprung verpasst. Der Zuwachs wird dann
      auch als gemessene Energie uebernommen.
    """
    if not config.detect_missed_sessions:
        return False
    if config.last_captured_at is None:
        return False
    start_time = config.last_captured_at
    end_time = snapshot.captured_at
    if end_time <= start_time:
        return False

    delta = None
    if config.last_soc is not None and snapshot.soc_percent is not None:
        delta = snapshot.soc_percent - config.last_soc
    by_soc = delta is not None and delta >= max(1, config.missed_session_min_soc_delta)

    energy = missed_energy(config, snapshot)
    low, high = PLAUSIBLE_ENERGY_KWH
    if energy is not None and not (low <= energy <= high):
        energy = None
    by_energy = energy is not None and energy >= MISSED_SESSION_MIN_ENERGY_KWH

    if not by_soc and not by_energy:
        return False

    soc_text = (
        f"der SoC von {config.last_soc} % auf {snapshot.soc_percent} % gestiegen"
        if delta is not None
        else "der SoC unbekannt geblieben"
    )
    if by_energy:
        reason = (
            f"Zwischen zwei Berichten hat der Energiezähler des Fahrzeugs "
            f"{fmt_number(energy, 2)} kWh gezählt und {soc_text}, ohne dass ein "
            "Ladezustand beobachtet wurde (der Vorgang lag vermutlich komplett "
            "zwischen zwei Berichten)."
        )
    else:
        reason = (
            f"Zwischen zwei Abfragen ist {soc_text}, ohne dass ein Ladezustand "
            "beobachtet wurde (Fahrzeug hat vermutlich geschlafen)."
        )
    notes = (
        f"Automatisch nacherkannt über {source.label}.\n"
        f"{reason}\n"
        "Start- und Endzeit sind deshalb nur die Zeitpunkte der beiden Abfragen, "
        "die Lade-Art ist unbekannt."
    )
    session = create_session(
        db,
        config,
        source,
        start_time=start_time,
        end_time=end_time,
        soc_start=config.last_soc,
        soc_end=snapshot.soc_percent,
        charging_type=None,
        odometer_km=snapshot.odometer_km,
        # Nacherkennung: es gab nie ein beobachtetes Einstecken, also auch
        # keinen gemerkten Wert - der aktuelle Abruf ist alles, was vorliegt.
        outside_temp_c=snapshot.outside_temp_c,
        latitude=snapshot.coordinates[0] if snapshot.coordinates else None,
        longitude=snapshot.coordinates[1] if snapshot.coordinates else None,
        notes=notes,
        energy_kwh=energy if by_energy else None,
    )
    trigger = (
        f"Energiezaehler +{fmt_number(energy, 2)} kWh"
        if by_energy
        else f"SoC-Sprung {config.last_soc}% -> {snapshot.soc_percent}%"
    )
    source.log(
        db,
        config,
        "missed_session",
        f"{trigger} ohne beobachteten "
        + (
            "Ladezustand - Ladevorgang nachtraeglich angelegt."
            if session is not None
            else "Ladezustand - Vorgang existierte bereits."
        ),
        level="warning",
        snapshot=snapshot,
        force=True,
    )
    return session is not None


def apply_snapshot(db: Session, config: Any, snapshot: Any, source: DetectionSource) -> bool:
    """Verarbeitet einen Bericht: Uebergaenge erkennen, Zustand fortschreiben.

    Gibt zurueck, ob der Bericht einen Ladezustand enthielt. Ohne Ladezustand
    wird bewusst NICHTS entschieden (ein laufender Vorgang wuerde sonst
    faelschlich beendet) - das Protokollieren uebernimmt dann der Aufrufer,
    weil nur er weiss, warum der Teil fehlt.
    """
    state = snapshot.charging_state
    if state is None:
        return False

    plugged_in = snapshot.is_plugged_in
    had_open_session = config.open_start_time is not None

    if plugged_in and not had_open_session:
        start_open_session(db, config, snapshot, source)
    elif plugged_in and had_open_session:
        update_open_session(db, config, snapshot)
        source.log(
            db,
            config,
            "poll",
            f"Ladevorgang laeuft (Zustand {state}), SoC {snapshot.soc_percent}%, "
            f"{fmt_number(snapshot.charge_power_kw)} kW.",
            snapshot=snapshot,
        )
    elif not plugged_in and had_open_session:
        finish_open_session(db, config, snapshot, source)
    else:
        detected = detect_missed_session(db, config, snapshot, source)
        if not detected:
            note = " (Rueckspeisung)" if state in source.discharging_states else ""
            source.log(
                db,
                config,
                "poll",
                f"Kein Ladevorgang (Zustand {state}{note}), SoC {snapshot.soc_percent}%.",
                snapshot=snapshot,
            )
    # Erst nach start_open_session, das den Merker zuruecksetzt.
    if plugged_in and state in source.charging_states:
        config.open_charging_seen = True

    config.last_charging_state = state
    # Ein Bericht ohne SoC loescht den letzten bekannten nicht - sonst fehlten
    # beim naechsten Einstecken Start-SoC und Rueckdatierung.
    if snapshot.soc_percent is not None:
        config.last_soc = snapshot.soc_percent
    config.last_captured_at = snapshot.captured_at
    raw_energy = _snapshot_energy(snapshot)
    if raw_energy is not None:
        config.last_energy_raw_kwh = raw_energy
    return True
