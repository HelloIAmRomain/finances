"""Réglages › État du système : taille de la base, dernières exécutions des tâches automatiques,
et journal des modifications (optimisations du 07/10/2026)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from app.models import Account
from app.models.ops import JobRun
from app.ops.journal import DOMAINS, PAGE_SIZE, entries
from app.ops.maintenance import database_size
from app.web.deps import CurrentUser, current_user, engine_of, templates_of

router = APIRouter()
User = Annotated[CurrentUser, Depends(current_user)]

JOB_LABELS = {
    "worker.heartbeat": "Signal de vie (surveillance du Pi)",
    "alerts.dispatch": "Envoi des notifications",
    "ops.checks": "Contrôles techniques",
    "ops.integrity": "Vérification de la base",
    "ops.maintenance": "Entretien de la base",
    "bank.sync": "Mises à jour des comptes bancaires",
    "rental.run": "Rapprochement des loyers",
    "deadlines.match": "Échéances réglées reconnues",
    "deadlines.remind": "Rappels d'échéances",
    "budgets.check": "Suivi des budgets",
    "recurring.check": "Opérations régulières",
    "categorize.ai": "Catégorisation par l'IA",
    "alerts.digest": "Bilan de la semaine",
    "forecast.check": "Prévision de trésorerie",
}
STATUS_LABELS = {
    "success": "réussie",
    "failed": "en échec",
    "running": "en cours",
    "skipped": "non lancée",
}
STATUS_STATES = {"success": "paye", "failed": "impaye", "running": "attendu", "skipped": "attendu"}
BANK_PREFIX = "bank.sync."


def _size_label(size: int) -> str:
    megabytes = size * 10 // (1024 * 1024)
    return f"{megabytes // 10},{megabytes % 10} Mo"


@router.get("/systeme", response_class=HTMLResponse)
def system_page(request: Request, user: User) -> Response:
    engine = engine_of(request)
    with engine.connect() as connection:
        latest = select(func.max(JobRun.id)).group_by(JobRun.job_name)
        runs = connection.execute(
            select(JobRun).where(JobRun.id.in_(latest)).order_by(JobRun.job_name)
        ).all()
        accounts = dict(connection.execute(select(Account.id, Account.name)).all())
    jobs = []
    for run in runs:
        name = run.job_name
        if name.startswith(BANK_PREFIX):
            account = name.removeprefix(BANK_PREFIX)
            label = f"Mise à jour du compte {accounts.get(int(account), account)}"
        else:
            label = JOB_LABELS.get(name, name)
        jobs.append(
            {
                "label": label,
                "status": STATUS_LABELS.get(str(run.status), str(run.status)),
                "state": STATUS_STATES.get(str(run.status), "attendu"),
                "at": run.finished_at or run.started_at,
            }
        )
    maintenance = next((run for run in runs if run.job_name == "ops.maintenance"), None)
    return templates_of(request).TemplateResponse(
        request,
        "system.html",
        {
            "user": user,
            "size": _size_label(database_size(engine)),
            "maintenance": maintenance,
            "jobs": sorted(jobs, key=lambda job: job["label"]),
            "failed": sum(1 for job in jobs if job["state"] == "impaye"),
        },
    )


@router.get("/journal", response_class=HTMLResponse)
def journal_page(request: Request, user: User) -> Response:
    domain = request.query_params.get("domaine") or None
    before = request.query_params.get("avant", "")
    rows = entries(
        engine_of(request),
        domain=domain if domain in DOMAINS else None,
        before_id=int(before) if before.isdigit() else None,
    )
    return templates_of(request).TemplateResponse(
        request,
        "journal.html",
        {
            "user": user,
            "rows": rows,
            "domains": {key: label for key, (label, _) in DOMAINS.items()},
            "domain": domain if domain in DOMAINS else "",
            "next_before": rows[-1].id if len(rows) == PAGE_SIZE else None,
        },
    )
