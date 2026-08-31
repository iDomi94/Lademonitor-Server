"""Sehr leichtgewichtiges i18n: JSON-Dateien mit Key->Text-Paaren pro Sprache
(de.json/en.json in diesem Verzeichnis), keine externe Bibliothek noetig.

Deutsch (`de`) ist die Basis-/Fallback-Sprache - fehlt ein Key in einer anderen
Sprachdatei (z.B. weil eine Uebersetzung noch nicht nachgezogen wurde), wird
automatisch der deutsche Text angezeigt statt eines kaputten Keys.

Die aktuell aktive Sprache wird NICHT als Funktionsargument durch jeden
Aufruf durchgereicht, sondern in einer contextvars.ContextVar gehalten
(`set_current_language()` einmal pro Request in main.py). contextvars sind
pro Request/Thread isoliert - auch bei FastAPIs sync Endpunkten, die Starlette
per anyio in einem Threadpool ausfuehrt, kopiert anyio den Kontext beim
Spawnen sauber mit. Das erlaubt, `translate()` als simple Jinja2-Global-
Funktion `t(key)` zu registrieren, ohne bei jedem Template-Aufruf die Sprache
mitgeben zu muessen.
"""

import contextvars
import json
from pathlib import Path

SUPPORTED_LANGUAGES: tuple[str, ...] = ("de", "en")
DEFAULT_LANGUAGE = "de"
LANGUAGE_COOKIE_NAME = "lang"
# ~400 Tage, wie beim Session-Cookie in routers/auth.py - Chrome deckelt
# Cookie-Max-Age ohnehin dort.
LANGUAGE_COOKIE_MAX_AGE = 60 * 60 * 24 * 400

_I18N_DIR = Path(__file__).parent


def _load_translations(lang: str) -> dict[str, str]:
    path = _I18N_DIR / f"{lang}.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


_TRANSLATIONS: dict[str, dict[str, str]] = {
    lang: _load_translations(lang) for lang in SUPPORTED_LANGUAGES
}

_current_language: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_language", default=DEFAULT_LANGUAGE
)


def set_current_language(lang: str | None) -> str:
    """Setzt die fuer den aktuellen Request aktive Sprache (Fallback auf
    DEFAULT_LANGUAGE bei unbekanntem/fehlendem Wert) und gibt sie zurueck -
    praktisch, um sie direkt in den Template-Kontext (`lang=...`) zu
    uebernehmen."""
    resolved = lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
    _current_language.set(resolved)
    return resolved


def get_current_language() -> str:
    return _current_language.get()


def translate(key: str, **kwargs: object) -> str:
    """Jinja2-Global `t()` sowie fuer Verwendung in JS-Bloecken
    (`{{ t('...')|tojson }}`). Deutsch als Fallback, falls ein Key in der
    aktiven Sprache fehlt; der Key selbst als letzter Fallback, falls er
    ueberhaupt nirgends existiert (auffaelliger als eine leere Zeichenkette,
    erleichtert das Nachtragen fehlender Uebersetzungen)."""
    lang = _current_language.get()
    value = _TRANSLATIONS.get(lang, {}).get(key)
    if value is None:
        value = _TRANSLATIONS.get(DEFAULT_LANGUAGE, {}).get(key, key)
    if kwargs:
        try:
            value = value.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return value


def translations_for(lang: str, prefix: str | None = None) -> dict[str, str]:
    """Uebersetzungs-Dict fuer eine Sprache, Deutsch-Keys als Fallback
    ergaenzt - fuer das kleine window.I18N_FILTER-Objekt, das base.html vor
    static/filter.js einbettet (die Datei ist ein normales statisches
    Static-File, kein Jinja2-Template, bekommt Texte also nicht per `t()`).

    `prefix` beschraenkt das Ergebnis auf Keys mit diesem Praefix (z.B.
    "filter." fuer genau die Keys, die filter.js tatsaechlich braucht) -
    haelt das eingebettete JSON klein statt aller ~280 Keys der Seite."""
    resolved = lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
    merged = dict(_TRANSLATIONS.get(DEFAULT_LANGUAGE, {}))
    merged.update(_TRANSLATIONS.get(resolved, {}))
    if prefix is not None:
        merged = {k: v for k, v in merged.items() if k.startswith(prefix)}
    return merged
