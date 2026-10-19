"""Automatischer Abruf geht von selbst an, sobald die Zugangsdaten stehen.

Wer Zugangsdaten eintraegt, will, dass der Abruf laeuft - der Schalter
"Automatischer Abruf" war aber ein zweiter, leicht vergessener Schritt, und
ohne ihn entstand nie ein Ladevorgang. Die Router schalten ihn deshalb ein,
wenn eine Konfiguration in diesem Speichern vollstaendig WIRD oder neue
Zugangsdaten bekommt. Wer ihn danach bewusst ausschaltet, behaelt das, solange
er nicht erneut Zugangsdaten eintraegt.

Nie eingeschaltet wird, wenn fuer das Fahrzeug schon eine andere automatische
Quelle laeuft - zwei Quellen legen jeden Ladevorgang doppelt an. Dann bleibt
es still beim Aus, statt das Speichern mit 422 abzulehnen.
"""

from sqlalchemy.orm import Session

from . import models

SOURCE_MODELS = (
    models.MySkodaConfig,
    models.BmwCarDataConfig,
    models.VwDataActConfig,
    models.RenaultConfig,
)


def other_source_active(db: Session, user_id: str, vehicle_id: str, own_model) -> bool:
    for model in SOURCE_MODELS:
        if model is own_model:
            continue
        active = (
            db.query(model.id)
            .filter(model.vehicle_id == vehicle_id, model.user_id == user_id, model.enabled == True)  # noqa: E712
            .first()
        )
        if active:
            return True
    return False


def should_enable(
    db: Session,
    config,
    *,
    complete_before: bool,
    complete_now: bool,
    credentials_entered: bool,
) -> bool:
    """True, wenn der Abruf jetzt von selbst eingeschaltet werden soll."""
    if config.enabled or not complete_now:
        return False
    if complete_before and not credentials_entered:
        return False
    return not other_source_active(db, config.user_id, config.vehicle_id, type(config))
