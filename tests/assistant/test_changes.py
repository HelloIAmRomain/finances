"""Lots de modifications faites via l'IA : journal, annulation, aperçu, confirmation (spec §4)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.assistant.changes import (
    ChangeError,
    _deserialize,
    change_sets,
    changes_of,
    consume_confirmation,
    issue_confirmation,
    save,
    simulate,
    snapshot,
    tracked,
    undo,
)
from app.categorize.service import categorize_manually, create_rule
from app.models import Categorization, RentAllocation, Rule
from app.rental.allocations import allocate_manually
from app.rental.views import lease_views
from tests.assistant.conftest import NOW, PASSWORD, Demo


def _rule(demo: Demo):  # type: ignore[no-untyped-def]
    restaurants = demo.category("Restaurants")
    return lambda engine: create_rule(
        engine,
        name="Uber Eats",
        priority=10,
        conditions={"label_contains": ["UBER EATS"]},
        category_id=restaurants,
        actor_user_id=demo.user_id,
        now=NOW,
    )


def _active(demo: Demo) -> dict[int, list[int]]:
    with demo.engine.connect() as connection:
        rows = connection.execute(
            select(Categorization.transaction_id, Categorization.category_id).where(
                Categorization.superseded_at.is_(None)
            )
        ).all()
    out: dict[int, list[int]] = {}
    for tx, category in rows:
        out.setdefault(tx, []).append(category)
    return out


def test_rule_creation_is_recorded_then_undone(demo: Demo) -> None:
    before = snapshot(demo.engine)
    changes = tracked(demo.engine, _rule(demo)).changes
    tables = {c.table for c in changes}
    assert tables == {"rule", "categorization"}
    assert _active(demo)[demo.tx["uber_aout"]] == [demo.category("Restaurants")]
    change_id = save(
        demo.engine,
        changes,
        token_id=None,
        actor_user_id=demo.user_id,
        tool="creer_regle",
        summary="Règle Uber Eats",
        now=NOW,
    )
    assert change_id is not None and changes_of(demo.engine, change_id) == changes
    undo(demo.engine, change_id, actor_user_id=demo.user_id, now=NOW)
    assert snapshot(demo.engine) == before
    assert change_sets(demo.engine)[0].undone_at is not None
    with pytest.raises(ChangeError, match="déjà annulée"):
        undo(demo.engine, change_id, actor_user_id=demo.user_id, now=NOW)
    with pytest.raises(ChangeError, match="introuvable"):
        undo(demo.engine, 999, actor_user_id=None, now=NOW)


def test_nothing_changed_means_no_change_set(demo: Demo) -> None:
    changes = tracked(demo.engine, lambda engine: None).changes
    assert changes == []
    assert (
        save(
            demo.engine, changes, token_id=None, actor_user_id=None, tool="x", summary="x", now=NOW
        )
        is None
    )


def test_undo_refused_when_changed_since(demo: Demo) -> None:
    changes = tracked(demo.engine, _rule(demo)).changes
    change_id = save(
        demo.engine,
        changes,
        token_id=None,
        actor_user_id=None,
        tool="creer_regle",
        summary="r",
        now=NOW,
    )
    assert change_id is not None
    # Je corrige ensuite une des opérations à la main : l'annulation ne doit pas l'écraser.
    categorize_manually(
        demo.engine,
        demo.tx["uber_aout"],
        [(demo.category("Sorties"), -2390)],
        actor_user_id=demo.user_id,
        now=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ChangeError, match=r"modifié depuis.*catégorisation"):
        undo(demo.engine, change_id, actor_user_id=None, now=NOW)
    assert _active(demo)[demo.tx["uber_aout"]] == [demo.category("Sorties")]


def test_simulation_writes_nothing_and_predicts_exactly(demo: Demo) -> None:
    before = snapshot(demo.engine)
    predicted = simulate(demo.engine, _rule(demo)).changes
    assert snapshot(demo.engine) == before
    with demo.engine.connect() as connection:
        assert connection.scalar(select(Rule.id)) is None
    applied = tracked(demo.engine, _rule(demo)).changes
    assert predicted == applied


def test_rent_allocation_undo_reruns_the_engine(demo: Demo) -> None:
    lease = lease_views(demo.engine, NOW.date(), demo.lease_id)[0]
    september = next(d for d in lease.dues if d.period == "2026-09")
    paid_before = september.paid_cents
    tx = demo.tx["ami_rembourse"]  # un autre virement, affecté à tort
    changes = tracked(
        demo.engine,
        lambda engine: allocate_manually(
            engine,
            due_id=september.id,
            transaction_id=tx,
            amount_cents=None,
            note=None,
            actor_user_id=None,
            now=NOW,
        ),
    ).changes
    assert "rent_allocation" in {c.table for c in changes}
    change_id = save(
        demo.engine,
        changes,
        token_id=None,
        actor_user_id=None,
        tool="affecter_loyer",
        summary="a",
        now=NOW,
    )
    assert change_id is not None
    undo(demo.engine, change_id, actor_user_id=None, now=NOW)
    with demo.engine.connect() as connection:
        assert (
            connection.scalar(select(RentAllocation.id).where(RentAllocation.transaction_id == tx))
            is None
        )
    lease = lease_views(demo.engine, NOW.date(), demo.lease_id)[0]
    assert next(d for d in lease.dues if d.period == "2026-09").paid_cents == paid_before


def test_confirmation_is_single_use_bound_and_expires(demo: Demo) -> None:
    from app.assistant.access import create_token  # noqa: PLC0415

    create_token(
        demo.engine,
        user_id=demo.user_id,
        password=PASSWORD,
        name="PC",
        can_write=True,
        now=NOW,
    )
    params = {"ids": ["t_1", "t_2"], "categorie": "Restaurants"}
    code = issue_confirmation(demo.engine, token_id=1, tool="categoriser", params=params, now=NOW)
    for tool, other_params, token, when in (
        ("creer_regle", params, 1, NOW),
        ("categoriser", {**params, "categorie": "Courses"}, 1, NOW),
        ("categoriser", params, 2, NOW),
        ("categoriser", params, 1, NOW + timedelta(minutes=11)),
    ):
        with pytest.raises(ChangeError):
            consume_confirmation(
                demo.engine, token_id=token, tool=tool, params=other_params, code=code, now=when
            )
    consume_confirmation(
        demo.engine,
        token_id=1,
        tool="categoriser",
        params={**params, "confirmation": code},
        code=code,
        now=NOW,
    )
    with pytest.raises(ChangeError):
        consume_confirmation(
            demo.engine, token_id=1, tool="categoriser", params=params, code=code, now=NOW
        )


def test_undo_restores_deleted_encrypted_rows(demo: Demo) -> None:
    """Retraits (et colonnes chiffrées) : l'annulation réinsère la ligne à l'identique."""
    from sqlalchemy import delete  # noqa: PLC0415

    from app.models import Party  # noqa: PLC0415

    before = snapshot(demo.engine)

    def drop_party(engine):  # type: ignore[no-untyped-def]
        with engine.begin() as connection:
            connection.execute(delete(Party))

    changes = tracked(demo.engine, drop_party).changes
    assert [(c.table, c.after) for c in changes] == [("party", None)]
    change_id = save(
        demo.engine, changes, token_id=None, actor_user_id=None, tool="t", summary="s", now=NOW
    )
    assert change_id is not None
    undo(demo.engine, change_id, actor_user_id=None, now=NOW)
    restored = snapshot(demo.engine)
    assert restored["party"] == before["party"]
    with demo.engine.connect() as connection:
        assert connection.scalar(select(Party.name_enc)) == "Alice Martinez"


@pytest.mark.parametrize(
    "stored",
    [
        '[["app_user", 1, null, {"id": 1}]]',  # table non suivie
        '[["rule", 1, null, {"id": 1, "name; DROP TABLE rule": "x"}]]',  # colonne inconnue
        '[["rule; DROP TABLE rule", 1, null, null]]',
    ],
)
def test_undo_journal_never_injects_unknown_names(stored: str) -> None:
    with pytest.raises(ChangeError, match="illisible"):
        _deserialize(stored)
