"""Abonnements (ANA-07) et anomalies (ANA-08) : périmètre, vue, alertes une seule fois."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Engine, insert, select, update

from app.alerts.engine import resolve
from app.ledger.subscriptions import check_recurring, recurring_view
from app.models import Account, Alert, LedgerTransaction, TransferLink
from app.models.alerts import AlertStatus, Severity
from app.models.ledger import AccountType

_ids = iter(range(1, 10_000_000))
T0 = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
TODAY = date(2026, 10, 20)


def _tx(engine: Engine, day: date, amount: int, label: str, account: int = 1) -> int:
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
                created_at=T0,
            )
        )
    return tx_id


def _monthly(engine: Engine, label: str, amounts: list[int], last: date, account: int = 1) -> None:
    for k, amount in enumerate(reversed(amounts)):
        _tx(engine, last - timedelta(days=30 * k), amount, f"{label} {k:02d}/10", account)


def _kind(key: str) -> str:
    """« recurring:<empreinte>:prix:<date> » → « recurring:prix » ; « anomaly:frais:<id> » →
    « anomaly:frais »."""
    parts = key.split(":")
    return f"{parts[0]}:{parts[2] if parts[0] == 'recurring' else parts[1]}"


def _alerts(engine: Engine) -> list[tuple[str, Severity, AlertStatus, str]]:
    with engine.connect() as connection:
        return [
            (_kind(row.dedupe_key), row.severity, row.status, row.title)
            for row in connection.execute(select(Alert).order_by(Alert.id))
        ]


def test_view_lists_active_series_and_excludes_cards_and_transfers(engine: Engine) -> None:
    _monthly(engine, "PRLV NETFLIX", [-1399] * 4, date(2026, 10, 15))
    _monthly(engine, "VIR SALAIRE", [250_000] * 3, date(2026, 9, 28))
    _monthly(engine, "PRLV ANCIEN", [-999] * 3, date(2026, 5, 1))  # disparu depuis longtemps
    with engine.begin() as connection:
        connection.execute(update(Account).where(Account.id == 2).values(type=AccountType.CARD))
    _monthly(engine, "CB SPOTIFY", [-1099] * 4, date(2026, 10, 10), account=2)  # carte
    for k in range(3):  # virement interne mensuel : pas un abonnement
        debit = _tx(engine, TODAY - timedelta(days=30 * k), -50_000, "VIR EPARGNE")
        credit = _tx(engine, TODAY - timedelta(days=30 * k), 50_000, "VIR RECU EPARGNE", 2)
        with engine.begin() as connection:
            connection.execute(
                insert(TransferLink).values(
                    debit_tx_id=debit,
                    credit_tx_id=credit,
                    method="manual",
                    status="confirmed",
                    created_at=T0,
                )
            )
    view = recurring_view(engine, TODAY)
    assert [s.key for s in view.active_debits] == ["PRLV NETFLIX"]
    assert [s.key for s in view.active_credits] == ["VIR SALAIRE"]
    assert view.missed == []
    assert view.monthly_debits_cents == -1399
    assert view.annual_debits_cents == -1399 * 12


def test_price_increase_alert_once_then_resolved_when_it_becomes_usual(engine: Engine) -> None:
    _monthly(engine, "PRLV BOX", [-2999, -2999, -2999, -3499], date(2026, 10, 15))
    assert check_recurring(engine, now=T0) == 1
    assert check_recurring(engine, now=T0) == 1
    [alert] = _alerts(engine)
    assert alert == (
        "recurring:prix",
        Severity.INFO,
        AlertStatus.OPEN,
        "Abonnement : hausse de prix",
    )
    with engine.connect() as connection:
        details: dict[str, str] = connection.execute(select(Alert.details)).scalar_one()
    assert details["libelle"] == "PRLV BOX"
    assert details["avant"].startswith("29,99")
    # Un mois et demi plus tard (prélèvement suivant au nouveau prix) : devenu l'habitude.
    _tx(engine, date(2026, 11, 14), -3499, "PRLV BOX 11/11")
    check_recurring(engine, now=T0 + timedelta(days=45))
    assert [status for _, _, status, _ in _alerts(engine)] == [AlertStatus.RESOLVED]


def test_missed_debit_alert_resolved_when_it_comes_back(engine: Engine) -> None:
    _monthly(engine, "PRLV ASSURANCE", [-4500] * 4, date(2026, 9, 10))
    check_recurring(engine, now=T0)  # attendu le 10/10, tolérance 5 jours : manqué le 20
    assert _alerts(engine)[0][:2] == ("recurring:manque", Severity.INFO)
    _tx(engine, date(2026, 10, 21), -4500, "PRLV ASSURANCE 21/10")
    check_recurring(engine, now=T0 + timedelta(days=1))
    assert [status for _, _, status, _ in _alerts(engine)] == [AlertStatus.RESOLVED]


def test_recent_anomalies_raise_alerts_once(engine: Engine) -> None:
    for month in (6, 7, 8):
        _tx(engine, date(2026, month, 2), -4500, "CB SALLE DE SPORT")
    _tx(engine, date(2026, 10, 18), -15000, "CB SALLE DE SPORT")
    _tx(engine, date(2026, 10, 19), -890, "FRAIS TENUE DE COMPTE")
    for _ in range(2):
        _tx(engine, date(2026, 10, 19), -1200, "CB BOULANGERIE")  # moins de 20 € : ignoré
    for _ in range(2):
        _tx(engine, date(2026, 10, 19), -12000, "CB GARAGE")
    _tx(engine, date(2026, 10, 1), -30000, "CB VIEILLE BIJOUTERIE")  # hors des 7 jours
    assert check_recurring(engine, now=T0) == 4
    assert sorted((kind, severity, title) for kind, severity, _, title in _alerts(engine)) == [
        ("anomaly:doublon", Severity.WARNING, "Doublon possible"),
        ("anomaly:frais", Severity.INFO, "Frais bancaires prélevés"),
        ("anomaly:montant_inhabituel", Severity.INFO, "Montant inhabituel"),
        ("anomaly:nouveau_crediteur", Severity.INFO, "Nouveau créancier"),
    ]
    with engine.connect() as connection:
        unusual: dict[str, str] = connection.execute(
            select(Alert.details).where(Alert.dedupe_key.like("anomaly:montant%"))
        ).scalar_one()
    assert unusual["habituellement"].startswith("45,00")
    # Résolue à la main : jamais relevée pour la même opération.
    with engine.connect() as connection:
        ids = list(connection.execute(select(Alert.id)).scalars())
    resolve(engine, ids[0], now=T0)
    check_recurring(engine, now=T0 + timedelta(hours=9))
    with engine.connect() as connection:
        assert len(connection.execute(select(Alert.id)).all()) == 4
