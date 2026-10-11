"""Erstattungsbericht fuer den Dienstwagen (ab v0.35.0).

Der Arbeitgeber erstattet den Strom fuer HEIMLADUNGEN je kWh - entweder zu
einer Pauschale je kWh (models.ReimbursementRate, mit "gueltig ab") oder zu den
tatsaechlichen Kosten. Der Bericht ist der Nachweis: je Vorgang kWh, Herkunft
der kWh und Betrag, dazu Summen je Monat.

Regeln (tests/test_reimbursement.py haelt sie fest; beide Apps rechnen sie in
LocalReimbursementCalculator nach - Aenderungen dort mitziehen):

- Es zaehlen nur Vorgaenge DIESES Fahrzeugs, deren Anbieter auf "Zuhause"
  steht (Provider.charging_place). "Arbeit" faellt heraus, dort traegt der
  Arbeitgeber den Strom ohnehin; Vorgaenge ohne Anbieter ebenso.
- Zeitraum ueber den lokalen Kalendertag von `start_time`, beide Enden
  inklusive.
- Pauschal: der Satz mit dem juengsten `valid_from` <= Ladetag. Davor gilt
  keiner - der Vorgang bekommt keinen Betrag und wird gezaehlt.
- Tatsaechlich: `price_total`, sonst `price_per_kwh` x kWh, sonst der letzte
  Preis des Heim-Anbieters fuer die Lade-Art x kWh. Auf Wunsch zusaetzlich der
  Grundgebuehren-Anteil (fees.py), der wie dort ueber ALLE Vorgaenge umgelegt
  wird.
- Geschaetzte kWh (SoC-Hub x Akkukapazitaet) zaehlen mit, sind aber als
  "estimated" markiert und in der Summe getrennt ausgewiesen.
- Betraege je Vorgang auf den Cent gerundet; die Summen sind die Summe der
  gerundeten Betraege, damit der Bericht in sich aufgeht.
"""

from __future__ import annotations

import csv
import io
from collections import OrderedDict
from datetime import date, datetime

from sqlalchemy.orm import Session

from . import models, schemas
from .battery import METER_VEHICLE, effective_meter
from .fees import load_allocation
from .i18n import translate

SOURCE_CHARGER = "charger"
SOURCE_VEHICLE = "vehicle"
SOURCE_ESTIMATED = "estimated"

# Vorschlag fuer den ersten Satz (Pauschale 2026), nur fuer die Oberflaechen.
DEFAULT_RATE_PER_KWH = 0.34


def kwh_source(session: models.ChargingSession) -> str:
    if session.energy_is_estimated:
        return SOURCE_ESTIMATED
    return SOURCE_VEHICLE if effective_meter(session) == METER_VEHICLE else SOURCE_CHARGER


def rate_at(rates: list[models.ReimbursementRate], day: date) -> float | None:
    """Satz mit dem juengsten `valid_from` <= `day`."""
    best = None
    for rate in rates:
        start = rate.valid_from.date()
        if start <= day and (best is None or start >= best.valid_from.date()):
            best = rate
    return best.rate_per_kwh if best else None


def _actual_price(session: models.ChargingSession) -> float | None:
    if session.price_total is not None:
        return session.price_total
    kwh = session.energy_kwh
    if not kwh:
        return None
    if session.price_per_kwh is not None:
        return session.price_per_kwh * kwh
    provider = session.provider
    if provider is None:
        return None
    is_dc = session.charging_type is not None and session.charging_type.value == "DC"
    last = provider.last_price_dc_per_kwh if is_dc else provider.last_price_ac_per_kwh
    return last * kwh if last is not None else None


def home_sessions(
    db: Session, vehicle: models.Vehicle, date_from: date, date_to: date
) -> list[models.ChargingSession]:
    sessions = (
        db.query(models.ChargingSession)
        .join(models.Provider, models.ChargingSession.provider_id == models.Provider.id)
        .filter(
            models.ChargingSession.vehicle_id == vehicle.id,
            models.Provider.charging_place == "home",
            models.ChargingSession.start_time >= datetime(date_from.year, date_from.month, date_from.day),
        )
        .order_by(models.ChargingSession.start_time, models.ChargingSession.id)
        .all()
    )
    return [s for s in sessions if s.start_time.date() <= date_to]


def build_report(
    db: Session, user: models.User, vehicle: models.Vehicle, date_from: date, date_to: date
) -> schemas.ReimbursementReportOut:
    sessions = home_sessions(db, vehicle, date_from, date_to)
    mode = vehicle.reimbursement_mode or "flat"
    include_fees = bool(vehicle.reimbursement_include_fees) and mode == "actual"
    rates = (
        db.query(models.ReimbursementRate)
        .filter(models.ReimbursementRate.vehicle_id == vehicle.id)
        .all()
    )
    shares = load_allocation(db, user.id).shares if include_fees else {}

    rows: list[schemas.ReimbursementRowOut] = []
    months: "OrderedDict[str, dict]" = OrderedDict()
    for s in sessions:
        source = kwh_source(s)
        kwh = s.energy_kwh
        fee = round(shares.get(s.id, 0.0), 2) if include_fees else 0.0
        rate: float | None
        amount: float | None
        if mode == "flat":
            rate = rate_at(rates, s.start_time.date())
            amount = round(kwh * rate, 2) if (rate is not None and kwh is not None) else None
        else:
            price = _actual_price(s)
            amount = round(price + fee, 2) if price is not None else None
            rate = round(amount / kwh, 4) if (amount is not None and kwh) else None
        rows.append(
            schemas.ReimbursementRowOut(
                session_id=s.id,
                start_time=s.start_time,
                energy_kwh=kwh,
                kwh_source=source,
                odometer_km=s.odometer_km,
                rate_per_kwh=rate,
                fee_share=fee if amount is not None else 0.0,
                amount=amount,
            )
        )
        key = s.start_time.strftime("%Y-%m")
        m = months.setdefault(key, {"sessions": 0, "kwh": 0.0, "est": 0.0, "amount": 0.0})
        m["sessions"] += 1
        m["kwh"] += kwh or 0.0
        if source == SOURCE_ESTIMATED:
            m["est"] += kwh or 0.0
        m["amount"] += amount or 0.0

    return schemas.ReimbursementReportOut(
        vehicle_id=vehicle.id,
        vehicle_name=vehicle.name,
        license_plate=vehicle.license_plate,
        employee_name=vehicle.employee_name,
        employee_number=vehicle.employee_number,
        mode=mode,
        include_fees=include_fees,
        date_from=date_from,
        date_to=date_to,
        rows=rows,
        months=[
            schemas.ReimbursementMonthOut(
                month=k,
                sessions=v["sessions"],
                energy_kwh=round(v["kwh"], 2),
                estimated_kwh=round(v["est"], 2),
                amount=round(v["amount"], 2),
            )
            for k, v in months.items()
        ],
        total_kwh=round(sum(r.energy_kwh or 0 for r in rows), 2),
        estimated_kwh=round(
            sum(r.energy_kwh or 0 for r in rows if r.kwh_source == SOURCE_ESTIMATED), 2
        ),
        estimated_sessions=sum(1 for r in rows if r.kwh_source == SOURCE_ESTIMATED),
        missing_amount_sessions=sum(1 for r in rows if r.amount is None),
        total_amount=round(sum(r.amount or 0 for r in rows), 2),
    )


def to_csv(report: schemas.ReimbursementReportOut, language: str) -> str:
    """CSV fuer Tabellenkalkulation und Lohnbuchhaltung.

    Deutsch mit Semikolon und Dezimalkomma (so oeffnet Excel sie direkt),
    Englisch mit Komma und Punkt. Kopfzeilen mit Fahrzeug/Mitarbeiter oben,
    Monatssummen unten - eine Datei, die man so weitergeben kann.
    """
    german = language == "de"
    delimiter = ";" if german else ","

    def num(value: float | None, digits: int = 2) -> str:
        if value is None:
            return ""
        text = f"{value:.{digits}f}"
        return text.replace(".", ",") if german else text

    t = translate
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")
    w.writerow([t("reimbursement.csv.title")])
    w.writerow([t("reimbursement.csv.vehicle"), report.vehicle_name])
    if report.license_plate:
        w.writerow([t("reimbursement.csv.license_plate"), report.license_plate])
    if report.employee_name:
        w.writerow([t("reimbursement.csv.employee_name"), report.employee_name])
    if report.employee_number:
        w.writerow([t("reimbursement.csv.employee_number"), report.employee_number])
    w.writerow([
        t("reimbursement.csv.period"),
        f"{report.date_from.isoformat()} - {report.date_to.isoformat()}",
    ])
    w.writerow([t("reimbursement.csv.mode"), t(f"reimbursement.mode.{report.mode}")
                + (f" ({t('reimbursement.csv.with_fees')})" if report.include_fees else "")])
    w.writerow([])
    w.writerow([
        t("reimbursement.csv.date"), t("reimbursement.csv.time"), "kWh",
        t("reimbursement.csv.source"), t("reimbursement.csv.odometer"),
        t("reimbursement.csv.rate"), t("reimbursement.csv.fee_share"), t("reimbursement.csv.amount"),
    ])
    for r in report.rows:
        w.writerow([
            r.start_time.date().isoformat(), r.start_time.strftime("%H:%M"),
            num(r.energy_kwh), t(f"reimbursement.source.{r.kwh_source}"),
            r.odometer_km if r.odometer_km is not None else "",
            num(r.rate_per_kwh, 4), num(r.fee_share) if r.fee_share else "", num(r.amount),
        ])
    w.writerow([])
    w.writerow([
        t("reimbursement.csv.month"), t("reimbursement.csv.sessions"), "kWh",
        t("reimbursement.csv.estimated_kwh"), t("reimbursement.csv.amount"),
    ])
    for m in report.months:
        w.writerow([m.month, m.sessions, num(m.energy_kwh), num(m.estimated_kwh), num(m.amount)])
    w.writerow([
        t("reimbursement.csv.total"), len(report.rows), num(report.total_kwh),
        num(report.estimated_kwh), num(report.total_amount),
    ])
    if report.missing_amount_sessions:
        w.writerow([])
        w.writerow([t("reimbursement.csv.missing", count=report.missing_amount_sessions)])
    # BOM, damit Excel die Umlaute als UTF-8 liest.
    return "﻿" + buf.getvalue()
