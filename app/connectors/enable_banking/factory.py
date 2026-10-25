"""Construction du client Enable Banking à partir des secrets (worker et interface web)."""

from app.config import Secrets
from app.connectors.enable_banking.client import Credentials, EnableBankingClient
from app.connectors.requester import BankRequester
from app.net.client import make_client


def enable_banking_client(secrets: Secrets) -> EnableBankingClient | None:
    """None tant que l'application Enable Banking n'est pas configurée (secrets absents)."""
    if secrets.enable_banking_app_id is None or secrets.enable_banking_private_key is None:
        return None
    return EnableBankingClient(
        BankRequester(make_client()),
        Credentials(
            application_id=secrets.enable_banking_app_id,
            private_key_pem=secrets.enable_banking_private_key.get_secret_value(),
        ),
    )
