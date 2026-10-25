"""Configuration de l'application.

Deux sources, volontairement séparées :

- `Settings` : valeurs non secrètes, lues dans les variables d'environnement `FINANCES_*`.
- `Secrets` : secrets lus **uniquement** dans des fichiers (un par secret), déposés dans un tmpfs
  par `deploy/render-secrets.sh` selon la convention des secrets Docker. Un secret passé par
  variable d'environnement est ignoré : l'environnement d'un conteneur est lisible via
  `docker inspect` (inv. 5).
"""

import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.net.allowlist import NTFY_INTERNAL, NTFY_INTERNAL_PORT, PLACEHOLDERS

DEFAULT_SECRETS_DIR = Path("/run/secrets")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINANCES_", extra="ignore")

    database_path: Path = Path("/data/finances.db")
    # Adresse de l'interface via Tailscale, pour les liens des notifications.
    public_base_url: str = "https://finances.invalid"
    ntfy_url: str = "http://ntfy:8080"
    ntfy_topic: str = "finances-alertes"
    # Adresse du serveur ntfy à saisir dans l'application du téléphone (affichée sur la page
    # Alertes) ; vide : celle de l'interface sur le port 8443 (deploy/bootstrap.sh).
    ntfy_public_url: str = ""
    # Surveillance de la sauvegarde (vraie en production, où l'hôte la lance) : T1.21.
    backup_expected: bool = False
    # Plafonds DSP2 par banque, en JSON (Q14, hypothèse) : {"Boursorama": {"transactions": 4}}.
    bank_budgets: dict[str, dict[str, int]] = Field(default_factory=dict)

    # Vérifiés au démarrage : une valeur que la liste blanche du client HTTP refuserait laissait
    # l'application démarrer, puis chaque notification échouait sans bruit.
    @field_validator("public_base_url")
    @classmethod
    def _public_base_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(
                "FINANCES_PUBLIC_BASE_URL doit être l'adresse HTTPS de l'interface, par exemple"
                " https://pi.exemple.ts.net (nom donné par Tailscale)"
            )
        return value.strip().rstrip("/")

    @field_validator("ntfy_url")
    @classmethod
    def _ntfy_url(cls, value: str) -> str:
        expected = f"http://{NTFY_INTERNAL}:{NTFY_INTERNAL_PORT}"
        if value.strip().rstrip("/") != expected:
            raise ValueError(
                f"FINANCES_NTFY_URL doit valoir {expected} (serveur ntfy de deploy/compose.yml) :"
                " toute autre adresse est refusée par la liste blanche du client HTTP"
            )
        return expected

    @field_validator("ntfy_topic")
    @classmethod
    def _ntfy_topic(cls, value: str) -> str:
        if not re.fullmatch(PLACEHOLDERS["topic"], value):
            raise ValueError(
                "FINANCES_NTFY_TOPIC doit commencer par « finances- », suivi de minuscules,"
                " chiffres ou tirets (64 au plus), par exemple finances-alertes"
            )
        return value

    def phone_ntfy_url(self) -> str:
        if self.ntfy_public_url:
            return self.ntfy_public_url.rstrip("/")
        parts = urlsplit(self.public_base_url)
        return f"{parts.scheme}://{parts.hostname}:8443"


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir=DEFAULT_SECRETS_DIR, extra="ignore")

    # Signature des sessions et des jetons CSRF (T1.22).
    app_secret_key: SecretStr = Field(min_length=32)
    # Chiffrement applicatif et index de recherche (T1.3) : 32 octets en base64, distinctes.
    data_encryption_key: SecretStr
    blind_index_key: SecretStr
    # Canaux de notification (T1.20) : facultatifs, un canal sans secret n'est pas utilisé.
    ntfy_token: SecretStr | None = None
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    alert_email_from: str | None = None
    alert_email_to: str | None = None
    healthchecks_worker_url: SecretStr | None = None
    # Enable Banking (T1.10) : identifiant de l'application et clé privée RSA (PEM).
    enable_banking_app_id: str | None = None
    enable_banking_private_key: SecretStr | None = None
    # Assistant intégré (phase B2) : clés API facultatives, fournisseur choisi sur la page Sécurité.
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    ovh_ai_api_key: SecretStr | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, file_secret_settings)


class MissingSecretError(RuntimeError):
    """Un secret attendu est absent ou invalide dans le répertoire des secrets."""


def load_secrets(secrets_dir: Path = DEFAULT_SECRETS_DIR) -> Secrets:
    if not secrets_dir.is_dir():
        raise MissingSecretError(f"répertoire des secrets introuvable : {secrets_dir}")
    try:
        return Secrets(_secrets_dir=secrets_dir)
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_input=False):
            name = ".".join(str(part) for part in error["loc"])
            problems.append(f"{secrets_dir / name} : {error['msg']}")
        raise MissingSecretError("secrets invalides : " + " ; ".join(problems)) from None
