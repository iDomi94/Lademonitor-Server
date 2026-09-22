"""Grundgebuehren und Abos von Anbietern auf Ladevorgaenge umlegen.

Beispiel: Ionity Powerpass, 15 EUR im Monat ab dem 03.05. Die Periode
03.05.-02.06. wird auf alle Ionity-Ladevorgaenge dieses Zeitraums verteilt,
**proportional zur geladenen Energie** - aus 0,39 EUR/kWh an der Saeule wird
so bei jedem Vorgang derselbe effektive Preis. Eine Verteilung je Vorgang
waere einfacher, gaebe einem 5-kWh-Zwischenstopp aber denselben Anteil wie
einer 60-kWh-Ladung.

Wie consumption.py wird das bei JEDEM Abruf frisch gerechnet und nie
gespeichert (Begruendung siehe models.ProviderFee). Deshalb braucht die
Umlage die VOLLSTAENDIGE Menge der Ladevorgaenge des Anbieters - ein
Datumsfilter in der Statistik darf erst NACH der Umlage greifen, sonst
bekaeme der letzte Vorgang vor der Filtergrenze die ganze Gebuehr.

Die Apps rechnen dasselbe lokal nach (LocalFeeAllocator in beiden Repos),
weil ihr Dashboard immer lokal entsteht. Aenderungen an den Regeln hier
MUESSEN dort mitgezogen werden; tests/test_fees.py haelt die Faelle fest,
die beide Seiten gleich beantworten muessen.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from . import models


@dataclass(frozen=True)
class FeePeriod:
    """Eine abgerechnete Periode einer Gebuehr.

    `start` ist inklusive, `end` exklusive: 03.05.-03.06. heisst "bis
    einschliesslich 02.06." - ein Ladevorgang am 03.06. faellt damit genau
    in EINE Periode, nicht in zwei.
    """

    fee_id: str
    provider_id: str
    start: date
    end: date
    amount: float


@dataclass
class Allocation:
    # Ladevorgang-ID -> umgelegter Anteil in EUR (Summe ueber alle Gebuehren).
    shares: dict[str, float] = field(default_factory=dict)
    # Perioden, in denen es gar keinen Ladevorgang des Anbieters gab. Bezahlt
    # sind sie trotzdem - die Statistik zaehlt sie deshalb mit, nur eben an
    # keinem Ladevorgang.
    unallocated: list[FeePeriod] = field(default_factory=list)


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def add_months(anchor: date, months: int) -> date:
    """Monate addieren, immer vom URSPRUENGLICHEN Ankertag aus.

    Ein Abo ab dem 31.01. laeuft am 28.02., 31.03., 30.04. - nicht 28.02.,
    28.03., 28.04., wie es beim Verketten Monat fuer Monat passieren wuerde.
    """
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(anchor.day, monthrange(year, month)[1]))


def periods(fee: models.ProviderFee, until: date) -> list[FeePeriod]:
    """Alle Perioden einer Gebuehr, die bis `until` (einschliesslich) begonnen
    haben.

    Eine angebrochene Periode zaehlt voll - bezahlt ist sie ja. Ist die
    Gebuehr gekuendigt (`end_date`), zaehlt die Periode, in die das Ende
    faellt, ebenfalls noch voll, ihre Ladevorgaenge aber nur bis zum
    Enddatum: danach gab es den Abo-Preis nicht mehr.
    """
    start = _as_date(fee.start_date)
    end_incl = _as_date(fee.end_date) if fee.end_date else None
    if start > until:
        return []

    if fee.interval == models.FeeInterval.ONCE:
        last = end_incl if end_incl and end_incl >= start else start
        return [FeePeriod(fee.id, fee.provider_id, start, last + timedelta(days=1), fee.amount)]

    step = 12 if fee.interval == models.FeeInterval.YEARLY else 1
    result: list[FeePeriod] = []
    k = 0
    while True:
        p_start = add_months(start, k * step)
        if p_start > until or (end_incl is not None and p_start > end_incl):
            break
        p_end = add_months(start, (k + 1) * step)
        if end_incl is not None:
            p_end = min(p_end, end_incl + timedelta(days=1))
        result.append(FeePeriod(fee.id, fee.provider_id, p_start, p_end, fee.amount))
        k += 1
    return result


def charged_to_date(fee: models.ProviderFee, until: date | None = None) -> float:
    """Was die Gebuehr bis heute insgesamt gekostet hat."""
    return round(sum(p.amount for p in periods(fee, until or date.today())), 2)


def _split(amount: float, sessions: list[models.ChargingSession]) -> dict[str, float]:
    """Verteilt `amount` nach kWh auf `sessions`, auf den Cent genau.

    Haben ALLE Vorgaenge keine Energieangabe, wird gleichmaessig verteilt -
    sonst ginge die Gebuehr verloren. Der Rundungsrest landet beim groessten
    Vorgang, damit die Summe exakt den Betrag ergibt.
    """
    weights = [max(s.energy_kwh or 0.0, 0.0) for s in sessions]
    total = sum(weights)
    if total <= 0:
        weights = [1.0] * len(sessions)
        total = float(len(sessions))
    shares = [round(amount * w / total, 2) for w in weights]
    remainder = round(amount - sum(shares), 2)
    if remainder:
        biggest = max(range(len(sessions)), key=lambda i: weights[i])
        shares[biggest] = round(shares[biggest] + remainder, 2)
    return {s.id: share for s, share in zip(sessions, shares)}


def allocate(
    fees: list[models.ProviderFee],
    sessions: list[models.ChargingSession],
    until: date | None = None,
) -> Allocation:
    """Legt alle Perioden aller Gebuehren auf die Ladevorgaenge um.

    `sessions` muss die VOLLSTAENDIGE Menge sein (alle Fahrzeuge des
    Nutzers - ein Abo gilt fuers Konto, nicht fuers Auto). Zugeordnet wird
    ueber das Datum von `start_time`, die als lokale Zeit in der DB liegt und
    damit direkt zu den Kalendertagen der Gebuehr passt.
    """
    until = until or date.today()
    by_provider: dict[str, list[models.ChargingSession]] = {}
    for s in sessions:
        if s.provider_id:
            by_provider.setdefault(s.provider_id, []).append(s)

    # Feste Reihenfolge, damit der Rundungsrest bei gleich grossen Vorgaengen
    # immer beim selben (dem fruehesten) landet - in den Apps genauso.
    for group in by_provider.values():
        group.sort(key=lambda s: (s.start_time, s.id))

    result = Allocation()
    for fee in fees:
        candidates = by_provider.get(fee.provider_id, [])
        for period in periods(fee, until):
            in_period = [s for s in candidates if period.start <= s.start_time.date() < period.end]
            if not in_period:
                result.unallocated.append(period)
                continue
            for session_id, share in _split(period.amount, in_period).items():
                result.shares[session_id] = round(result.shares.get(session_id, 0.0) + share, 2)
    return result


def load_allocation(db: Session, user_id: str) -> Allocation:
    """Umlage fuer einen Nutzer, mit genau den Ladevorgaengen, die sie braucht
    (die der Anbieter mit Gebuehren - alle anderen koennen keinen Anteil
    bekommen)."""
    fees = db.query(models.ProviderFee).filter(models.ProviderFee.user_id == user_id).all()
    if not fees:
        return Allocation()
    provider_ids = {f.provider_id for f in fees}
    sessions = (
        db.query(models.ChargingSession)
        .filter(
            models.ChargingSession.user_id == user_id,
            models.ChargingSession.provider_id.in_(provider_ids),
        )
        .all()
    )
    return allocate(fees, sessions)
