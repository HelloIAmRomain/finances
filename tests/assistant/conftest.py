"""Base de démonstration des outils de l'IA : noms, références et IBAN inventés (inv. 5)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, insert, select, text, update

from app.assistant.access import ENABLED, WRITES, authenticate, create_token, set_switch
from app.assistant.terms import load_pseudonymizer
from app.assistant.tools import Context
from app.auth.users import create_user
from app.categorize.seed import seed_categories
from app.models import Account, AppUser, Category, LedgerTransaction
from app.models.rental import LeaseKind, PartyRole
from app.rental.engine import run_rental
from app.rental.leases import LeaseTerms, add_party, create_lease, create_property

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
PASSWORD = "phrase-de-passe-des-tests-ia"  # noqa: S105 (factice)
TENANT = "Alice Martinez"


@dataclass
class Demo:
    engine: Engine
    user_id: int
    lease_id: int
    tx: dict[str, int]

    def category(self, name: str) -> int:
        with self.engine.connect() as connection:
            return int(connection.scalar(select(Category.id).where(Category.name == name)) or 0)


def add_tx(engine: Engine, account_id: int, day: date, amount: int, label: str) -> int:
    with engine.begin() as connection:
        next_id = int(
            connection.scalar(text("SELECT coalesce(max(id), 0) + 1 FROM ledger_transaction")) or 1
        )
        connection.execute(
            insert(LedgerTransaction).values(
                id=next_id,
                account_id=account_id,
                dedupe_key=f"ext:demo:{next_id}",
                fingerprint="x",
                occurrence=1,
                booking_date=day,
                amount_cents=amount,
                currency="EUR",
                label_raw=label,
                label_clean=label.upper(),
                created_at=NOW,
            )
        )
    return next_id


@pytest.fixture
def demo(engine: Engine) -> Demo:
    return build_demo(engine)


def build_demo(engine: Engine) -> Demo:
    """Compte 1 courant compté, 2 joint en consultation seule, 3 carte, 4 ignoré."""
    with engine.begin() as connection:
        connection.execute(update(Account).where(Account.id == 1).values(name="Courant Bourso"))
        connection.execute(
            update(Account)
            .where(Account.id == 2)
            .values(name="Joint Alice", include_in_net_worth=False)
        )
        connection.execute(
            insert(Account).values(
                id=3,
                name="Carte",
                type="card",
                currency="EUR",
                include_in_net_worth=False,
                syncs_per_day=2,
            )
        )
        connection.execute(
            insert(Account).values(
                id=4,
                name="Gestion pilotée",
                type="other",
                currency="EUR",
                include_in_net_worth=True,
                syncs_per_day=2,
                ignored_at=NOW,
            )
        )
    seed_categories(engine)
    create_user(engine, username="admin", password=PASSWORD, now=NOW)
    with engine.connect() as connection:
        user_id = int(connection.scalar(select(AppUser.id)) or 0)
    tx = {
        "salaire_aout": add_tx(
            engine, 1, date(2026, 8, 28), 250000, "VIR SEPA ACME INDUSTRIES SALAIRE AOUT"
        ),
        "courses_aout": add_tx(
            engine, 1, date(2026, 8, 5), -8250, "CARTE 05/08/26 CARREFOUR CITY CB*4521"
        ),
        "uber_aout": add_tx(
            engine, 1, date(2026, 8, 12), -2390, "CARTE 12/08/26 UBER EATS CB*4521"
        ),
        "uber_sept": add_tx(engine, 1, date(2026, 9, 3), -1850, "CARTE 03/09/26 UBER EATS CB*4521"),
        "courses_sept": add_tx(
            engine, 1, date(2026, 9, 6), -9120, "CARTE 06/09/26 CARREFOUR CITY CB*4521"
        ),
        "netflix_juil": add_tx(engine, 1, date(2026, 7, 15), -1399, "PRLV SEPA NETFLIX ECH/150726"),
        "netflix_aout": add_tx(engine, 1, date(2026, 8, 15), -1399, "PRLV SEPA NETFLIX ECH/150826"),
        "netflix_sept": add_tx(engine, 1, date(2026, 9, 15), -1399, "PRLV SEPA NETFLIX ECH/150926"),
        "loyer_sept": add_tx(
            engine, 1, date(2026, 9, 3), 80000, "VIR INST MME ALICE MARTINEZ LOYER SEPT"
        ),
        "ami": add_tx(engine, 1, date(2026, 9, 8), -3000, "VIR INST M HUGO BLANCHARD RESTO"),
        "ami_rembourse": add_tx(
            engine, 1, date(2026, 9, 10), 3000, "VIR INST M HUGO BLANCHARD REMBT"
        ),
        "joint": add_tx(engine, 2, date(2026, 9, 9), -4500, "CARTE 09/09/26 MONOPRIX CB*7788"),
        "carte": add_tx(engine, 3, date(2026, 9, 6), -9120, "CARREFOUR CITY"),
        "ignore": add_tx(engine, 4, date(2026, 9, 1), -100000, "ACHAT FONDS SECRET"),
    }
    bien = create_property(
        engine,
        name="Studio Martinez",
        address=None,
        furnished=True,
        shares=[("moi", 5000), ("indivisaire", 5000)],
        now=NOW,
    )
    lease_id = create_lease(
        engine,
        LeaseTerms(
            kind=LeaseKind.RENT,
            label="Bail Martinez",
            property_id=bien,
            account_id=1,
            start_date=date(2026, 9, 1),
            rent_cents=80000,
            charges_cents=5000,
        ),
        now=NOW,
    )
    add_party(engine, lease_id, role=PartyRole.TENANT, name=TENANT, patterns=["ALICE MARTINEZ"])
    run_rental(engine, now=NOW)
    return Demo(engine, user_id, lease_id, tx)


@pytest.fixture
def context(demo: Demo) -> Context:
    """Contexte d'appel d'un jeton lecture + écriture, interrupteurs activés."""
    token = create_token(
        demo.engine, user_id=demo.user_id, password=PASSWORD, name="PC", can_write=True, now=NOW
    )
    for key in (ENABLED, WRITES):
        set_switch(demo.engine, key, True, actor_user_id=demo.user_id, now=NOW)
    info = authenticate(demo.engine, token, now=NOW)
    assert info is not None
    return Context(
        engine=demo.engine,
        token=info,
        now=NOW,
        masker=load_pseudonymizer(demo.engine),
        switches={ENABLED: True, WRITES: True},
    )
