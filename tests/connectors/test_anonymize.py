"""Anonymisation des réponses réelles avant qu'elles quittent la machine (inv. 5, lot 1)."""

import json
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from app.connectors.enable_banking.anonymize import CODE, CODE_KEYS, anonymize
from app.connectors.enable_banking.mapping import to_transactions
from app.iban import is_valid_iban
from tests.connectors.eb_fake import load
from tests.factories import fake_iban

REAL_LOOKING: dict[str, Any] = {
    "transactions": [
        {
            "entry_reference": "REF-SECRETE-123",
            "transaction_amount": {"currency": "EUR", "amount": "1234.56"},
            "creditor": {"name": "Alice Exemple"},
            "creditor_account": {"iban": fake_iban(42)},
            "credit_debit_indicator": "DBIT",
            "status": "BOOK",
            "booking_date": "2026-09-25",
            "value_date": "2026-09-24",
            "remittance_information": ["PRLV SEPA ASSUREUR NUMERO 778899", "Alice Exemple"],
            "bank_transaction_code": {
                "description": "Prélèvement",
                "code": "PMNT",
                "sub_code": "DD",
            },
        },
        {
            "entry_reference": "REF-SECRETE-124",
            "transaction_amount": {"currency": "EUR", "amount": "850"},
            "debtor": {"name": "Alice Exemple"},
            "credit_debit_indicator": "CRDT",
            "status": "BOOK",
            "booking_date": "2026-09-05",
            "remittance_information": ["LOYER SEPTEMBRE"],
        },
    ],
    "continuation_key": "clé-de-page-opaque",
}


def _strings(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [s for value in node.values() for s in _strings(value)]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item)]
    return [node] if isinstance(node, str) else []


def test_no_personal_text_survives() -> None:
    out = json.dumps(anonymize(REAL_LOOKING), ensure_ascii=False)
    for secret in ("Alice", "ASSUREUR", "778899", "REF-SECRETE", "LOYER", "Prélèvement", "opaque"):
        assert secret not in out
    assert fake_iban(42) not in out and "1234.56" not in out and "2026-09-25" not in out


def test_structure_codes_and_formats_are_kept() -> None:
    out = anonymize(REAL_LOOKING)
    first, second = out["transactions"]
    assert first.keys() == REAL_LOOKING["transactions"][0].keys()
    assert (first["status"], first["credit_debit_indicator"]) == ("BOOK", "DBIT")
    assert first["bank_transaction_code"]["code"] == "PMNT"
    assert first["booking_date"] == "2023-12-30"  # décalée de 1000 jours, même format
    assert is_valid_iban(first["creditor_account"]["iban"])
    assert "99999" in first["creditor_account"]["iban"]  # IBAN fictif accepté par gitleaks
    assert len(first["transaction_amount"]["amount"].split(".")[1]) == 2
    assert "." not in second["transaction_amount"]["amount"]  # même nombre de décimales (0)
    # Même valeur d'origine → même remplaçant (les égalités servent au dédoublonnage).
    assert first["creditor"]["name"] == second["debtor"]["name"]
    assert first["entry_reference"] != second["entry_reference"]


def test_mapping_still_works_on_anonymized_documents() -> None:
    for document in (REAL_LOOKING, load("transactions_page1.json")):
        before, after = to_transactions(document), to_transactions(anonymize(document))
        assert len(after) == len(before)
        assert [t.amount < 0 for t in after] == [t.amount < 0 for t in before]
        assert [t.external_id is None for t in after] == [t.external_id is None for t in before]


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(min_size=1, max_size=30),
    lambda inner: (
        st.lists(inner, max_size=4)
        | st.dictionaries(
            st.sampled_from(["name", "status", "amount", "iban", "note", "code"]), inner
        )
    ),
    max_leaves=25,
)


def _pairs(node: Any, key: str = "") -> list[tuple[str, str]]:
    if isinstance(node, dict):
        return [pair for name, value in node.items() for pair in _pairs(value, name)]
    if isinstance(node, list):
        return [pair for item in node for pair in _pairs(item, key)]
    return [(key, node)] if isinstance(node, str) else []


@given(document=json_values)
def test_property_nothing_but_codes_survives(document: Any) -> None:
    """Quelle que soit la réponse, un texte d'origine d'au moins 4 caractères ne réapparaît que
    s'il a la forme d'un code et figurait sous une clé de code connue."""
    kept = set(_strings(anonymize(document)))
    for key, value in _pairs(document):
        if len(value) >= 4 and value in kept:
            allowed = {v for k, v in _pairs(document) if k in CODE_KEYS and CODE.match(v)}
            assert value in allowed, (key, value)
