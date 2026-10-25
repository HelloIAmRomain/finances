from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select

import app.models  # noqa: F401
from app.assistant.access import authenticate, create_token, internal_token
from app.auth.devices import create_pairing_code, device_user, pair
from app.auth.users import create_user, open_session, session_user
from app.cli import main
from app.connectors.budget import acquire, record_response
from app.db import Base, make_engine
from app.models import Account, Category
from app.models.ledger import AccountType
from app.timeutil import utcnow


def test_seed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "cli.db"
    Base.metadata.create_all(make_engine(db))
    monkeypatch.setenv("FINANCES_DATABASE_PATH", str(db))
    main(["seed"])
    main(["seed"])
    assert capsys.readouterr().out.splitlines()[-1] == "0 catégorie(s) créée(s)."
    with make_engine(db).connect() as connection:
        assert connection.execute(select(func.count()).select_from(Category)).scalar_one() > 20


def test_unknown_command_fails() -> None:
    with pytest.raises(SystemExit):
        main(["inconnue"])


def test_observed_limits_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "cli.db"
    engine = make_engine(db)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("FINANCES_DATABASE_PATH", str(db))
    main(["limites-observees"])
    assert capsys.readouterr().out.startswith("Aucun refus de quota")
    with engine.begin() as connection:
        connection.execute(
            insert(Account).values(
                id=1, name="Compte", type=AccountType.CHECKING, include_in_net_worth=True
            )
        )
    for minutes in (0, 30):
        call = acquire(
            engine,
            account_id=1,
            endpoint="transactions",
            unattended=True,
            method="GET",
            now=utcnow() - timedelta(minutes=60 - minutes),
        )
    record_response(engine, call, 429)
    main(["limites-observees"])
    assert "transactions  compte 1 : refus après 2 requête(s) sur 24 h" in capsys.readouterr().out


def test_close_access_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Vol ou fuite (docs/runbook-vol.md) : sessions, téléphones et accès IA fermés d'un coup ;
    changer `app_secret_key` ne suffit pas (les sessions ne dépendent pas de cette clé)."""
    db = tmp_path / "cli.db"
    engine = make_engine(db)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("FINANCES_DATABASE_PATH", str(db))
    now = utcnow()
    password = "phrase-de-passe-de-test"  # noqa: S105 (factice)
    create_user(engine, username="admin", password=password, now=now)
    user_id = 1
    session = open_session(engine, user_id, now=now)
    code = create_pairing_code(engine, user_id=user_id, password=password, now=now)
    phone = pair(engine, code=code, device_name="Pixel", now=now)
    ai = create_token(
        engine, user_id=user_id, password=password, name="PC", can_write=False, now=now
    )
    internal = internal_token(engine, user_id, now=now)
    main(["fermer-acces"])
    assert "1 session(s), 1 téléphone(s), 1 accès IA" in capsys.readouterr().out
    assert session_user(engine, session, now=now) is None
    assert device_user(engine, phone.token, now=now) is None
    assert authenticate(engine, ai, now=now) is None
    assert internal_token(engine, user_id, now=now) == internal  # jeton interne : inchangé
