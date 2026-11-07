"""Point d'entrée de l'interface : streamlit run frontend/app.py"""

import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import streamlit as st  # noqa: E402

from frontend import api_client  # noqa: E402
from frontend.api_client import ApiError, SessionExpiredError  # noqa: E402
from frontend.views import (  # noqa: E402
    render_alerts,
    render_chat,
    render_dashboard,
    render_missions,
)

PAGES = {
    "Tableau de bord": render_dashboard,
    "Alertes": render_alerts,
    "Missions": render_missions,
    "Chatbot": render_chat,
}
PAGE_ICONS = {
    "Tableau de bord": ":material/dashboard:",
    "Alertes": ":material/notifications:",
    "Missions": ":material/local_shipping:",
    "Chatbot": ":material/smart_toy:",
}
ROLE_LABELS = {"executive": "Dirigeant", "operator": "Exploitant", "admin": "Administrateur"}
SIDEBAR_FOOTER_STYLE = """
<style>
.st-key-sidebar-footer { position: fixed; bottom: 1rem; width: 15rem !important; max-width: 15rem; }
[data-testid="stSidebarUserContent"] { padding-bottom: 7rem; }
.st-key-sidebar-nav, .st-key-sidebar-chats { gap: 0.25rem; }
.st-key-sidebar-nav button, .st-key-sidebar-chats button {
    justify-content: flex-start; border: none; border-radius: 8px;
    min-height: 2.5rem; padding: 0.4rem 0.75rem;
}
.st-key-sidebar-nav button > div, .st-key-sidebar-chats button > div { justify-content: flex-start; }
.st-key-sidebar-chats button p { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.st-key-sidebar-nav button[data-testid="stBaseButton-tertiary"]:hover,
.st-key-sidebar-chats button[data-testid="stBaseButton-tertiary"]:hover {
    background: rgba(128, 128, 128, 0.12);
}
.st-key-sidebar-nav button[data-testid="stBaseButton-primary"],
.st-key-sidebar-nav button[data-testid="stBaseButton-primary"]:hover,
.st-key-sidebar-chats button[data-testid="stBaseButton-primary"],
.st-key-sidebar-chats button[data-testid="stBaseButton-primary"]:hover {
    background: rgba(37, 99, 235, 0.12); color: #2563eb; font-weight: 600;
}
</style>
"""


def render_login() -> None:
    st.title("FleetMind AI")
    with st.form("login"):
        username = st.text_input("Identifiant")
        password = st.text_input("Mot de passe", type="password")
        submitted = st.form_submit_button("Se connecter")
    if not submitted:
        return
    try:
        result = api_client.get_api().login(username.strip(), password)
    except ApiError as error:
        st.error(str(error))
        return
    st.session_state.clear()
    st.session_state["auth"] = {"token": result["access_token"], "user": result["user"]}
    st.rerun()


def render_navigation() -> str:
    """Affiche les liens de navigation et retourne la page active."""
    current = st.session_state.setdefault("page", next(iter(PAGES)))
    nav = st.sidebar.container(key="sidebar-nav")
    for name, icon in PAGE_ICONS.items():
        kind = "primary" if name == current else "tertiary"
        if nav.button(name, key=f"nav-{name}", icon=icon, type=kind, width="stretch"):
            st.session_state["page"] = name
            st.rerun()
    return current


def render_app(auth: dict) -> None:
    user = auth["user"]
    api = api_client.get_api().with_token(auth["token"])
    st.sidebar.title("FleetMind AI")
    st.sidebar.caption("Agent d'exploitation local.")
    page = render_navigation()
    st.markdown(SIDEBAR_FOOTER_STYLE, unsafe_allow_html=True)
    footer = st.sidebar.container(key="sidebar-footer")
    allowed = "tous" if "*" in user["allowed_tms"] else ", ".join(user["allowed_tms"])
    footer.caption(f"{user['username']}, {ROLE_LABELS.get(user['role'], user['role'])}. TMS : {allowed}.")
    if footer.button("Se déconnecter"):
        try:
            api.logout()
        except ApiError:
            pass
        st.session_state.clear()
        st.rerun()
    try:
        PAGES[page](api, user)
    except SessionExpiredError as error:
        st.session_state.clear()
        st.warning(str(error))
        st.button("Revenir à la connexion")
    except ApiError as error:
        st.error(str(error))


st.set_page_config(page_title="FleetMind AI", layout="wide")
current = st.session_state.get("auth")
if current:
    render_app(current)
else:
    render_login()
