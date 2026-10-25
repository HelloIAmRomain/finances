"""Budgets de temps sur 10 ans d'historique (NF-PERF-02) : 60 000 opérations dont 70 %
catégorisées, trois baux suivis depuis 2021. Chaque page et chaque tâche planifiée récurrente
reste dans son budget (machine de CI ; le Pi est plus lent, d'où la marge)."""

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from app.categorize.budgets import check_budgets
from app.categorize.seed import seed_categories
from app.ledger.cross_dedupe import detect_duplicates
from app.ledger.forecast import check_low_balances
from app.ledger.subscriptions import check_recurring
from app.ledger.transfers import detect_transfers
from app.models import Categorization, Category, LedgerTransaction
from app.models.categorize import CategorizationSource
from app.models.rental import LeaseKind, PartyRole
from app.rental.engine import run_rental
from app.rental.leases import LeaseTerms, NewParty, create_lease_with_parties
from tests.web.conftest import Web

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
MERCHANTS = ("CB SUPERMARCHE", "CB TRAIN", "PRLV ENERGIE", "CB LIBRAIRIE", "CB CINEMA",
             "PRLV INTERNET", "CB PHARMACIE", "CB RESTAURANT", "CB ESSENCE", "CB ZOO")  # fmt: skip
PAGES = ("/", "/transactions", "/transactions?page=50", "/transactions?texte=pharmacie",
         "/loyers", "/loyers/1", "/biens/1", "/prevision", "/abonnements", "/budgets",
         "/patrimoine", "/a-valider", "/alertes", "/echeances", "/journal", "/comptes")  # fmt: skip
PAGE_BUDGET = 2.0  # secondes, première visite (sans cache)
EXPORT_BUDGET = 4.0
JOB_BUDGET = 4.0


def _history(web: Web) -> None:
    engine = web.engine
    seed_categories(engine)
    with engine.connect() as connection:
        categories = list(connection.scalars(select(Category.id)))
    start = date(2016, 10, 1)
    rows, lines = [], []
    for n in range(60_000):
        rent = n % 480 == 0
        amount = 80_000 if rent else (-(n * 37 % 9000) - 100 if n % 7 else n % 5000 + 1000)
        label = "VIR DE ALICE EXEMPLE" if rent else f"{MERCHANTS[n % 10]} {n % 97}"
        rows.append({
            "id": n + 1, "account_id": 1 + (n % 3 == 0), "dedupe_key": f"ext:perf:{n}",
            "fingerprint": "x", "occurrence": 1, "booking_date": start + timedelta(days=n // 16),
            "amount_cents": amount, "currency": "EUR", "label_raw": label, "label_clean": label,
            "created_at": NOW,
        })  # fmt: skip
        if n % 10 < 7:
            lines.append({
                "transaction_id": n + 1, "category_id": categories[n % len(categories)],
                "amount_cents": amount, "source": CategorizationSource.RULE, "created_at": NOW,
            })  # fmt: skip
    with engine.begin() as connection:
        connection.execute(insert(LedgerTransaction), rows)
        connection.execute(insert(Categorization), lines)
    for i in range(3):
        create_lease_with_parties(
            engine,
            LeaseTerms(
                kind=LeaseKind.RENT, label=f"Studio {i}", account_id=1,
                start_date=date(2021, 1, 1), rent_cents=80_000, tracking_start=date(2021, 1, 1),
            ),
            new_property=(f"Bien {i}", 50),
            parties=[NewParty(PartyRole.TENANT, "Alice Exemple", patterns=("ALICE EXEMPLE",))],
            now=NOW,
            actor_user_id=None,
        )  # fmt: skip


def _seconds(action: Callable[[], object]) -> float:
    began = time.perf_counter()
    action()
    return time.perf_counter() - began


@pytest.mark.slow
def test_pages_and_jobs_within_their_time_budget(web: Web) -> None:
    web.clock.current = NOW
    _history(web)
    engine = web.engine
    jobs: dict[str, Callable[[], object]] = {
        "rental.run": lambda: run_rental(engine, now=NOW),
        "detect_transfers": lambda: detect_transfers(engine),
        "detect_duplicates": lambda: detect_duplicates(engine, account_id=1),
        "recurring.check": lambda: check_recurring(engine, now=NOW),
        "budgets.check": lambda: check_budgets(engine, now=NOW),
        "forecast.check": lambda: check_low_balances(engine, now=NOW),
    }
    slow = {name: s for name, job in jobs.items() if (s := _seconds(job)) > JOB_BUDGET}
    web.login()
    for path in PAGES:
        began = time.perf_counter()
        response = web.client.get(path)
        seconds = time.perf_counter() - began
        assert response.status_code == 200, path
        if seconds > PAGE_BUDGET:
            slow[path] = seconds
    export = _seconds(lambda: web.client.get("/transactions/export.csv").raise_for_status())
    if export > EXPORT_BUDGET:
        slow["export CSV"] = export
    assert slow == {}
