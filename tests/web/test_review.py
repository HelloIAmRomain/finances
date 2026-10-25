"""Page « À valider » : décisions sur doublons, virements et conflits ; annulation (T1.25c)."""

from datetime import date

from sqlalchemy import Engine, insert, select

from app.categorize.seed import seed_categories
from app.categorize.service import categorize_manually
from app.ledger.cross_dedupe import detect_duplicates
from app.ledger.transfers import detect_transfers
from app.models import (
    AuditLog,
    Categorization,
    Category,
    DuplicateLink,
    ImportBatch,
    LedgerTransaction,
    ReviewItem,
    TransferLink,
)
from app.models.categorize import ReviewStatus
from app.models.ledger import ImportSource, ImportStatus
from tests.web.conftest import T0, Web

_ids = iter(range(1, 100_000))


def _tx(engine: Engine, *, account: int, amount: int, day: date, source: ImportSource) -> int:
    tx_id = next(_ids)
    with engine.begin() as connection:
        connection.execute(
            insert(ImportBatch).values(
                id=tx_id,
                account_id=account,
                source=source,
                started_at=T0,
                status=ImportStatus.SUCCESS,
            )
        )
        connection.execute(
            insert(LedgerTransaction).values(
                id=tx_id,
                account_id=account,
                dedupe_key=f"ext:{source}:{tx_id}",
                fingerprint="x",
                occurrence=1,
                booking_date=day,
                amount_cents=amount,
                currency="EUR",
                label_raw="CB BOULANGERIE",
                label_clean="CB BOULANGERIE",
                import_batch_id=tx_id,
                created_at=T0,
            )
        )
    return tx_id


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def _open_kinds(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.scalars(
            select(ReviewItem.kind).where(ReviewItem.status == ReviewStatus.OPEN)
        )
        return [str(kind) for kind in rows]


def test_probable_duplicate_confirmed_from_the_page(web: Web) -> None:
    _tx(
        web.engine, account=1, amount=-420, day=date(2026, 9, 3), source=ImportSource.ENABLE_BANKING
    )
    _tx(web.engine, account=1, amount=-420, day=date(2026, 9, 2), source=ImportSource.CSV)
    detect_duplicates(web.engine, account_id=1)
    web.login()
    page = web.client.get("/a-valider").text
    assert "Doublon probable" in page and "Opération conservée" in page
    with web.engine.connect() as connection:
        link_id = connection.scalars(select(DuplicateLink.id)).one()
    web.client.post(f"/a-valider/doublons/{link_id}/confirmer", data={"csrf": _csrf(page)})
    assert _open_kinds(web.engine) == []
    page = web.client.get("/a-valider").text
    assert "Rien à valider." in page and f"Lien n° {link_id}" in page
    web.client.post(f"/a-valider/doublons/{link_id}/annuler", data={"csrf": _csrf(page)})
    with web.engine.connect() as connection:
        assert str(connection.scalars(select(DuplicateLink.status)).one()) == "reverted"


def test_transfer_candidate_rejected(web: Web) -> None:
    _tx(
        web.engine,
        account=1,
        amount=-50000,
        day=date(2026, 9, 10),
        source=ImportSource.ENABLE_BANKING,
    )
    _tx(
        web.engine,
        account=2,
        amount=50000,
        day=date(2026, 9, 10),
        source=ImportSource.ENABLE_BANKING,
    )
    detect_transfers(web.engine)
    web.login()
    page = web.client.get("/a-valider").text
    assert "Virement probable entre deux de vos comptes" in page
    with web.engine.connect() as connection:
        link_id = connection.scalars(select(TransferLink.id)).one()
    web.client.post(f"/a-valider/virements/{link_id}/rejeter", data={"csrf": _csrf(page)})
    with web.engine.connect() as connection:
        assert str(connection.scalars(select(TransferLink.status)).one()) == "rejected"
        assert list(connection.scalars(select(AuditLog.action)))[-1] == "transfer.rejected"
    assert _open_kinds(web.engine) == []


def test_category_conflict_take_duplicate(web: Web) -> None:
    seed_categories(web.engine)
    with web.engine.connect() as connection:
        courses, restos = (
            connection.scalars(select(Category.id).where(Category.name == name)).one()
            for name in ("Courses", "Restaurants")
        )
    api = _tx(
        web.engine, account=1, amount=-420, day=date(2026, 9, 3), source=ImportSource.ENABLE_BANKING
    )
    csv = _tx(web.engine, account=1, amount=-420, day=date(2026, 9, 3), source=ImportSource.CSV)
    categorize_manually(web.engine, api, [(courses, -420)], actor_user_id=None, now=T0)
    categorize_manually(web.engine, csv, [(restos, -420)], actor_user_id=None, now=T0)
    detect_duplicates(web.engine, account_id=1)  # lien certain + report → conflit ouvert
    assert _open_kinds(web.engine) == ["category_conflict"]
    web.login()
    page = web.client.get("/a-valider").text
    assert "Deux catégories choisies à la main" in page
    with web.engine.connect() as connection:
        item_id = connection.scalars(select(ReviewItem.id)).one()
    web.client.post(
        f"/a-valider/conflits/{item_id}/categorie", data={"csrf": _csrf(page), "choix": "doublon"}
    )
    with web.engine.connect() as connection:
        current = list(
            connection.scalars(
                select(Categorization.category_id).where(
                    Categorization.transaction_id == api, Categorization.superseded_at.is_(None)
                )
            )
        )
    assert current == [restos]
    assert _open_kinds(web.engine) == []


def test_decisions_require_csrf(web: Web) -> None:
    web.login()
    assert web.client.post("/a-valider/doublons/1/confirmer").status_code == 403


def test_stale_or_unknown_decisions_are_ignored(web: Web) -> None:
    """Double clic, onglet resté ouvert : une décision déjà prise ou inconnue ne plante pas."""
    web.login()
    csrf = _csrf(web.client.get("/a-valider").text)
    for path, data in (
        ("/a-valider/doublons/999/confirmer", {}),
        ("/a-valider/doublons/999/inconnue", {}),
        ("/a-valider/virements/999/confirmer", {}),
        ("/a-valider/virements/999/inconnue", {}),
        ("/a-valider/conflits/999/categorie", {"choix": "doublon"}),
        ("/a-valider/conflits/999/virement", {}),
    ):
        response = web.client.post(path, data={"csrf": csrf, **data}, follow_redirects=False)
        assert response.status_code == 303, path
