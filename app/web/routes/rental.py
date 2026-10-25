"""Pages « Loyers » : baux et encaissements attendus, échéances, rapprochement manuel, décisions
(LOC-01 à LOC-06, LOC-14, LOC-15, Q17). Chaque modification relance le moteur, pour que états et
alertes soient à jour tout de suite."""

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from app.ledger.views import visible_accounts
from app.models import Account, Lease, Property
from app.models.rental import LeaseKind, OverpaymentDecision, PartyRole
from app.months import is_month, month_start
from app.rental.allocations import (
    AllocationError,
    allocate_manually,
    confirm_proposal,
    decide_overpayment,
    dissociate,
    reject_proposal,
)
from app.rental.deposit import KIND_LABELS as DEPOSIT_KIND_LABELS
from app.rental.deposit import STATUS_LABELS as DEPOSIT_STATUS_LABELS
from app.rental.deposit import candidates, deposit_state
from app.rental.engine import run_rental
from app.rental.irl import indices, proposal, publication_hint, quarter_label
from app.rental.leases import (
    LeaseChanges,
    LeaseError,
    LeaseTerms,
    NewParty,
    add_party,
    create_lease_with_parties,
    end_lease,
    lease_account,
    untracked_dues,
    update_lease,
)
from app.rental.receipts import missing, missing_message
from app.rental.views import (
    CreditSearch,
    lease_views,
    pending_overpayments,
    pending_proposals,
    search_credits,
)
from app.tax.calendar import anniversary
from app.timeutil import paris_date
from app.web.deps import (
    CurrentUser,
    engine_of,
    now_of,
    redirect_with_error,
    redirect_with_message,
    templates_of,
)
from app.web.routes.rental_shared import User, amount_or_error, page_context, refresh_rental

router = APIRouter()


@router.get("/loyers", response_class=HTMLResponse)
def rental_page(request: Request, user: User) -> Response:
    engine, today = engine_of(request), paris_date(now_of(request))
    return templates_of(request).TemplateResponse(
        request,
        "rental.html",
        {
            **page_context(request, user),
            "leases": lease_views(engine, today),
            "proposals": pending_proposals(engine),
            "overpayments": pending_overpayments(engine),
        },
    )


@router.post("/loyers/actualiser")
def refresh(request: Request, user: User) -> Response:
    stats = run_rental(engine_of(request), now=now_of(request))
    return redirect_with_message("/loyers", f"à jour : {stats.confirmed} paiement(s) rapproché(s)")


def _new_form(request: Request, user: CurrentUser, error: str | None, status: int) -> Response:
    engine = engine_of(request)
    with engine.connect() as connection:
        accounts = connection.execute(
            select(Account.id, Account.name)
            .where(Account.id.in_(visible_accounts()), Account.closed_at.is_(None))
            .order_by(Account.name)
        ).all()
        properties = connection.execute(
            select(Property.id, Property.name)
            .where(Property.archived_at.is_(None))
            .order_by(Property.name)
        ).all()
    return templates_of(request).TemplateResponse(
        request,
        "rental_new.html",
        {
            **page_context(request, user),
            "accounts": accounts,
            "properties": properties,
            "erreur": error,
            "today": paris_date(now_of(request)),
        },
        status_code=status,
    )


def _month_start(month: str) -> date:
    """« AAAA-MM » (champ mois du navigateur) → premier jour du mois."""
    if not is_month(month[:7]):
        raise LeaseError(f"Mois illisible : {month}")
    return month_start(month[:7])


def _patterns(text: str) -> tuple[str, ...]:
    return (text,) if text.strip() else ()


@router.get("/loyers/nouveau", response_class=HTMLResponse)
def new_lease_page(request: Request, user: User) -> Response:
    return _new_form(request, user, None, 200)


@router.post("/loyers", response_class=HTMLResponse)
def create_lease_route(  # noqa: PLR0913 (formulaire complet d'un bail)
    request: Request,
    user: User,
    *,
    type: Annotated[LeaseKind, Form()],
    nom: Annotated[str, Form()],
    compte: Annotated[int, Form()],
    debut: Annotated[date, Form()],
    montant: Annotated[str, Form()],
    charges: Annotated[str, Form()] = "",
    bien: Annotated[str, Form()] = "",
    nouveau_bien: Annotated[str, Form()] = "",
    ma_part: Annotated[int, Form()] = 50,
    jour_echeance: Annotated[int, Form()] = 1,
    jour_alerte: Annotated[int, Form()] = 5,
    jour_impaye: Annotated[int, Form()] = 10,
    depot: Annotated[str, Form()] = "",
    payeur_nom: Annotated[str, Form()] = "",
    payeur_iban: Annotated[str, Form()] = "",
    payeur_motif: Annotated[str, Form()] = "",
    caution_nom: Annotated[str, Form()] = "",
    caution_motif: Annotated[str, Form()] = "",
    suivi: Annotated[str, Form()] = "",
) -> Response:
    engine, now = engine_of(request), now_of(request)
    payer_role = PartyRole.TENANT if type is LeaseKind.RENT else PartyRole.BORROWER
    parties = [
        NewParty(payer_role, payeur_nom, payeur_iban or None, _patterns(payeur_motif)),
        *([NewParty(PartyRole.GUARANTOR, caution_nom, None, _patterns(caution_motif))]
          if caution_nom.strip() else []),
    ] if payeur_nom.strip() else []  # fmt: skip
    try:
        lease_id = create_lease_with_parties(
            engine,
            LeaseTerms(
                tracking_start=_month_start(suivi) if suivi else None,
                kind=type,
                label=nom,
                property_id=int(bien) if bien.strip() else None,
                account_id=compte,
                start_date=debut,
                rent_cents=amount_or_error(montant),
                charges_cents=amount_or_error(charges, required=False),
                due_day=jour_echeance,
                alert_day=jour_alerte,
                unpaid_day=jour_impaye,
                deposit_cents=amount_or_error(depot, required=False),
            ),
            new_property=(nouveau_bien or nom, ma_part),
            parties=parties,
            now=now,
            actor_user_id=user.id,
        )
    except LeaseError as exc:
        return _new_form(request, user, str(exc), 422)
    refresh_rental(request)
    return redirect_with_message(f"/loyers/{lease_id}", "enregistré")


@router.get("/loyers/{lease_id}", response_class=HTMLResponse)
def lease_page(request: Request, lease_id: int, user: User) -> Response:
    engine, today = engine_of(request), paris_date(now_of(request))
    views = lease_views(engine, today, lease_id)
    if not views:
        return redirect_with_message("/loyers", "bail introuvable")
    with engine.connect() as connection:
        reference = connection.scalar(
            select(Lease.irl_reference_quarter).where(Lease.id == lease_id)
        )
    gaps = missing(engine, lease_id)
    return templates_of(request).TemplateResponse(
        request,
        "rental_detail.html",
        {
            **page_context(request, user),
            "lease": views[0],
            "roles": [r for r in PartyRole],
            "irl_reference": reference,
            "irl_proposal": proposal(engine, lease_id, today),
            "irl_indices": list(indices(engine).items())[:8],
            "quarter_label": quarter_label,
            "publication_hint": publication_hint,
            "next_anniversary": anniversary(views[0].start_date, today),
            "untracked": untracked_dues(engine, lease_id),
            "receipt_gaps": gaps,
            "receipt_gaps_message": missing_message(gaps) if gaps.any else "",
            "deposit": deposit_state(engine, lease_id),
            "deposit_credits": candidates(engine, lease_id, credit=True, today=today),
            "deposit_debits": candidates(engine, lease_id, credit=False, today=today),
            "deposit_kinds": DEPOSIT_KIND_LABELS,
            "deposit_statuses": DEPOSIT_STATUS_LABELS,
            "today": today,
        },
    )


def _search_date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError as exc:
        raise LeaseError("Date illisible : choisissez-la dans le calendrier.") from exc


@router.post("/loyers/{lease_id}/affecter")
def assign(
    request: Request,
    lease_id: int,
    user: User,
    *,
    echeance: Annotated[int, Form()],
    operation: Annotated[int, Form()],
    montant: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    retour: Annotated[str, Form()] = "",
) -> Response:
    back = retour if retour.startswith(f"/loyers/{lease_id}") else f"/loyers/{lease_id}"
    try:
        amount = amount_or_error(montant) if montant.strip() else None
        allocate_manually(
            engine_of(request),
            due_id=echeance,
            transaction_id=operation,
            amount_cents=amount,
            note=note,
            actor_user_id=user.id,
            now=now_of(request),
        )
    except (AllocationError, LeaseError) as exc:
        return redirect_with_error(back.split("?")[0], str(exc))
    refresh_rental(request)
    return redirect_with_message(f"/loyers/{lease_id}", "opération affectée")


@router.post("/loyers/{lease_id}/affectations/{allocation_id}/dissocier")
def unassign(
    request: Request,
    lease_id: int,
    allocation_id: int,
    user: User,
    note: Annotated[str, Form()] = "",
) -> Response:
    try:
        dissociate(
            engine_of(request), allocation_id, note=note, actor_user_id=user.id, now=now_of(request)
        )
    except AllocationError as exc:
        return redirect_with_error(f"/loyers/{lease_id}", str(exc))
    refresh_rental(request)
    return redirect_with_message(f"/loyers/{lease_id}", "opération dissociée")


@router.post("/loyers/{lease_id}/payeurs")
def add_payer(
    request: Request,
    lease_id: int,
    user: User,
    *,
    role: Annotated[PartyRole, Form()],
    nom: Annotated[str, Form()],
    iban: Annotated[str, Form()] = "",
    motif: Annotated[str, Form()] = "",
) -> Response:
    try:
        add_party(
            engine_of(request),
            lease_id,
            role=role,
            name=nom,
            iban=iban or None,
            patterns=[motif] if motif.strip() else [],
        )
    except LeaseError as exc:
        return redirect_with_error(f"/loyers/{lease_id}", str(exc))
    refresh_rental(request)
    return redirect_with_message(f"/loyers/{lease_id}", "payeur ajouté")


@router.post("/loyers/{lease_id}/terminer")
def finish(request: Request, lease_id: int, user: User, fin: Annotated[date, Form()]) -> Response:
    try:
        end_lease(
            engine_of(request), lease_id, end_date=fin, now=now_of(request), actor_user_id=user.id
        )
    except LeaseError as exc:
        return redirect_with_error(f"/loyers/{lease_id}", str(exc))
    return redirect_with_message(f"/loyers/{lease_id}", "fin enregistrée")


@router.post("/loyers/propositions/{allocation_id}/{decision}")
def decide_proposal(request: Request, allocation_id: int, decision: str, user: User) -> Response:
    engine, now = engine_of(request), now_of(request)
    try:
        if decision == "confirmer":
            confirm_proposal(engine, allocation_id, actor_user_id=user.id, now=now)
        elif decision == "refuser":
            reject_proposal(engine, allocation_id, actor_user_id=user.id, now=now)
        else:
            raise AllocationError("Décision inconnue.")
    except AllocationError as exc:
        return redirect_with_error("/loyers", str(exc))
    refresh_rental(request)
    return redirect_with_message(
        "/loyers", "proposition confirmée" if decision == "confirmer" else "proposition refusée"
    )


@router.post("/loyers/trop-percus/{overpayment_id}/{decision}")
def decide_overpaid(request: Request, overpayment_id: int, decision: str, user: User) -> Response:
    choices = {
        "reporter": OverpaymentDecision.CARRY_FORWARD,
        "rembourser": OverpaymentDecision.REFUND,
    }
    try:
        if decision not in choices:
            raise AllocationError("Décision inconnue.")
        decide_overpayment(
            engine_of(request),
            overpayment_id,
            choices[decision],
            actor_user_id=user.id,
            now=now_of(request),
        )
    except AllocationError as exc:
        return redirect_with_error("/loyers", str(exc))
    refresh_rental(request)
    return redirect_with_message("/loyers", "trop-perçu traité")


@router.get("/loyers/{lease_id}/echeances/{due_id}/affecter", response_class=HTMLResponse)
def assign_page(request: Request, lease_id: int, due_id: int, user: User) -> Response:
    """Recherche d'un encaissement à affecter à une échéance : compte, période, libellé, montant."""
    engine, today = engine_of(request), paris_date(now_of(request))
    views = lease_views(engine, today, lease_id)
    due = next((d for d in views[0].dues if d.id == due_id), None) if views else None
    if due is None:
        return redirect_with_message("/loyers", "échéance introuvable")
    params = request.query_params
    period_start = _month_start(due.period)
    default_from = (period_start - timedelta(days=1)).replace(day=1)
    try:
        search = CreditSearch(
            account_id=None if params.get("compte") == "tous" else lease_account(engine, lease_id),
            date_from=_search_date(params.get("du")) or default_from,
            date_to=_search_date(params.get("au")),
            text=(params.get("texte") or "").strip() or None,
            amount_cents=amount_or_error(params["montant"]) if params.get("montant") else None,
        )
        error = None
    except (ValueError, LeaseError) as exc:
        search, error = CreditSearch(account_id=lease_account(engine, lease_id)), str(exc)
    return templates_of(request).TemplateResponse(
        request,
        "rental_assign.html",
        {
            **page_context(request, user),
            "lease": views[0],
            "due": due,
            "search": search,
            "all_accounts": params.get("compte") == "tous",
            "results": search_credits(engine, search),
            "erreur": error,
            "here": str(request.url.path)
            + ("?" + str(request.url.query) if request.url.query else ""),
        },
    )


@router.post("/loyers/{lease_id}/modifier")
def modify(  # noqa: PLR0913 (formulaire de modification d'un bail)
    request: Request,
    lease_id: int,
    user: User,
    *,
    nom: Annotated[str, Form()] = "",
    suivi: Annotated[str, Form()] = "",
    montant: Annotated[str, Form()] = "",
    charges: Annotated[str, Form()] = "",
    a_partir_de: Annotated[str, Form()] = "",
    jour_echeance: Annotated[int | None, Form()] = None,
    jour_alerte: Annotated[int | None, Form()] = None,
    jour_impaye: Annotated[int | None, Form()] = None,
) -> Response:
    try:
        new_amounts = bool(montant.strip() or charges.strip())
        if new_amounts and not a_partir_de:
            raise LeaseError("Indiquer le premier mois concerné par le nouveau montant.")
        removed = update_lease(
            engine_of(request),
            lease_id,
            LeaseChanges(
                label=nom or None,
                tracking_start=_month_start(suivi) if suivi else None,
                rent_cents=amount_or_error(montant) if montant.strip() else None,
                charges_cents=amount_or_error(charges, required=False) if charges.strip() else None,
                amounts_from=a_partir_de[:7] if new_amounts else None,
                due_day=jour_echeance,
                alert_day=jour_alerte,
                unpaid_day=jour_impaye,
            ),
            actor_user_id=user.id,
            now=now_of(request),
        )
    except LeaseError as exc:
        return redirect_with_error(f"/loyers/{lease_id}", str(exc))
    refresh_rental(request)
    detail = f" ; {len(removed)} échéance(s) antérieure(s) au suivi retirée(s)" if removed else ""
    return redirect_with_message(f"/loyers/{lease_id}", f"bail modifié{detail}")
