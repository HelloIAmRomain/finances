"""Page « Transactions » : filtres (ANA-11), règle suggérée (ANA-03), export (FIS-06)."""

import re
from datetime import date

import pytest
from sqlalchemy import Engine, insert, select

from app.categorize.seed import seed_categories
from app.models import AuditLog, Category, LedgerTransaction, Rule
from tests.web.conftest import T0, Web

_ids = iter(range(1, 100_000))


def _tx(engine: Engine, label: str, amount: int, day: date, account: int = 1) -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id,
                account_id=account,
                dedupe_key=f"ext:test:{tx_id}",
                fingerprint="x",
                occurrence=1,
                booking_date=day,
                amount_cents=amount,
                currency="EUR",
                label_raw=label,
                label_clean=label.upper(),
                created_at=T0,
            )
        )
    return tx_id


def _category(engine: Engine, name: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalars(select(Category.id).where(Category.name == name)).one())


@pytest.fixture
def data(web: Web) -> Web:
    seed_categories(web.engine)
    _tx(web.engine, "CB CARREFOUR MARKET", -4520, date(2026, 9, 3))
    _tx(web.engine, "PRLV SEPA ENERGIE EXEMPLE", -8000, date(2026, 9, 10))
    _tx(web.engine, "VIR SEPA SALAIRE", 250000, date(2026, 9, 28), account=2)
    web.login()
    return web


def _labels(page: str) -> list[str]:
    return re.findall(r"<td>((?:CB|PRLV|VIR)[^<]*)", page)


def test_lists_newest_first(data: Web) -> None:
    page = data.client.get("/transactions").text
    assert _labels(page) == ["VIR SEPA SALAIRE", "PRLV SEPA ENERGIE EXEMPLE", "CB CARREFOUR MARKET"]
    assert "3 opération(s)" in page
    assert "-45,20\u00a0€" in page


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("texte=carrefour", ["CB CARREFOUR MARKET"]),
        ("sens=credit", ["VIR SEPA SALAIRE"]),
        ("compte=2", ["VIR SEPA SALAIRE"]),
        ("du=2026-09-05&au=2026-09-15", ["PRLV SEPA ENERGIE EXEMPLE"]),
        ("min=-50&max=-10", ["CB CARREFOUR MARKET"]),
        ("max=-50,00", ["PRLV SEPA ENERGIE EXEMPLE"]),
        ("texte=sepa&sens=debit", ["PRLV SEPA ENERGIE EXEMPLE"]),
    ],
)
def test_filters(data: Web, query: str, expected: list[str]) -> None:
    assert _labels(data.client.get(f"/transactions?{query}").text) == expected


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def test_manual_category_then_suggested_rule(data: Web) -> None:
    page = data.client.get("/transactions?texte=energie").text
    tx_id = int(re.search(r"/transactions/(\d+)/categorie", page).group(1))  # type: ignore[union-attr]
    response = data.client.post(
        f"/transactions/{tx_id}/categorie",
        data={
            "csrf": _csrf(page),
            "categorie": _category(data.engine, "Énergie"),
            "retour": "/transactions?texte=energie",
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/transactions?texte=energie&suggestion={tx_id}"
    suggested = data.client.get(response.headers["location"]).text
    assert "Logement › Énergie" in suggested
    assert "« PRLV SEPA ENERGIE EXEMPLE »" in suggested
    data.client.post(f"/regles/depuis-transaction/{tx_id}", data={"csrf": _csrf(suggested)})
    with data.engine.connect() as connection:
        rule = connection.execute(select(Rule.conditions, Rule.category_id)).one()
    assert rule.conditions == {
        "label_contains": ["PRLV SEPA ENERGIE EXEMPLE"],
        "direction": "debit",
    }
    assert rule.category_id == _category(data.engine, "Énergie")


def test_category_filters(data: Web) -> None:
    page = data.client.get("/transactions").text
    tx_id = int(re.findall(r"/transactions/(\d+)/categorie", page)[-1])
    data.client.post(
        f"/transactions/{tx_id}/categorie",
        data={"csrf": _csrf(page), "categorie": _category(data.engine, "Courses")},
    )
    assert _labels(
        data.client.get(f"/transactions?categorie={_category(data.engine, 'Alimentation')}").text
    ) == ["CB CARREFOUR MARKET"]
    assert "CB CARREFOUR MARKET" not in _labels(data.client.get("/transactions?categorie=0").text)


def test_export_csv_follows_filters_and_is_audited(data: Web) -> None:
    response = data.client.get("/transactions/export.csv?sens=debit")
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.content.decode("utf-8-sig").splitlines()
    assert lines[0] == "Date;Compte;Libellé;Catégories;Montant (EUR);Virement interne"
    assert lines[1:] == [
        "10/09/2026;Compte;PRLV SEPA ENERGIE EXEMPLE;;-80,00;",
        "03/09/2026;Compte;CB CARREFOUR MARKET;;-45,20;",
    ]
    with data.engine.connect() as connection:
        audit = connection.execute(select(AuditLog.action, AuditLog.details)).all()[-1]
    assert audit.action == "export.transactions_csv" and audit.details == {
        "filtres": {"direction": "debit"}
    }


def test_export_csv_neutralises_formulas_from_bank_labels(data: Web) -> None:
    _tx(data.engine, '=HYPERLINK("https://pirate.example/";"Voir")', -100, date(2026, 9, 20))
    _tx(data.engine, "+33 VIR RECU", 2_500, date(2026, 9, 21))
    lines = data.client.get("/transactions/export.csv").content.decode("utf-8-sig").splitlines()
    assert "21/09/2026;Compte;'+33 VIR RECU;;25,00;" in lines
    assert any(line.startswith("20/09/2026;Compte;\"'=HYPERLINK(") for line in lines)
    assert not any(";=" in line or ";+" in line or ";@" in line for line in lines)


def test_pagination(data: Web) -> None:
    for number in range(60):
        _tx(data.engine, f"CB ACHAT {number}", -100, date(2026, 8, 1))
    first = data.client.get("/transactions").text
    assert "page 1 sur 2" in first and "Suivante" in first
    second = data.client.get("/transactions?page=2").text
    assert "Précédente" in second


def test_categorizing_requires_csrf(data: Web) -> None:
    response = data.client.post("/transactions/1/categorie", data={"categorie": 1})
    assert response.status_code == 403


def test_classify_unknown_transaction_or_category_is_harmless(web: Web) -> None:
    web.login()
    page = web.client.get("/transactions").text
    csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    for path, category in (
        ("/transactions/999/categorie", "1"),
        ("/transactions/1/categorie", "99999"),
    ):
        response = web.client.post(
            path, data={"csrf": csrf, "categorie": category}, follow_redirects=False
        )
        assert response.status_code == 303  # retour à la liste, sans erreur 500
        shown = web.client.get(response.headers["location"]).text
        assert "Catégorie non enregistrée" in shown and 'role="alert"' in shown


def test_unreadable_filters_are_announced_not_silently_dropped(data: Web) -> None:
    page = data.client.get("/transactions?min=beaucoup&du=hier&texte=sepa").text
    assert "Filtre ignoré, saisie illisible : Du, Montant min." in page
    assert sorted(_labels(page)) == ["PRLV SEPA ENERGIE EXEMPLE", "VIR SEPA SALAIRE"]
    assert "Filtre ignoré" not in data.client.get("/transactions?min=-10").text
