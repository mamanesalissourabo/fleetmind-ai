"""Écrans de l'interface."""

from typing import Any

import streamlit as st

from frontend.api_client import ApiError, FleetMindApi
from frontend.formatting import (
    ACTION_LABELS,
    NOT_PROVIDED,
    SEVERITY_LABELS,
    STATUS_LABELS,
    alert_row,
    format_time,
    label,
    mission_row,
    sort_alerts,
)

ROLES_ACTING_ON_ALERTS = ("operator", "admin")


def render_dashboard(api: FleetMindApi, user: dict[str, Any]) -> None:
    st.title("Tableau de bord")
    status = api.monitoring_status()
    if not status["scheduler_running"]:
        st.warning("La surveillance automatique est arrêtée : la situation peut être obsolète.")
    alerts = api.alerts()
    active = [a for a in alerts if a["status"] != "RESOLVED"]
    urgent = [a for a in active if a["status"] == "OPEN" and a["severity"] in ("HIGH", "UNKNOWN")]
    late = [t["source_tms"] for t in status["tms"] if t["overdue"]]

    first, second, third = st.columns(3)
    first.metric("Alertes actives", len(active))
    second.metric("Graves, non prises en charge", len(urgent))
    third.metric("TMS en retard de surveillance", len(late))
    if late:
        st.warning(f"Surveillance en retard : {', '.join(late)}. Absence d'alerte ne signifie pas absence de problème.")

    st.subheader("À traiter d'abord")
    if urgent:
        rows = [alert_row(a) for a in sort_alerts(urgent)[:5]]
        st.dataframe(rows, hide_index=True, width="stretch")
    elif late:
        st.info("Aucune alerte grave enregistrée, mais la surveillance est incomplète.")
    else:
        st.success("Aucune alerte grave non prise en charge.")

    st.subheader("Surveillance par TMS")
    st.dataframe(
        [
            {
                "TMS": t["source_tms"],
                "Dernier cycle": format_time(t["last_run_at"]),
                "Résultat": t["last_run_status"] or NOT_PROVIDED,
                "Déclenchement": t["last_run_trigger"] or NOT_PROVIDED,
                "Dernier succès": format_time(t["last_success_at"]),
                "En retard": "Oui" if t["overdue"] else "Non",
                "Erreur": t["error"] or "",
            }
            for t in status["tms"]
        ],
        hide_index=True,
        width="stretch",
    )
    if user["role"] == "admin":
        _render_admin(api)


def _render_admin(api: FleetMindApi) -> None:
    with st.expander("Actions d'administration"):
        if st.button("Lancer un cycle maintenant"):
            with st.spinner("Cycle en cours..."):
                results = api.run_cycle()
            for result in results:
                st.write(
                    f"{result['report']['source_tms']} : {result['report']['status']}, "
                    f"{result['alerts']['opened']} alerte(s) ouverte(s), "
                    f"{result['alerts']['notifications']} notification(s)."
                )
        if st.button("Réindexer les procédures"):
            with st.spinner("Réindexation..."):
                result = api.reindex_documents()
            st.write(f"{result['sections_indexed']} sections indexées.")


def render_missions(api: FleetMindApi, user: dict[str, Any]) -> None:
    st.title("Missions")
    st.caption("Données lues à l'instant dans chaque TMS, sous forme normalisée.")
    for source in api.missions():
        st.subheader(source["source_tms"])
        if not source["available"]:
            st.error(f"Données indisponibles : aucune conclusion possible. {source['error'] or ''}")
            continue
        st.caption(f"Lu le {format_time(source['retrieved_at'])}")
        if source["rejected_records"]:
            st.warning(f"{source['rejected_records']} enregistrement(s) illisible(s) écarté(s).")
        rows = [mission_row(m) for m in source["missions"]]
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
        else:
            st.info("Aucune mission dans ce TMS.")


def render_alerts(api: FleetMindApi, user: dict[str, Any]) -> None:
    st.title("Alertes")
    all_alerts = api.alerts()
    tms_options = sorted({a["source_tms"] for a in all_alerts})
    left, right = st.columns(2)
    chosen_status = left.selectbox(
        "Statut", ["Actives", "Ouvertes", "Prises en charge", "Résolues", "Toutes"]
    )
    chosen_tms = right.selectbox("TMS", ["Tous", *tms_options])

    alerts = [a for a in all_alerts if _matches(a, chosen_status, chosen_tms)]
    if not alerts:
        st.info("Aucune alerte pour ces critères.")
        return
    ordered = sort_alerts(alerts)
    st.dataframe([alert_row(a) for a in ordered], hide_index=True, width="stretch")

    labels = {
        a["id"]: f"N° {a['id']}, {a['mission_external_id']} : {a['message'][:70]}" for a in ordered
    }
    alert_id = st.selectbox("Alerte à consulter", list(labels), format_func=labels.get)
    _render_alert_detail(api, alert_id, user)


def _matches(alert: dict[str, Any], status: str, tms: str) -> bool:
    wanted = {
        "Actives": {"OPEN", "ACKNOWLEDGED"},
        "Ouvertes": {"OPEN"},
        "Prises en charge": {"ACKNOWLEDGED"},
        "Résolues": {"RESOLVED"},
        "Toutes": {"OPEN", "ACKNOWLEDGED", "RESOLVED"},
    }
    return alert["status"] in wanted.get(status, set()) and tms in ("Tous", alert["source_tms"])


def _render_alert_detail(api: FleetMindApi, alert_id: int, user: dict[str, Any]) -> None:
    alert = api.alert(alert_id)
    st.subheader(f"Alerte {alert['id']} : {alert['mission_external_id']}")
    st.write(alert["message"])
    columns = st.columns(3)
    columns[0].metric("Gravité", label(SEVERITY_LABELS, alert["severity"]))
    columns[1].metric("Statut", label(STATUS_LABELS, alert["status"]))
    columns[2].metric("Niveau d'escalade", alert["escalation_level"])
    st.caption(f"Règle {alert['rule_id']}, version {alert['ruleset_version']}, TMS {alert['source_tms']}")

    if alert["status"] != "RESOLVED":
        if user["role"] in ROLES_ACTING_ON_ALERTS:
            _render_actions(api, alert, user)
        else:
            st.caption("Votre rôle permet de consulter cette alerte, pas de la traiter.")
    if st.button("Expliquer cette alerte", key=f"explain-{alert_id}"):
        with st.spinner("Analyse en cours, cela peut prendre une minute..."):
            st.session_state[f"analysis-{alert_id}"] = api.ask(f"Explique l'alerte {alert_id}")
    analysis = st.session_state.get(f"analysis-{alert_id}")
    if analysis:
        render_answer(analysis)

    with st.expander("Historique"):
        st.dataframe(
            [
                {
                    "Heure": format_time(row["occurred_at"]),
                    "Action": label(ACTION_LABELS, row["action"]),
                    "Auteur": row["actor"],
                    "Note": row["note"] or "",
                }
                for row in alert["history"]
            ],
            hide_index=True,
            width="stretch",
        )
    with st.expander("Notifications simulées (aucun envoi réel)"):
        if alert["notifications"]:
            st.dataframe(
                [
                    {
                        "Heure": format_time(row["created_at"]),
                        "Type": row["kind"],
                        "Destinataire": row["recipient"],
                        "Message": row["message"],
                    }
                    for row in alert["notifications"]
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.write("Aucune notification.")


def _render_actions(api: FleetMindApi, alert: dict[str, Any], user: dict[str, Any]) -> None:
    alert_id = alert["id"]
    with st.form(key=f"actions-{alert_id}"):
        note = st.text_input("Commentaire (facultatif)")
        st.caption(f"L'action sera enregistrée au nom de {user['username']}.")
        left, right = st.columns(2)
        acknowledged = left.form_submit_button(
            "Prendre en charge", disabled=alert["status"] != "OPEN"
        )
        resolved = right.form_submit_button("Résoudre")
    if not (acknowledged or resolved):
        return
    if acknowledged:
        api.acknowledge(alert_id, note.strip())
    else:
        api.resolve(alert_id, note.strip())
    st.rerun()


def render_chat(api: FleetMindApi, user: dict[str, Any]) -> None:
    st.title("Chatbot")
    st.caption(
        "Le chatbot consulte les données et les procédures. Il ne modifie rien et n'envoie aucun message."
    )
    allowed = "tous" if "*" in user["allowed_tms"] else ", ".join(user["allowed_tms"])
    st.caption(f"Vos réponses portent uniquement sur vos TMS autorisés : {allowed}.")
    history = st.session_state.setdefault("chat_history", [])
    for entry in history:
        with st.chat_message("user"):
            st.write(entry["question"])
        with st.chat_message("assistant"):
            if entry["answer"]:
                render_answer(entry["answer"])
            else:
                st.error(entry["error"])

    question = st.chat_input("Par exemple : quels sont les incidents non résolus chez Alpha ?")
    if not question:
        return
    entry: dict[str, Any] = {"question": question, "answer": None, "error": None}
    with st.spinner("Recherche en cours, cela peut prendre une minute..."):
        try:
            entry["answer"] = api.ask(question)
        except ApiError as error:
            entry["error"] = str(error)
    history.append(entry)
    st.rerun()


def render_answer(answer: dict[str, Any]) -> None:
    """Affiche à part ce qui vient du code et ce qui a été rédigé par le LLM."""
    if not answer["answered"]:
        st.info(answer["summary"])
        return
    st.write(answer["summary"])
    source = "rédigé par le LLM local, puis contrôlé" if answer["summary_source"] == "llm" else "produit par le code"
    st.caption(f"Résumé {source}. Les éléments détaillés ci-dessous font foi.")

    for tms in answer["scope"]:
        if not tms["available"]:
            st.error(f"{tms['tms_id']} : données indisponibles, résultat incomplet.")
    if answer["scope"]:
        names = ", ".join(tms["tms_id"] for tms in answer["scope"])
        fresh = "; ".join(f"{t['tms_id']} : {format_time(t['data_as_of'])}" for t in answer["scope"] if t["available"])
        period = f" Période : {answer['period']['label']}." if answer.get("period") else ""
        st.caption(f"Périmètre : {names}.{period} Fraîcheur : {fresh or NOT_PROVIDED}.")

    _render_items("Faits", answer["facts"])
    _render_items("Risques", answer["risks"])
    _render_items("Informations manquantes", answer["missing_information"])

    if answer.get("draft_message"):
        st.markdown("**Brouillon de relance (non envoyé)**")
        st.code(answer["draft_message"], language=None, wrap_lines=True)
        st.caption(f"Destinataire suggéré par les règles : {answer.get('suggested_recipient') or NOT_PROVIDED}")
    if answer.get("sources"):
        first = answer["sources"][0]
        demo = " (procédure de démonstration)" if first["origin"] == "demonstration" else ""
        with st.expander(f"Procédure : {first['title']}, version {first['version']}{demo}"):
            for section in answer["sources"]:
                st.markdown(f"**{section['section']}**")
                st.write(section["excerpt"])


def _render_items(title: str, items: list[dict[str, Any]]) -> None:
    if items:
        st.markdown(f"**{title}**")
        for item in items:
            st.markdown(f"- {item['text']}")
