"""Questions de suite : le code reprend le contexte de la réponse précédente, sans l'historique."""

import re
from enum import StrEnum

from backend.agents.periods import RELATIVE_PERIODS, mentions_period
from backend.schemas.chat import ChatIntent, Interpretation, PeriodKind
from backend.schemas.tms_config import TmsConfig

# Seuls ces sujets se prolongent : expliquer une alerte ou citer une procédure exige une demande précise.
INHERITABLE_INTENTS = {
    ChatIntent.OPEN_INCIDENTS,
    ChatIntent.OPEN_ALERTS,
    ChatIntent.DELIVERY_SUMMARY,
    ChatIntent.DELAYED_MISSIONS,
    ChatIntent.MISSING_INFORMATION,
    ChatIntent.SITUATION_BRIEFING,
    ChatIntent.DELAY_RISK,
}
PERIOD_INTENTS = {
    ChatIntent.OPEN_INCIDENTS,
    ChatIntent.OPEN_ALERTS,
    ChatIntent.DELIVERY_SUMMARY,
    ChatIntent.DELAYED_MISSIONS,
    ChatIntent.MISSING_INFORMATION,
}
INTENT_LABELS = {
    ChatIntent.OPEN_INCIDENTS: "incidents ouverts",
    ChatIntent.OPEN_ALERTS: "alertes actives",
    ChatIntent.DELIVERY_SUMMARY: "bilan des missions",
    ChatIntent.DELAYED_MISSIONS: "retards",
    ChatIntent.MISSING_INFORMATION: "informations manquantes",
    ChatIntent.SITUATION_BRIEFING: "point de situation",
    ChatIntent.DELAY_RISK: "risques de retard",
}

FOLLOW_UP_START = re.compile(r"^\W*(et|m[êe]me chose|pareil|idem)\b", re.IGNORECASE)
ALL_TMS = re.compile(r"\b(tous|toutes|partout|ensemble|global(?:ement)?|au total)\b", re.IGNORECASE)
PERIOD_TEXT = re.compile(
    r"\b(aujourd'hui|ce jour|avant-hier|hier|semaine|mois|ann[ée]e|trimestre|derni[eè]re?|"
    r"pass[ée]e?|cette|ce|cet|janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|"
    r"octobre|novembre|d[ée]cembre|(?:19|20)\d{2}|\d{1,2}/\d{1,2}(?:/\d{4})?)\b",
    re.IGNORECASE,
)
FILLER_WORDS = re.compile(
    r"\b(et|chez|pour|par|[àa]|au|aux|en|de|du|des|la|le|les|l|d|sur|dans|c[ôo]t[ée]|alors|"
    r"maintenant|aussi|m[êe]me|chose|pareil|idem|tms|p[ée]riode|qu|est|il|quoi|tous|toutes|"
    r"partout|ensemble|global(?:ement)?|total|entre|depuis|jusqu|ci|l[àa])\b",
    re.IGNORECASE,
)


class FollowUp(StrEnum):
    NONE = "none"
    SAME_SUBJECT = "same_subject"  # « et chez Beta ? » : seuls le TMS ou la période changent
    NEW_SUBJECT = "new_subject"  # « et les incidents ? » : le sujet change, le reste est repris


def _tms_names(config: TmsConfig) -> list[str]:
    return [config.id, config.name.removeprefix("TMS ")]


def cited_tms(question: str, configs: list[TmsConfig]) -> list[str]:
    """TMS cités par leur nom, détectés par le code et non par le LLM."""
    return [
        config.id
        for config in configs
        if any(re.search(rf"\b{re.escape(name)}\b", question, re.IGNORECASE) for name in _tms_names(config))
    ]


def cites_period(question: str) -> bool:
    return mentions_period(question) or any(pattern.search(question) for pattern, _ in RELATIVE_PERIODS)


def needs_llm_for_period(question: str) -> bool:
    """Une période relative est calculée par le code ; des dates précises demandent le LLM."""
    return mentions_period(question) and not any(pattern.search(question) for pattern, _ in RELATIVE_PERIODS)


def _remainder(question: str, configs: list[TmsConfig]) -> str:
    """Ce qui reste une fois retirés les TMS, les périodes et les mots de liaison."""
    text = question
    for config in configs:
        for name in _tms_names(config):
            text = re.sub(rf"\b{re.escape(name)}\b", " ", text, flags=re.IGNORECASE)
    text = PERIOD_TEXT.sub(" ", text)
    text = re.sub(r"[’']", " ", text)
    text = FILLER_WORDS.sub(" ", text)
    return re.sub(r"[\W_]+", "", text)


def classify_follow_up(question: str, previous: Interpretation | None, configs: list[TmsConfig]) -> FollowUp:
    """Seule la réponse précédente compte ; sans contexte, la question est traitée seule."""
    if previous is None or previous.intent not in INHERITABLE_INTENTS:
        return FollowUp.NONE
    if previous.tms_id is not None and previous.tms_id not in {config.id for config in configs}:
        return FollowUp.NONE
    changes_something = bool(cited_tms(question, configs)) or cites_period(question) or bool(
        ALL_TMS.search(question)
    )
    if not _remainder(question, configs):
        return FollowUp.SAME_SUBJECT if changes_something else FollowUp.NONE
    return FollowUp.NEW_SUBJECT if FOLLOW_UP_START.search(question) else FollowUp.NONE


def complete(
    question: str, interpretation: Interpretation, previous: Interpretation, configs: list[TmsConfig]
) -> Interpretation:
    """Reprend le TMS et la période non cités ; ce que la question précise l'emporte toujours."""
    if interpretation.intent not in INHERITABLE_INTENTS:
        return interpretation
    cited = cited_tms(question, configs)
    if len(cited) == 1:
        tms_id = cited[0]
    elif cited or ALL_TMS.search(question):
        tms_id = None
    else:
        tms_id = previous.tms_id
    update: dict = {"tms_id": tms_id}
    if not cites_period(question):
        if interpretation.intent in PERIOD_INTENTS:
            update |= {
                "period": previous.period,
                "start_date": previous.start_date,
                "end_date": previous.end_date,
            }
        else:
            update |= {"period": PeriodKind.NONE, "start_date": None, "end_date": None}
    return interpretation.model_copy(update=update)


def describe(
    interpretation: Interpretation,
    previous: Interpretation,
    question: str,
    configs: list[TmsConfig],
    period_label: str | None,
) -> str:
    """Dit ce qui a été repris de la question précédente, pour que l'exploitant puisse le vérifier."""
    names = {config.id: config.name for config in configs}
    parts = []
    if interpretation.intent == previous.intent:
        parts.append(f"sujet « {INTENT_LABELS[interpretation.intent]} »")
    if not cited_tms(question, configs) and not ALL_TMS.search(question):
        parts.append(names[interpretation.tms_id] if interpretation.tms_id else "tous vos TMS")
    if not cites_period(question) and period_label:
        parts.append(f"période {period_label}")
    if not parts:
        return ""
    return "Suite de la question précédente : " + ", ".join(parts) + "."
