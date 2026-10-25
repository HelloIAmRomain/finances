"""Commandes d'administration : `python -m app.cli <commande>`.

- `create-admin` : crée le compte administrateur (mot de passe demandé, secret TOTP affiché
  **une seule fois** pour l'application d'authentification). À lancer soi-même, dans son propre
  terminal : `sudo /srv/finances/deploy/compose.sh run --rm web python -m app.cli create-admin`.
- `seed` : crée l'arborescence de catégories par défaut (idempotent).
- `verifier-conversion` : passe toutes les réponses Enable Banking stockées dans la conversion ;
  n'affiche que des statistiques (porte du lot 1).
- `echantillons-anonymises` : un échantillon anonymisé par banque et type de compte, en JSON,
  pour en faire des cassettes de test (rien de personnel ne sort, cf. `anonymize.py`).
- `limites-observees` : refus de quota (429) constatés par banque, avec le nombre de requêtes des
  24 h précédentes, pour ajuster les profils de `app/connectors/bank_profiles.py` (Q14).
- `fermer-acces` : ferme toutes les sessions et retire tous les téléphones et accès IA, en cas
  de vol ou de fuite (`docs/runbook-vol.md`). Les banques ne sont pas touchées.
"""

import argparse
import getpass
import json
import sys
from datetime import timedelta

from app.assistant.access import revoke_all_tokens
from app.auth.devices import revoke_all_devices
from app.auth.users import MIN_PASSWORD_LENGTH, close_all_sessions, create_user, totp_uri
from app.categorize.seed import seed_categories
from app.config import DEFAULT_SECRETS_DIR, Settings, load_secrets
from app.connectors.budget import observed_limits
from app.connectors.enable_banking.samples import anonymized_samples, conversion_report
from app.crypto import Keyring, configure_keyring
from app.db import make_engine
from app.timeutil import to_paris, utcnow


def _create_admin(username: str) -> None:
    password = getpass.getpass(f"Mot de passe ({MIN_PASSWORD_LENGTH} caractères au moins) : ")
    if password != getpass.getpass("Confirmation : "):
        sys.exit("Les mots de passe diffèrent.")
    settings = Settings()
    configure_keyring(Keyring.from_secrets(load_secrets(DEFAULT_SECRETS_DIR)))
    secret = create_user(
        make_engine(settings.database_path), username=username, password=password, now=utcnow()
    )
    print("Compte créé. Ajouter ce compte dans l'application d'authentification :")
    print(f"  clé : {secret}")
    print(f"  ou lien : {totp_uri(username, secret)}")
    print("Ce secret ne sera plus jamais affiché.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="créer le compte administrateur")
    admin.add_argument("--identifiant", default="admin")
    commands.add_parser("seed", help="créer les catégories par défaut")
    commands.add_parser(
        "verifier-conversion", help="statistiques de conversion sur données réelles"
    )
    commands.add_parser("echantillons-anonymises", help="échantillons anonymisés (JSON)")
    limits = commands.add_parser("limites-observees", help="refus de quota constatés par banque")
    limits.add_argument("--jours", type=int, default=90)
    commands.add_parser(
        "fermer-acces", help="fermer toutes les sessions, retirer téléphones et accès IA"
    )
    args = parser.parse_args(argv)
    if args.command == "create-admin":
        _create_admin(args.identifiant)
    elif args.command == "seed":
        created = seed_categories(make_engine(Settings().database_path))
        print(f"{created} catégorie(s) créée(s).")
    elif args.command == "limites-observees":
        _observed_limits(args.jours)
    elif args.command == "fermer-acces":
        _close_access()
    elif args.command in ("verifier-conversion", "echantillons-anonymises"):
        configure_keyring(Keyring.from_secrets(load_secrets(DEFAULT_SECRETS_DIR)))
        engine = make_engine(Settings().database_path)
        produce = conversion_report if args.command == "verifier-conversion" else anonymized_samples
        print(json.dumps(produce(engine), ensure_ascii=False, indent=2, sort_keys=True))


def _close_access() -> None:
    engine, now = make_engine(Settings().database_path), utcnow()
    sessions = close_all_sessions(engine, now=now)
    devices = revoke_all_devices(engine, now=now)
    tokens = revoke_all_tokens(engine, now=now)
    print(
        f"Accès fermés : {sessions} session(s), {devices} téléphone(s), {tokens} accès IA. "
        "Reconnectez-vous, puis appairez de nouveau le téléphone et recréez l'accès IA."
    )


def _observed_limits(days: int) -> None:
    engine = make_engine(Settings().database_path)
    limits = observed_limits(engine, since=utcnow() - timedelta(days=days))
    if not limits:
        print(f"Aucun refus de quota sur les {days} derniers jours.")
    for limit in limits:
        print(
            f"{to_paris(limit.at):%d/%m/%Y %H:%M}  {limit.bank}  {limit.endpoint}  "
            f"compte {limit.account_id} : refus après {limit.calls_in_window} requête(s) sur 24 h"
        )


if __name__ == "__main__":
    main()
