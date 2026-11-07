"""Calcul des périodes : le LLM choisit une catégorie, le code calcule les dates."""

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from backend.schemas.chat import Interpretation, PeriodKind, ReportPeriod

PERIOD_WORDS = re.compile(
    r"\b(hier|avant-hier|semaine|mois|ann[ée]e|trimestre|janvier|f[ée]vrier|mars|avril|mai|juin|"
    r"juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre|(?:19|20)\d{2}|\d{1,2}/\d{1,2})\b",
    re.IGNORECASE,
)


class InvalidPeriodError(ValueError):
    """La période demandée est absente, incohérente ou incompréhensible."""


def mentions_period(question: str) -> bool:
    """Détecte une date citée dans la question, indépendamment du LLM."""
    return PERIOD_WORDS.search(question) is not None


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _next_month(day: date) -> date:
    return (_month_start(day) + timedelta(days=32)).replace(day=1)


def _bounds(interpretation: Interpretation, today: date) -> tuple[date, date]:
    """Retourne le premier jour inclus et le jour de fin exclu."""
    monday = today - timedelta(days=today.weekday())
    match interpretation.period:
        case PeriodKind.TODAY:
            return today, today + timedelta(days=1)
        case PeriodKind.YESTERDAY:
            return today - timedelta(days=1), today
        case PeriodKind.THIS_WEEK:
            return monday, monday + timedelta(days=7)
        case PeriodKind.LAST_WEEK:
            return monday - timedelta(days=7), monday
        case PeriodKind.THIS_MONTH:
            return _month_start(today), _next_month(today)
        case PeriodKind.LAST_MONTH:
            start = _month_start(_month_start(today) - timedelta(days=1))
            return start, _month_start(today)
        case PeriodKind.CUSTOM:
            start, end = interpretation.start_date, interpretation.end_date
            if start is None or end is None or start > end:
                raise InvalidPeriodError("Dates de période absentes ou incohérentes")
            return start, end + timedelta(days=1)
    raise InvalidPeriodError("Période inconnue")


def resolve_period(
    interpretation: Interpretation, reference_time: datetime, timezone: str
) -> ReportPeriod | None:
    """Convertit l'interprétation en bornes exactes, ou None si aucune période n'est demandée."""
    if interpretation.period == PeriodKind.NONE:
        return None
    zone = ZoneInfo(timezone)
    start_day, end_day = _bounds(interpretation, reference_time.astimezone(zone).date())
    last_day = end_day - timedelta(days=1)
    return ReportPeriod(
        kind=interpretation.period,
        label=f"du {start_day:%d/%m/%Y} au {last_day:%d/%m/%Y}",
        start=datetime.combine(start_day, time.min, zone).astimezone(UTC),
        end=datetime.combine(end_day, time.min, zone).astimezone(UTC),
    )


# L'ordre compte : les expressions les plus précises sont testées d'abord.
RELATIVE_PERIODS = [
    (re.compile(r"\baujourd'hui\b|\bce jour\b", re.IGNORECASE), PeriodKind.TODAY),
    (re.compile(r"(?<!avant-)\bhier\b", re.IGNORECASE), PeriodKind.YESTERDAY),
    (re.compile(r"\bsemaine (derni[èe]re|pass[ée]e)\b", re.IGNORECASE), PeriodKind.LAST_WEEK),
    (re.compile(r"\bcette semaine\b", re.IGNORECASE), PeriodKind.THIS_WEEK),
    (re.compile(r"\bmois (dernier|pass[ée])\b", re.IGNORECASE), PeriodKind.LAST_MONTH),
    (re.compile(r"\bce mois\b", re.IGNORECASE), PeriodKind.THIS_MONTH),
]
MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
}
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b")
MONTH_NAME = re.compile(r"\b(" + "|".join(MONTHS) + r")\b", re.IGNORECASE)


def correct_relative_period(question: str, interpretation: Interpretation) -> Interpretation:
    """Une expression relative est décidée par le code, quelle que soit la réponse du LLM."""
    for pattern, kind in RELATIVE_PERIODS:
        if pattern.search(question):
            return interpretation.model_copy(update={"period": kind})
    return interpretation


def period_matches_question(question: str, period: ReportPeriod, timezone: str) -> bool:
    """Vérifie que chaque année, mois ou date cités tombent bien dans la période retenue."""
    zone = ZoneInfo(timezone)
    first = period.start.astimezone(zone).date()
    last = (period.end.astimezone(zone) - timedelta(days=1)).date()
    covered_months = set()
    cursor = first.replace(day=1)
    while cursor <= last:
        covered_months.add((cursor.year, cursor.month))
        cursor = _next_month(cursor)

    years = {int(year) for year in YEAR.findall(question)}
    if any(not first.year <= year <= last.year for year in years):
        return False
    for name in MONTH_NAME.findall(question):
        if MONTHS[name.lower()] not in {month for _, month in covered_months}:
            return False
    for day, month, year in NUMERIC_DATE.findall(question):
        try:
            cited = date(int(year) if year else first.year, int(month), int(day))
        except ValueError:
            return False
        if not first <= cited <= last:
            return False
    return True
