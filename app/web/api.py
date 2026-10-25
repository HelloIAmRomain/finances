"""API JSON v1 de l'application Android (UI-01, UI-02) : lecture, et les quelques décisions utiles
en mobilité. Montants en centimes entiers (inv. 2), dates ISO, instants en UTC (inv. 9).

- Authentification par jeton d'appareil (`Authorization: Bearer …`), obtenu par appairage :
  aucun cookie, donc pas de CSRF. Un jeton révoqué est refusé immédiatement.
- Aucun endpoint ne déplace d'argent (inv. 1) ; toute écriture passe par les mêmes services
  (audités) que l'interface web.
- Tailscale seulement (inv. 7), comme le reste de l'application.
"""

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.alerts.engine import (
    RESOLVED_SHOWN,
    AlertTransitionError,
    current_alerts,
    mark_seen,
    recently_resolved,
    resolve,
)
from app.assistant.chat import (
    AssistantError,
    conversation,
    conversations,
    delete,
    history,
    pin,
    rename,
    send,
)
from app.assistant.runs import RunBusyError
from app.auth.devices import PairingError, device_user, pair
from app.categorize.service import CategorizationError, categorize_manually
from app.connectors.budget import ManualSyncRefusedError, check_manual_sync, manual_sync_blocker
from app.connectors.enable_banking.connector import EnableBankingConnector
from app.ledger.accounts import accounts_overview
from app.ledger.search import TransactionFilters, category_choices, search
from app.ledger.sync import SyncDeferred, sync_account
from app.models import LedgerTransaction
from app.models.ledger import ConnectionStatus
from app.months import month_of
from app.overview import month_overview
from app.rental.allocations import AllocationError, confirm_proposal, reject_proposal
from app.rental.engine import run_rental
from app.rental.leases import LeaseChanges, LeaseError, LeaseNotFoundError, update_lease
from app.rental.views import lease_views, pending_overpayments, pending_proposals
from app.timeutil import paris_date
from app.web.assistant_turns import start_turn, turn_payload
from app.web.cache import DataCache
from app.web.deps import engine_of, now_of
from app.web.routes.connections import BANK_ERRORS, bank_of

router = APIRouter(prefix="/api/v1")
API_VERSION = 1
PAGE_SIZE = 50


class Me(BaseModel):
    appareil: int
    version: int


def device(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> tuple[int, int]:
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
    found = device_user(engine_of(request), token, now=now_of(request))
    if found is None:
        raise HTTPException(
            status_code=401,
            detail="Appareil non appairé ou révoqué.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return found


Device = Annotated[tuple[int, int], Depends(device)]


# --- Appairage --------------------------------------------------------------------------------


class PairingRequest(BaseModel):
    code: str
    nom: str


class PairingResponse(BaseModel):
    jeton: str
    appareil: int


@router.post("/appairage")
def pairing(request: Request, body: PairingRequest) -> PairingResponse:
    try:
        paired = pair(engine_of(request), code=body.code, device_name=body.nom, now=now_of(request))
    except PairingError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return PairingResponse(jeton=paired.token, appareil=paired.device_id)


@router.get("/moi")
def me(found: Device) -> Me:
    return Me(appareil=found[0], version=API_VERSION)


# --- Tableau de bord --------------------------------------------------------------------------


class MonthFlowOut(BaseModel):
    mois: str
    recettes: int
    depenses: int


class RentOut(BaseModel):
    bail: str
    etat: str
    reste_du: int


class DashboardOut(BaseModel):
    mois: str
    patrimoine: int
    comptes_sans_solde: list[str]
    recettes: int
    depenses: int
    solde: int
    taux_epargne_pour_mille: int | None
    recettes_n_1: int
    depenses_n_1: int
    categories: list[tuple[str, int]]
    flux_12_mois: list[MonthFlowOut]
    loyers: list[RentOut]


@router.get("/tableau-de-bord")
def dashboard(
    request: Request,
    found: Device,
    mois: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
) -> DashboardOut:
    engine, now = engine_of(request), now_of(request)
    today = paris_date(now)
    month = mois or month_of(today)
    cache: DataCache = request.app.state.page_cache
    overview = cache.get(
        engine, ("overview", month, today), lambda: month_overview(engine, month, today), now=now
    )
    current, previous = overview.current, overview.previous_year
    return DashboardOut(
        mois=month,
        patrimoine=overview.worth.total,
        comptes_sans_solde=overview.worth.accounts_without_balance,
        recettes=current.income,
        depenses=current.expenses,
        solde=current.net,
        taux_epargne_pour_mille=current.savings_rate_permille,
        recettes_n_1=previous.income,
        depenses_n_1=previous.expenses,
        categories=overview.categories,
        flux_12_mois=[
            MonthFlowOut(mois=f.month, recettes=f.income, depenses=f.expenses)
            for f in overview.flows
        ],
        loyers=[
            RentOut(bail=r.lease, etat=str(r.state), reste_du=r.remaining_cents)
            for r in overview.rents
        ],
    )


# --- Comptes ----------------------------------------------------------------------------------


class AccountOut(BaseModel):
    id: int
    nom: str
    type: str
    banque: str | None
    connexion: str | None
    consentement_jusqu_au: datetime | None
    derniere_synchro: datetime | None
    derniere_erreur: str | None
    solde: int | None
    solde_type: str | None
    solde_date: date | None
    compte: bool
    synchros_par_jour: int
    synchro_manuelle_impossible: str | None


@router.get("/comptes")
def accounts(request: Request, found: Device) -> list[AccountOut]:
    engine, now = engine_of(request), now_of(request)
    result = []
    for a in accounts_overview(engine):
        if a.ignored:
            continue
        blocker = None
        if a.connection_status is ConnectionStatus.ACTIVE:
            refusal = manual_sync_blocker(engine, account_id=a.id, now=now)
            blocker = str(refusal) if refusal else None
        else:
            blocker = "compte non relié à une banque active"
        result.append(
            AccountOut(
                id=a.id,
                nom=a.name,
                type=str(a.type),
                banque=a.institution,
                connexion=str(a.connection_status) if a.connection_status else None,
                consentement_jusqu_au=a.consent_expires_at,
                derniere_synchro=a.last_success_at,
                derniere_erreur=a.last_error,
                solde=a.balance_cents,
                solde_type=str(a.balance_type) if a.balance_type else None,
                solde_date=a.balance_date,
                compte=a.counted,
                synchros_par_jour=a.syncs_per_day,
                synchro_manuelle_impossible=blocker,
            )
        )
    return result


class MessageOut(BaseModel):
    message: str


@router.post("/comptes/{account_id}/synchroniser")
def sync(request: Request, account_id: int, found: Device) -> MessageOut:
    engine, now, bank = engine_of(request), now_of(request), bank_of(request)
    if bank is None:
        raise HTTPException(
            status_code=503, detail="Liaison bancaire non configurée sur le serveur."
        )
    try:
        check_manual_sync(engine, account_id=account_id, now=now)
        result = sync_account(
            engine, EnableBankingConnector(bank), account_id, unattended=False, now=now
        )
    except ManualSyncRefusedError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except BANK_ERRORS as exc:
        raise HTTPException(status_code=502, detail=f"Échec : {exc}") from exc
    if isinstance(result, SyncDeferred):
        return MessageOut(message=f"Mise à jour {result.reason}")
    run_rental(engine, now=now)
    return MessageOut(message="Mise à jour terminée")


# --- Transactions -----------------------------------------------------------------------------


class TransactionOut(BaseModel):
    id: int
    date: date
    compte: str
    libelle: str
    montant: int
    categories: list[str]
    virement_interne: bool


class TransactionsOut(BaseModel):
    total: int
    page: int
    transactions: list[TransactionOut]


@router.get("/transactions")
def transactions(
    request: Request,
    found: Device,
    *,
    texte: str | None = None,
    compte: int | None = None,
    du: date | None = None,
    au: date | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> TransactionsOut:
    filters = TransactionFilters(account_id=compte, date_from=du, date_to=au, text=texte or None)
    rows, total = search(
        engine_of(request), filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
    )
    return TransactionsOut(
        total=total,
        page=page,
        transactions=[
            TransactionOut(
                id=r.id,
                date=r.booking_date,
                compte=r.account_name,
                libelle=r.label,
                montant=r.amount_cents,
                categories=r.categories,
                virement_interne=r.is_transfer,
            )
            for r in rows
        ],
    )


class CategoryOut(BaseModel):
    id: int
    libelle: str


@router.get("/categories")
def categories(request: Request, found: Device) -> list[CategoryOut]:
    return [CategoryOut(id=i, libelle=label) for i, label in category_choices(engine_of(request))]


class CategorizeIn(BaseModel):
    categorie: int


@router.post("/transactions/{transaction_id}/categorie")
def categorize(
    request: Request, transaction_id: int, body: CategorizeIn, found: Device
) -> MessageOut:
    engine = engine_of(request)
    with engine.connect() as connection:
        amount = connection.scalar(
            select(LedgerTransaction.amount_cents).where(LedgerTransaction.id == transaction_id)
        )
    if amount is None:
        raise HTTPException(status_code=404, detail="Transaction introuvable.")
    try:
        categorize_manually(
            engine,
            transaction_id,
            [(body.categorie, int(amount))],
            actor_user_id=found[1],
            now=now_of(request),
        )
    except CategorizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return MessageOut(message="Catégorie enregistrée")


# --- Loyers -----------------------------------------------------------------------------------


class DueOut(BaseModel):
    id: int
    mois: str
    montant: int
    recu: int
    reste_du: int
    etat: str


class LeaseOut(BaseModel):
    id: int
    type: str
    nom: str
    bien: str | None
    compte: str
    mensuel: int
    jour_alerte: int
    jour_impaye: int
    echeances: list[DueOut]


class PendingOut(BaseModel):
    id: int
    bail: str
    date: date
    libelle: str
    montant: int
    mois: str | None = None


class RentalOut(BaseModel):
    baux: list[LeaseOut]
    a_valider: list[PendingOut]
    trop_percus: list[PendingOut]


@router.get("/loyers")
def rental(request: Request, found: Device) -> RentalOut:
    engine, today = engine_of(request), paris_date(now_of(request))
    return RentalOut(
        baux=[
            LeaseOut(
                id=v.id,
                type=str(v.kind),
                nom=v.label,
                bien=v.property_name,
                compte=v.account_name,
                mensuel=v.monthly_cents,
                jour_alerte=v.alert_day,
                jour_impaye=v.unpaid_day,
                echeances=[
                    DueOut(
                        id=d.id,
                        mois=d.period,
                        montant=d.amount_cents,
                        recu=d.paid_cents,
                        reste_du=d.remaining_cents,
                        etat=str(d.state),
                    )
                    for d in reversed(v.dues)
                ],
            )
            for v in lease_views(engine, today)
        ],
        a_valider=[
            PendingOut(
                id=p["id"],
                bail=p["lease"],
                date=p["booking_date"],
                libelle=p["label"],
                montant=p["amount_cents"],
                mois=p["period"],
            )
            for p in pending_proposals(engine)
        ],
        trop_percus=[
            PendingOut(
                id=o["id"],
                bail=o["lease"],
                date=o["booking_date"],
                libelle=o["label"],
                montant=o["amount_cents"],
            )
            for o in pending_overpayments(engine)
        ],
    )


@router.post("/loyers/propositions/{allocation_id}/{decision}")
def decide(
    request: Request, allocation_id: int, decision: Literal["confirmer", "refuser"], found: Device
) -> MessageOut:
    engine, now = engine_of(request), now_of(request)
    try:
        if decision == "confirmer":
            confirm_proposal(engine, allocation_id, actor_user_id=found[1], now=now)
        else:
            reject_proposal(engine, allocation_id, actor_user_id=found[1], now=now)
    except AllocationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    run_rental(engine, now=now)
    return MessageOut(
        message="Proposition confirmée" if decision == "confirmer" else "Proposition refusée"
    )


class LeaseRenameIn(BaseModel):
    nom: str = Field(max_length=128)


@router.post("/loyers/{lease_id}/renommer")
def rename_lease(request: Request, lease_id: int, body: LeaseRenameIn, found: Device) -> MessageOut:
    """Renommer un loyer ou un remboursement depuis l'application (widget, liste des loyers)."""
    name = " ".join(body.nom.split())
    if not name:
        raise HTTPException(
            status_code=422, detail="Nom vide : écrivez par exemple « Studio Lyon »."
        )
    try:
        update_lease(
            engine_of(request),
            lease_id,
            LeaseChanges(label=name),
            actor_user_id=found[1],
            now=now_of(request),
        )
    except LeaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LeaseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return MessageOut(message=f"Renommé en « {name} »")


# --- Alertes ----------------------------------------------------------------------------------


class AlertOut(BaseModel):
    id: int
    type: str
    gravite: str
    etat: str
    titre: str
    ouverte_le: datetime


@router.get("/alertes")
def alerts(request: Request, found: Device, toutes: bool = False) -> list[AlertOut]:
    """Alertes en cours, les plus graves d'abord ; avec `toutes`, suivies des dernières résolues."""
    engine = engine_of(request)
    rows = current_alerts(engine) + (recently_resolved(engine, RESOLVED_SHOWN) if toutes else [])
    return [
        AlertOut(
            id=a.id,
            type=a.type,
            gravite=str(a.severity),
            etat=str(a.status),
            titre=a.title,
            ouverte_le=a.opened_at,
        )
        for a in rows
    ]


@router.post("/alertes/{alert_id}/{action}")
def alert_action(
    request: Request, alert_id: int, action: Literal["vue", "resoudre"], found: Device
) -> MessageOut:
    engine, now = engine_of(request), now_of(request)
    try:
        (mark_seen if action == "vue" else resolve)(engine, alert_id, now=now)
    except AlertTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return MessageOut(message="Alerte vue" if action == "vue" else "Alerte résolue")


# --- Assistant (phase B2) ---------------------------------------------------------------------


class AssistantIn(BaseModel):
    texte: str
    conversation: int | None = None


class AssistantOut(BaseModel):
    conversation: int
    titre: str
    reponse: str
    outils: list[str]
    modifications: list[str]


class ConversationOut(BaseModel):
    id: int
    titre: str
    mise_a_jour: datetime
    epinglee: bool  # en tête de liste


class ExchangeOut(BaseModel):
    role: Literal["user", "assistant"]
    texte: str
    outils: list[str]


class ConversationDetailOut(BaseModel):
    id: int
    titre: str
    epinglee: bool
    messages: list[ExchangeOut]


class RenameIn(BaseModel):
    titre: str


class PinIn(BaseModel):
    epinglee: bool


@router.post("/assistant")
def assistant(request: Request, body: AssistantIn, found: Device) -> AssistantOut:
    """Question en langage naturel (texte tapé ou dicté) ; l'assistant peut appeler les outils
    de lecture et, si l'interrupteur le permet, d'écriture (annulables)."""
    try:
        reply = send(
            engine_of(request),
            request.app.state.secrets,
            user_id=found[1],
            conversation_id=body.conversation,
            text=body.texte,
            now=now_of(request),
            transport=getattr(request.app.state, "ai_transport", None),
        )
    except AssistantError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    title = conversation(engine_of(request), found[1], reply.conversation_id).title
    return AssistantOut(
        conversation=reply.conversation_id,
        titre=title,
        reponse=reply.text,
        outils=reply.tools,
        modifications=reply.changes,
    )


@router.get("/assistant/conversations")
def assistant_conversations(request: Request, found: Device) -> list[ConversationOut]:
    return [
        ConversationOut(
            id=c.id,
            titre=c.title,
            mise_a_jour=c.updated_at,
            epinglee=c.pinned,
        )
        for c in conversations(engine_of(request), found[1])
    ]


def _conversation(request: Request, user_id: int, conversation_id: int) -> ConversationDetailOut:
    engine = engine_of(request)
    try:
        exchanges = history(engine, user_id, conversation_id)
        row = conversation(engine, user_id, conversation_id)
    except AssistantError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ConversationDetailOut(
        id=conversation_id,
        titre=row.title,
        epinglee=row.pinned,
        messages=[
            ExchangeOut(role=e["role"], texte=e["text"], outils=e.get("tools", []))
            for e in exchanges
        ],
    )


@router.get("/assistant/conversations/{conversation_id}")
def assistant_conversation(
    request: Request, conversation_id: int, found: Device
) -> ConversationDetailOut:
    return _conversation(request, found[1], conversation_id)


@router.post("/assistant/conversations/{conversation_id}/renommer")
def assistant_rename(
    request: Request, conversation_id: int, body: RenameIn, found: Device
) -> ConversationDetailOut:
    try:
        rename(engine_of(request), found[1], conversation_id, body.titre, now=now_of(request))
    except AssistantError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _conversation(request, found[1], conversation_id)


@router.post("/assistant/conversations/{conversation_id}/epingler")
def assistant_pin(
    request: Request, conversation_id: int, body: PinIn, found: Device
) -> ConversationDetailOut:
    """Épingle (en tête de liste) ou désépingle une conversation."""
    try:
        pin(engine_of(request), found[1], conversation_id, body.epinglee, now=now_of(request))
    except AssistantError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _conversation(request, found[1], conversation_id)


@router.delete("/assistant/conversations/{conversation_id}")
def assistant_delete(request: Request, conversation_id: int, found: Device) -> MessageOut:
    try:
        delete(engine_of(request), found[1], conversation_id, now=now_of(request))
    except AssistantError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return MessageOut(message="Conversation supprimée")


# --- Assistant asynchrone : la question part tout de suite, la réponse s'écrit en direct -----


class TurnStartOut(BaseModel):
    tour: str
    conversation: int
    titre: str


class TurnEventOut(BaseModel):
    n: int
    type: Literal["etape", "fin", "erreur"]
    texte: str


class TurnOut(BaseModel):
    tour: str
    conversation: int
    etat: Literal["en_cours", "termine", "erreur"]
    evenements: list[TurnEventOut]
    texte_partiel: str
    reponse: str | None
    outils: list[str]
    modifications: list[str]
    erreur: str | None


@router.post("/assistant/messages", status_code=202)
def assistant_message(request: Request, body: AssistantIn, found: Device) -> TurnStartOut:
    try:
        run_id, conversation, title = start_turn(
            request, user_id=found[1], conversation_id=body.conversation, text=body.texte
        )
    except RunBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AssistantError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TurnStartOut(tour=run_id, conversation=conversation, titre=title)


@router.get("/assistant/tours/{run_id}")
def assistant_turn(request: Request, run_id: str, found: Device, depuis: int = 0) -> TurnOut:
    view = request.app.state.assistant_runs.view(run_id, found[1], since=depuis)
    if view is None:
        raise HTTPException(status_code=404, detail="Réponse introuvable ou expirée.")
    return TurnOut.model_validate(turn_payload(view))
