"""Automatischer Abruf geht von selbst an, sobald die Zugangsdaten stehen.

Wer Zugangsdaten eintraegt, will, dass der Abruf laeuft - der Schalter
"Automatischer Abruf" war aber ein zweiter, leicht vergessener Schritt, und
ohne ihn entstand nie ein Ladevorgang. Die Router schalten ihn deshalb ein,
wenn eine Konfiguration in diesem Speichern vollstaendig WIRD oder neue
Zugangsdaten bekommt. Wer ihn danach bewusst ausschaltet, behaelt das, solange
er nicht erneut Zugangsdaten eintraegt.

Bis 0.45 blieb der Abruf aus, wenn fuer das Fahrzeug schon eine andere
automatische Quelle lief (sonst entstand jeder Vorgang doppelt). Seit 0.46.0
ergaenzt eine zweite Quelle den vorhandenen Vorgang (`session_merge.py`), die
Sperre ist entfallen.
"""

from sqlalchemy.orm import Session


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
    return not complete_before or credentials_entered
