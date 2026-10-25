"""Catégorisation automatique par IA (ANA-02) : fournisseur simulé (aucun appel réseau),
pseudonymisation, seuil de confiance, file « À valider », priorité des règles, plafond."""

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, insert, select, update

from app.assistant.access import ENABLED, set_switch
from app.assistant.pseudonym import LeakError
from app.assistant.terms import add_term
from app.auth.users import create_user
from app.categorize.ai import (
    AiCategorizationError,
    Choice,
    Recorded,
    _record,
    categorize_pending,
    parse_answer,
    pending,
    set_settings,
    settings,
)
from app.categorize.review import decide_ai_suggestion, dismiss_ai_suggestion, open_items
from app.categorize.seed import seed_categories
from app.categorize.service import apply_rules, categorize_manually, create_rule
from app.config import Secrets
from app.models import Account, AuditLog, Categorization, Category, LedgerTransaction, ReviewItem
from app.models.categorize import CategorizationSource, CategoryKind, ReviewKind, ReviewStatus
from app.models.ledger import AccountType

NOW = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
SECRETS = Secrets.model_construct(anthropic_api_key=SecretStr("cle-factice"))
_ids = iter(range(1, 1_000_000))


class FakeProvider:
    """Répond, pour chaque opération reçue, selon le libellé : `answers[libellé] = (catégorie,
    confiance)` ; mémorise les requêtes."""

    def __init__(self, answers: dict[str, tuple[str | None, Any]], status: int = 200) -> None:
        self.answers = answers
        self.status = status
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        text = body["messages"][0]["content"]
        operations = [json.loads(line) for line in text.split("Opérations :\n")[1].splitlines()]
        reply = [
            {"n": op["n"], "categorie": self.answers.get(op["libelle"], (None, 0))[0],
             "confiance": self.answers.get(op["libelle"], (None, 0))[1]}
            for op in operations
        ]  # fmt: skip
        return httpx.Response(
            self.status,
            json={
                "content": [{"type": "text", "text": "Voici : " + json.dumps(reply)}],
                "usage": {"input_tokens": 900, "output_tokens": 100},
            },
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def sent_labels(self) -> list[str]:
        text = self.bodies[-1]["messages"][0]["content"]
        return [
            json.loads(line)["libelle"] for line in text.split("Opérations :\n")[1].splitlines()
        ]


@pytest.fixture
def db(engine: Engine) -> Engine:
    seed_categories(engine)
    user_id = create_user(engine, username="moi", password="phrase-de-passe-test", now=NOW)  # noqa: S106 (factice)
    assert user_id
    set_switch(engine, ENABLED, True, actor_user_id=1, now=NOW)
    set_settings(engine, enabled=True, threshold=800, actor_user_id=1, now=NOW)
    return engine


def _tx(
    engine: Engine, label: str, amount: int, day: date = date(2026, 10, 1), account: int = 1
) -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id,
                account_id=account,
                dedupe_key=f"ext:t:{tx_id}",
                fingerprint="x",
                occurrence=1,
                booking_date=day,
                amount_cents=amount,
                currency="EUR",
                label_raw=label,
                label_clean=label,
                created_at=NOW,
            )
        )
    return tx_id


def _category(engine: Engine, tx_id: int) -> tuple[str, CategorizationSource, int | None] | None:
    with engine.connect() as connection:
        row = connection.execute(
            select(Category.name, Categorization.source, Categorization.confidence_permille)
            .join(Category, Category.id == Categorization.category_id)
            .where(Categorization.transaction_id == tx_id, Categorization.superseded_at.is_(None))
        ).first()
    return (row[0], row[1], row[2]) if row else None


def _cat(engine: Engine, name: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalars(select(Category.id).where(Category.name == name)).one())


def test_confident_answers_categorize_the_rest_waits_for_review(db: Engine) -> None:
    sure = _tx(db, "CB CARREFOUR CITY", -2350)
    unsure = _tx(db, "CB BAZAR DU COIN", -1500)
    unknown = _tx(db, "PRLV SEPA XYZ", -990)
    wrong_kind = _tx(db, "VIR SALAIRE ACME", 250_000)
    fake = FakeProvider(
        {
            "CB CARREFOUR CITY": ("Alimentation › Courses", 95),
            "CB BAZAR DU COIN": ("Achats › High-tech", 40),
            "VIR SALAIRE ACME": ("Alimentation › Courses", 99),  # dépense pour une recette
        }
    )
    result = categorize_pending(db, SECRETS, now=NOW, transport=fake.transport)
    assert (result.sent, result.categorized, result.to_review, result.tokens) == (4, 1, 3, 1000)
    assert _category(db, sure) == ("Courses", CategorizationSource.AI, 950)
    assert _category(db, unsure) is None
    with db.connect() as connection:
        items = {
            row.transaction_id: row.details
            for row in connection.execute(
                select(ReviewItem).where(ReviewItem.kind == ReviewKind.AI_SUGGESTION)
            )
        }
    assert items == {
        unsure: {"categorie": _cat(db, "High-tech"), "confiance": 400},
        unknown: {},
        wrong_kind: {},
    }
    # Rejouable : rien n'est renvoyé.
    assert categorize_pending(db, SECRETS, now=NOW, transport=fake.transport).sent == 0
    assert len(fake.bodies) == 1
    with db.connect() as connection:
        audit: dict[str, int] = connection.execute(
            select(AuditLog.details).where(AuditLog.action == "categorize.ai")
        ).scalar_one()
    assert audit["jetons"] == 1000 and audit["categorisees"] == 1


def test_categorized_by_hand_during_the_call_is_counted_nowhere(db: Engine) -> None:
    """Opération catégorisée à la main pendant l'appel à l'IA : laissée telle quelle, et ni
    « catégorisée » ni « à valider » dans le bilan (elle était comptée à valider)."""
    meanwhile = _tx(db, "CB CARREFOUR CITY", -2350)
    fake = FakeProvider({"CB CARREFOUR CITY": ("Alimentation › Courses", 95)})

    def answer_after_a_manual_choice(request: httpx.Request) -> httpx.Response:
        categorize_manually(db, meanwhile, [(_cat(db, "High-tech"), -2350)], actor_user_id=1,
                            now=NOW)  # fmt: skip
        return fake(request)

    result = categorize_pending(
        db, SECRETS, now=NOW, transport=httpx.MockTransport(answer_after_a_manual_choice)
    )
    assert (result.sent, result.categorized, result.to_review) == (1, 0, 0)
    assert _category(db, meanwhile) == ("High-tech", CategorizationSource.MANUAL, None)


def test_only_pseudonymized_labels_are_sent(db: Engine) -> None:
    add_term(db, kind="personne", value="Dupont", actor_user_id=1, now=NOW)
    _tx(db, "VIR INST M JEAN DUPONT REF 123456789", 5000)
    fake = FakeProvider({})
    categorize_pending(db, SECRETS, now=NOW, transport=fake.transport)
    [label] = fake.sent_labels()
    assert "DUPONT" not in label.upper() and "JEAN" not in label.upper()
    assert "123456789" not in label
    text = fake.bodies[0]["messages"][0]["content"]
    assert "Alimentation › Courses" in text  # liste des catégories
    assert fake.bodies[0].get("tools") in (None, [])


def test_scope_excludes_cards_ignored_old_and_categorized(db: Engine) -> None:
    with db.begin() as connection:
        connection.execute(update(Account).where(Account.id == 2).values(type=AccountType.CARD))
    _tx(db, "CB CARTE", -100, account=2)
    _tx(db, "TRES VIEILLE", -100, day=date(2026, 1, 1))
    done = _tx(db, "DEJA FAITE", -100)
    categorize_manually(db, done, [(_cat(db, "Courses"), -100)], actor_user_id=1, now=NOW)
    kept = _tx(db, "A ENVOYER", -100)
    fake = FakeProvider({})
    assert categorize_pending(db, SECRETS, now=NOW, transport=fake.transport).sent == 1
    assert fake.sent_labels() == ["A ENVOYER"]
    assert kept


def test_rules_and_manual_choices_win_over_ai(db: Engine) -> None:
    tx = _tx(db, "CB CARREFOUR CITY", -2350)
    fake = FakeProvider({"CB CARREFOUR CITY": ("Alimentation › Restaurants", 90)})
    categorize_pending(db, SECRETS, now=NOW, transport=fake.transport)
    assert _category(db, tx) == ("Restaurants", CategorizationSource.AI, 900)
    create_rule(
        db,
        name="Carrefour",
        priority=10,
        conditions={"label_contains": ["CARREFOUR"]},
        category_id=_cat(db, "Courses"),
        actor_user_id=1,
        now=NOW,
    )
    apply_rules(db, now=NOW)
    assert _category(db, tx) == ("Courses", CategorizationSource.RULE, None)


def test_review_decisions(db: Engine) -> None:
    first = _tx(db, "CB BAZAR", -1500)
    second = _tx(db, "CB AUTRE", -1500)
    third = _tx(db, "CB TROISIEME", -1500)
    fake = FakeProvider({"CB BAZAR": ("Achats › Cadeaux", 50), "CB AUTRE": ("Loisirs", 30)})
    categorize_pending(db, SECRETS, now=NOW, transport=fake.transport)
    items = {item.second.id: item for item in open_items(db) if item.second}
    assert set(items) == {first, second, third}
    decide_ai_suggestion(db, items[first].id, category_id=None, actor_user_id=1, now=NOW)
    assert _category(db, first) == ("Cadeaux", CategorizationSource.MANUAL, None)
    decide_ai_suggestion(
        db, items[second].id, category_id=_cat(db, "Sport"), actor_user_id=1, now=NOW
    )
    assert _category(db, second) == ("Sport", CategorizationSource.MANUAL, None)
    dismiss_ai_suggestion(db, items[third].id, actor_user_id=1, now=NOW)
    assert open_items(db) == []
    with db.connect() as connection:
        statuses = sorted(str(s) for s in connection.scalars(select(ReviewItem.status)))
    assert statuses == [ReviewStatus.DISMISSED, ReviewStatus.RESOLVED, ReviewStatus.RESOLVED]
    # Rejetée : l'IA ne la reprend pas.
    assert categorize_pending(db, SECRETS, now=NOW, transport=fake.transport).sent == 0


def test_refusals(db: Engine) -> None:
    _tx(db, "CB X", -100)
    fake = FakeProvider({}, status=401)
    with pytest.raises(AiCategorizationError, match="Clé API refusée"):
        categorize_pending(db, SECRETS, now=NOW, transport=fake.transport)
    with db.connect() as connection:  # panne : rien n'est marqué
        assert connection.execute(select(ReviewItem)).all() == []
    with pytest.raises(AiCategorizationError, match="Clé API"):
        categorize_pending(db, Secrets.model_construct(), now=NOW)
    set_switch(db, ENABLED, False, actor_user_id=1, now=NOW)
    with pytest.raises(AiCategorizationError, match="Accès IA désactivé"):
        categorize_pending(db, SECRETS, now=NOW)
    set_settings(db, enabled=False, threshold=800, actor_user_id=1, now=NOW)
    with pytest.raises(AiCategorizationError, match="désactivée"):
        categorize_pending(db, SECRETS, now=NOW)
    with pytest.raises(AiCategorizationError, match="Seuil"):
        set_settings(db, enabled=True, threshold=1001, actor_user_id=1, now=NOW)
    assert settings(db) == {"categorisation_auto": False, "seuil_confiance": 800}


def test_monthly_cap_is_shared_with_the_assistant(db: Engine) -> None:
    _tx(db, "CB X", -100)
    with db.begin() as connection:
        connection.execute(
            insert(AuditLog).values(
                at=NOW, actor_user_id=None, action="assistant.message", object_type="x",
                object_id=None, details={"jetons": 2_000_000},
            )
        )  # fmt: skip
    with pytest.raises(AiCategorizationError, match="Plafond"):
        categorize_pending(db, SECRETS, now=NOW)


def test_nothing_to_send(db: Engine) -> None:
    fake = FakeProvider({})
    assert categorize_pending(db, SECRETS, now=NOW, transport=fake.transport).sent == 0
    assert fake.bodies == []


def test_parse_answer_is_tolerant_but_strict_on_categories() -> None:
    categories = {
        "Alimentation › Courses": (1, CategoryKind.EXPENSE),
        "Loisirs › Sport": (2, CategoryKind.EXPENSE),
        "Divers › Sport": (3, CategoryKind.EXPENSE),
        "Revenus": (4, CategoryKind.INCOME),
    }
    text = (
        "```json\n["
        '{"n": 1, "categorie": "alimentation > courses", "confiance": 0.92},'
        '{"n": 2, "categorie": "Sport", "confiance": 80},'  # ambigu : deux « Sport »
        '{"n": 3, "categorie": "Courses", "confiance": 70},'  # feuille unique
        '{"n": 4, "categorie": "Revenus", "confiance": "beaucoup"},'
        '{"n": 9, "categorie": "Revenus", "confiance": 90},'  # hors plage
        '{"n": 1, "categorie": "Revenus", "confiance": 90},'  # doublon de rang
        '"bruit", {"n": "5"}]\n```'
    )
    choices = parse_answer(text, 5, categories)
    assert choices[1].category_id == 1 and choices[1].confidence_permille == 920
    assert choices[2].category_id is None
    assert (choices[3].category_id, choices[3].confidence_permille) == (1, 700)
    assert (choices[4].category_id, choices[4].confidence_permille) == (4, 0)
    assert set(choices) == {1, 2, 3, 4}
    assert parse_answer("pas de JSON", 3, categories) == {}
    assert parse_answer("[pas, du, json]", 3, categories) == {}
    assert parse_answer('[{"n": 1}] [et puis]', 3, categories) == {}
    assert parse_answer('{"n": 1}', 1, categories) == {}
    assert (
        parse_answer('[{"n": 1, "categorie": "Revenus", "confiance": 150}]', 1, categories)[
            1
        ].confidence_permille
        == 0
    )
    assert (
        parse_answer('[{"n": 1, "categorie": "Revenus", "confiance": true}]', 1, categories)[
            1
        ].confidence_permille
        == 0
    )


def test_income_and_transfer_categories_follow_the_sign(db: Engine) -> None:
    salary = _tx(db, "SALAIRE ACME", 250_000)
    saving = _tx(db, "PRLV LIVRET EPARGNE", -10_000)
    fake = FakeProvider(
        {
            "SALAIRE ACME": ("Revenus › Salaire", 97),
            "PRLV LIVRET EPARGNE": ("Épargne et placements › Versement épargne", 90),
        }
    )
    assert categorize_pending(db, SECRETS, now=NOW, transport=fake.transport).categorized == 2
    assert _category(db, salary) == ("Salaire", CategorizationSource.AI, 970)
    assert _category(db, saving) == ("Versement épargne", CategorizationSource.AI, 900)


def test_operation_categorized_meanwhile_is_left_alone(db: Engine) -> None:
    tx = _tx(db, "CB X", -100)
    [row] = pending(db, NOW.date())
    categorize_manually(db, tx, [(_cat(db, "Courses"), -100)], actor_user_id=1, now=NOW)
    choice = Choice(_cat(db, "Restaurants"), 990)
    kinds = {_cat(db, "Restaurants"): CategoryKind.EXPENSE}
    with db.begin() as connection:
        assert _record(connection, row, choice, kinds, 800, NOW) is Recorded.SKIPPED
    assert _category(db, tx) == ("Courses", CategorizationSource.MANUAL, None)


def test_batch_with_an_unmasked_identifier_is_not_sent(
    db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tx(db, "CB X", -100)

    class Leaky:
        def label(self, raw: str) -> str:
            return raw

        def check(self, payload: Any) -> None:
            raise LeakError("identifiant non masqué")

    monkeypatch.setattr("app.categorize.ai.load_pseudonymizer", lambda engine: Leaky())
    fake = FakeProvider({})
    with pytest.raises(AiCategorizationError, match="non pseudonymisable"):
        categorize_pending(db, SECRETS, now=NOW, transport=fake.transport)
    assert fake.bodies == []
