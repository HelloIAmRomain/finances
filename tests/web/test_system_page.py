"""Réglages › État du système et journal des modifications."""

from datetime import timedelta

from sqlalchemy import insert

from app.models import Account, AuditLog
from app.models.ops import JobRun, JobStatus
from app.ops.journal import PAGE_SIZE, author_of, domain_of, label_of
from tests.web.conftest import T0, Web


def _run(web: Web, name: str, status: JobStatus, stats: dict[str, int] | None = None) -> None:
    with web.engine.begin() as connection:
        connection.execute(
            insert(JobRun).values(
                job_name=name, slot_start=T0, status=status, started_at=T0,
                finished_at=T0 + timedelta(seconds=3), attempt=1, stats=stats or {},
            )
        )  # fmt: skip


def test_system_page_lists_jobs_in_words_and_database_size(web: Web) -> None:
    web.login()
    assert "Aucune tâche exécutée" in web.client.get("/systeme").text
    with web.engine.begin() as connection:
        account = connection.execute(
            insert(Account)
            .values(
                name="Livret démo", type="savings", currency="EUR",
                include_in_net_worth=True, syncs_per_day=4,
            )
            .returning(Account.id)
        ).scalar_one()  # fmt: skip
    _run(web, "rental.run", JobStatus.SUCCESS)
    _run(web, "ops.maintenance", JobStatus.SUCCESS, {"compactee": 1})
    _run(web, f"bank.sync.{account}", JobStatus.FAILED)
    _run(web, "tache.inconnue", JobStatus.SKIPPED)
    page = web.client.get("/systeme").text
    assert "Rapprochement des loyers" in page and "réussie" in page
    assert "Mise à jour du compte Livret démo" in page and "en échec" in page
    assert "1 en échec" in page and "(base compactée)" in page
    assert " Mo</strong>" in page
    assert 'href="/systeme" aria-current="page"' in page  # Réglages › État du système


def test_journal_in_words_filtered_and_paginated(web: Web) -> None:
    web.login()  # « Connexion réussie » journalisée
    with web.engine.begin() as connection:
        connection.execute(
            insert(AuditLog),
            [
                {"at": T0, "actor_user_id": None, "action": "rent.allocated", "details": {}}
                for _ in range(PAGE_SIZE + 5)
            ],
        )
        connection.execute(
            insert(AuditLog).values(at=T0, action="mcp.call", details={"outil": "x"})
        )
    page = web.client.get("/journal").text
    assert "Lecture de vos données par l&#39;IA" in page and "l&#39;IA</td>" in page
    assert "Plus ancien →" in page
    assert 'href="/systeme" aria-current="page"' not in page  # rubrique Réglages, sans page active
    filtered = web.client.get("/journal?domaine=locations").text
    assert (
        "Paiement affecté à une échéance" in filtered and "Lecture de vos données" not in filtered
    )
    assert "domaine=locations&amp;avant=" in filtered
    empty = web.client.get("/journal?domaine=patrimoine").text
    assert "Rien dans ce domaine" in empty
    older = web.client.get("/journal?avant=3&domaine=inconnu").text
    assert "Plus ancien" not in older


def test_labels_domains_and_authors() -> None:
    assert label_of("lease.created") == "Bail ou remboursement créé"
    assert label_of("quelque.chose") == "Autre modification"
    assert domain_of("quelque.chose") == "autres"
    assert author_of("ai.change_set", 1) == "l'IA"
    assert author_of("rent.allocated", None) == "automatique"
    assert author_of("rent.allocated", 1) == "vous"
