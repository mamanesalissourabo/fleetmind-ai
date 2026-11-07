"""Gestion des comptes en ligne de commande : le mot de passe est saisi au clavier."""

import argparse
import getpass
import sys
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.core.database import open_session
from backend.core.security import hash_password
from backend.models.user import User
from backend.schemas.auth import ALL_TMS, Role
from backend.services.auth import check_password_rules, create_user


def _read_password() -> str:
    password = getpass.getpass("Mot de passe : ")
    if password != getpass.getpass("Confirmation : "):
        raise ValueError("Les deux saisies sont différentes")
    return password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Créer un compte")
    create.add_argument("--username", required=True)
    create.add_argument("--role", required=True, choices=[role.value for role in Role])
    create.add_argument("--tms", required=True, help=f"TMS séparés par des virgules, ou {ALL_TMS} pour tous")
    change = commands.add_parser("set-password", help="Changer un mot de passe")
    change.add_argument("--username", required=True)
    deactivate = commands.add_parser("deactivate", help="Désactiver un compte")
    deactivate.add_argument("--username", required=True)
    args = parser.parse_args()

    with open_session() as session:
        try:
            if args.command == "create":
                tms = [item.strip() for item in args.tms.split(",") if item.strip()]
                create_user(session, args.username, _read_password(), Role(args.role), tms, datetime.now(UTC))
            else:
                user = session.scalar(select(User).where(User.username == args.username))
                if user is None:
                    raise ValueError("Compte introuvable")
                if args.command == "set-password":
                    password = _read_password()
                    check_password_rules(password)
                    user.password_hash = hash_password(password)
                else:
                    user.is_active = False
                user.sessions.clear()
            session.commit()
        except (ValueError, IntegrityError) as error:
            message = "Ce compte existe déjà" if isinstance(error, IntegrityError) else str(error)
            print(f"Échec : {message}")
            return 1
    print(f"Terminé pour {args.username}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
