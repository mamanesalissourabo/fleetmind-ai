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
ROLE_LABELS = {"executive": "Dirigeant", "operator": "Exploitant", "admin": "Administrateur"}
SIDEBAR_FOOTER_STYLE = """
<style>
.st-key-sidebar-footer { position: fixed; bottom: 1rem; }
[data-testid="stSidebarUserContent"] { padding-bottom: 7rem; }
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


def render_app(auth: dict) -> None:
    user = auth["user"]
    api = api_client.get_api().with_token(auth["token"])
    st.sidebar.title("FleetMind AI")
    st.sidebar.caption("Agent d'exploitation local. Données et procédures de démonstration.")
    page = st.sidebar.radio("Navigation", list(PAGES), label_visibility="collapsed")
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
