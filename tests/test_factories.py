"""Données factices, anonymisation et faux connecteur (inv. 5, NF-EXP-02)."""

from datetime import date
from typing import cast

import pytest

from app.connectors.base import RemoteTransaction
from app.connectors.requester import Budget
from app.iban import is_valid_iban, normalize_iban
from app.money import Cents
from scripts.anonymize_cassette import anonymize
from tests.factories import FAKE_BANK_CODE, fake_email, fake_iban
from tests.fakes.connector import FakeBankConnector, InjectedFailureError

NO_BUDGET = cast(Budget, None)  # le faux connecteur ne consomme pas de budget

# IBAN d'exemple largement publié (documentation bancaire), assemblé pour ne pas figurer tel quel
# dans le dépôt : il sert à vérifier que l'anonymiseur remplace bien un IBAN au format réel.
SAMPLE_REAL_FORMAT = "FR76" + " 3000 6000 0112 3456 7890 189"


def test_fake_iban_is_valid_deterministic_and_fake() -> None:
    assert is_valid_iban(fake_iban(1))
    assert fake_iban(1) == fake_iban(1) != fake_iban(2)
    assert fake_iban(42)[4:9] == FAKE_BANK_CODE
    assert len(fake_iban(1)) == 27


def test_iban_checksum_detects_typos() -> None:
    valid = fake_iban(7)
    typo = valid[:-1] + ("0" if valid[-1] != "0" else "1")
    assert not is_valid_iban(typo)
    assert is_valid_iban(SAMPLE_REAL_FORMAT)
    assert not is_valid_iban("FR76 1234")


def test_anonymize_replaces_real_ibans_consistently() -> None:
    text = f'{{"a": "{SAMPLE_REAL_FORMAT}", "b": "{normalize_iban(SAMPLE_REAL_FORMAT)}"}}'
    result = anonymize(text, [])
    assert "3000 6000" not in result and "30006000" not in result
    assert result.count(fake_iban(1)) == 2  # le même IBAN devient le même IBAN fictif


def test_anonymize_keeps_fake_ibans_and_masks_other_data() -> None:
    # Adresse assemblée à l'exécution : elle ne figure jamais telle quelle dans le dépôt.
    email = "jeanne.dupont" + "@" + "banque.fr"
    text = f"{fake_iban(9)} {email} Jeanne DUPONT 06 12 34 56 78"
    result = anonymize(text, ["Jeanne Dupont"])
    assert fake_iban(9) in result
    assert "banque.fr" not in result and fake_email(1) in result
    assert "dupont" not in result.lower()
    assert "06 12" not in result


def _tx(number: int) -> RemoteTransaction:
    return RemoteTransaction(
        external_id=f"T{number}",
        booking_date=date(2026, 9, 1 + number % 28),
        value_date=None,
        amount=Cents(-100 * number),
        currency="EUR",
        label=f"CB MAGASIN {number}",
        counterparty_name=None,
        counterparty_iban=None,
        bank_tx_code=None,
    )


def test_fake_connector_pages_and_filters() -> None:
    connector = FakeBankConnector(
        transaction_items={"uid-1": [_tx(n) for n in range(5)]}, page_size=2
    )
    pages = list(connector.transactions("uid-1", date(2026, 9, 1), NO_BUDGET))
    assert [len(page.items) for page in pages] == [2, 2, 1]
    assert all(page.raw.content_type == "application/json" for page in pages)


def test_fake_connector_injects_failure_mid_sync() -> None:
    connector = FakeBankConnector(
        transaction_items={"uid-1": [_tx(n) for n in range(5)]}, page_size=2, fail_on_page=1
    )
    pages = connector.transactions("uid-1", date(2026, 9, 1), NO_BUDGET)
    assert len(next(pages).items) == 2
    with pytest.raises(InjectedFailureError):
        next(pages)
