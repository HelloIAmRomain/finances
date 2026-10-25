"""Application web (fabrique) : `uvicorn --factory app.web.main:create_app`."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine

from app.alerts.channels import Sender
from app.alerts.dispatch import build_senders
from app.assistant.runs import RunRegistry
from app.config import DEFAULT_SECRETS_DIR, Secrets, Settings, load_secrets
from app.connectors.bank_profiles import configure_profiles
from app.connectors.enable_banking.client import EnableBankingClient
from app.connectors.enable_banking.factory import enable_banking_client
from app.crypto import Keyring, configure_keyring
from app.db import make_engine
from app.models.alerts import Channel
from app.ops.startup_guard import check_startup
from app.timeutil import utcnow
from app.web import api as api_v1
from app.web.cache import DataCache
from app.web.deps import LoginRequiredError
from app.web.routes import (
    accounts,
    ai,
    alerts,
    auth,
    budgets,
    connections,
    dashboard,
    deadlines,
    devices,
    forecast,
    imports,
    mcp,
    properties,
    receipts,
    rental,
    rental_deposit,
    rental_irl,
    review,
    rules,
    security,
    subscriptions,
    system,
    transactions,
    wealth,
)
from app.web.security import BodySizeLimit, security_headers
from app.web.templating import make_templates

STATIC_DIR = Path(__file__).parent / "static"
# Toutes les pages et API ; tests/web/test_route_security.py vérifie la protection de chacune.
ROUTERS = (
    auth.router,
    accounts.router,
    imports.router,
    transactions.router,
    rules.router,
    review.router,
    alerts.router,
    dashboard.router,
    connections.router,
    rental.router,
    rental_deposit.router,
    rental_irl.router,
    properties.router,
    receipts.router,
    deadlines.router,
    budgets.router,
    subscriptions.router,
    forecast.router,
    wealth.router,
    system.router,
    devices.router,
    security.router,
    api_v1.router,
    mcp.router,
    ai.router,
)


def create_app(
    *,
    settings: Settings | None = None,
    secrets: Secrets | None = None,
    engine: Engine | None = None,
    now: Callable[[], datetime] = utcnow,
    bank: EnableBankingClient | None = None,
    runs: RunRegistry | None = None,
    senders: dict[Channel, Sender] | None = None,
) -> FastAPI:
    """`bank` et `senders` servent aux tests ; sinon ils sont construits depuis les secrets."""
    settings = settings or Settings()
    if secrets is None:
        check_startup(DEFAULT_SECRETS_DIR)  # inv. 1
        secrets = load_secrets(DEFAULT_SECRETS_DIR)
    configure_keyring(Keyring.from_secrets(secrets))
    configure_profiles(settings.bank_budgets)

    # Documentation interactive désactivée : surface d'attaque inutile pour un seul utilisateur.
    registry = runs or RunRegistry()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        registry.shutdown()  # réponses en cours abandonnées : signalées dans l'historique

    api = FastAPI(
        title="Finances", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    api.state.assistant_runs = registry
    api.state.settings = settings
    api.state.secrets = secrets
    api.state.engine = engine or make_engine(settings.database_path)
    api.state.now = now
    api.state.bank = bank if bank is not None else enable_banking_client(secrets)
    api.state.senders = senders if senders is not None else build_senders(settings, secrets)
    templates = make_templates()
    api.state.templates = templates
    api.state.page_cache = DataCache()
    api.add_middleware(BodySizeLimit)  # à l'intérieur : son refus porte les en-têtes ci-dessous
    api.middleware("http")(security_headers)
    api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for router in ROUTERS:
        api.include_router(router)

    @api.exception_handler(LoginRequiredError)
    def login_required(request: Request, exc: LoginRequiredError) -> Response:
        if request.headers.get("HX-Request"):
            return Response(status_code=401, headers={"HX-Redirect": "/connexion"})
        return RedirectResponse(f"/connexion?suivant={quote(exc.next_path)}", status_code=303)

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json"
        )

    @api.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        # Servi à la racine pour couvrir tout le site (portée « / »).
        return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript")

    return api
