"""Clés de sécurité FIDO2 / WebAuthn (NF-SEC-04) : second facteur au choix avec le TOTP."""

import copy
import json
from datetime import timedelta

import pytest
from sqlalchemy import Engine, select

from app.auth.security_keys import (
    RelyingParty,
    SecurityKeyError,
    keys,
    login,
    login_options,
    register,
    registration_options,
    remove,
)
from app.auth.users import MAX_FAILURES, AuthError, LockedError, create_user
from app.models import AuditLog, SecurityKey
from tests.web.conftest import T0
from tests.web.webauthn_helpers import SoftKey

RP = RelyingParty.from_base_url("https://pi.exemple.ts.net")
PASSWORD = "phrase-de-passe-de-test"  # noqa: S105 (factice)
WRONG = "mauvais-mot-de-passe"


@pytest.fixture
def user(engine: Engine) -> int:
    create_user(engine, username="admin", password=PASSWORD, now=T0)
    with engine.connect() as connection:
        from app.models import AppUser  # noqa: PLC0415

        return int(connection.scalar(select(AppUser.id)) or 0)


def _enrol(engine: Engine, user: int, key: SoftKey, name: str = "YubiKey noire") -> int:
    options = registration_options(engine, user_id=user, password=PASSWORD, rp=RP, now=T0)
    return register(
        engine, user_id=user, credential=key.create(options, RP.origin), name=name, rp=RP, now=T0
    )


def test_register_then_login_with_the_key(engine: Engine, user: int) -> None:
    key = SoftKey()
    key_id = _enrol(engine, user, key)
    assert [k.name for k in keys(engine, user)] == ["YubiKey noire"]
    options = login_options(engine, username="admin", password=PASSWORD, rp=RP, now=T0)
    assert json.loads(options)["allowCredentials"][0]["id"]
    assert login(engine, credential=key.get(options, RP.origin), rp=RP, now=T0) == user
    with engine.connect() as connection:
        stored = connection.execute(select(SecurityKey).where(SecurityKey.id == key_id)).one()
        audit = [
            (a.action, a.details.get("facteur"))
            for a in connection.execute(select(AuditLog).order_by(AuditLog.id))
        ]
    assert stored.sign_count == 1 and stored.last_used_at is not None
    assert ("security_key.registered", None) in audit and ("login.success", "cle") in audit


def test_replayed_or_expired_or_misused_challenges_are_refused(engine: Engine, user: int) -> None:
    key = SoftKey()
    _enrol(engine, user, key)
    options = login_options(engine, username="admin", password=PASSWORD, rp=RP, now=T0)
    answer = key.get(options, RP.origin)
    login(engine, credential=answer, rp=RP, now=T0)
    with pytest.raises(SecurityKeyError, match="déjà utilisée"):
        login(engine, credential=answer, rp=RP, now=T0)  # rejeu
    late = login_options(engine, username="admin", password=PASSWORD, rp=RP, now=T0)
    with pytest.raises(SecurityKeyError, match="expirée"):
        login(engine, credential=key.get(late, RP.origin), rp=RP, now=T0 + timedelta(minutes=6))
    enrolment = registration_options(engine, user_id=user, password=PASSWORD, rp=RP, now=T0)
    fresh = json.loads(login_options(engine, username="admin", password=PASSWORD, rp=RP, now=T0))
    fresh["challenge"] = json.loads(enrolment)["challenge"]  # défi d'inscription détourné
    with pytest.raises(SecurityKeyError, match="expirée ou déjà utilisée"):
        login(engine, credential=key.get(json.dumps(fresh), RP.origin), rp=RP, now=T0)


def test_phishing_origin_and_cloned_key_are_refused(engine: Engine, user: int) -> None:
    key = SoftKey()
    _enrol(engine, user, key)
    options = login_options(engine, username="admin", password=PASSWORD, rp=RP, now=T0)
    with pytest.raises(SecurityKeyError, match="refusée"):
        login(engine, credential=key.get(options, "https://faux-site.example"), rp=RP, now=T0)
    options = login_options(engine, username="admin", password=PASSWORD, rp=RP, now=T0)
    good = key.get(options, RP.origin)
    clone = copy.deepcopy(key)
    login(engine, credential=good, rp=RP, now=T0)
    clone.device.sign_count = 0  # compteur en retard : clé dupliquée
    options = login_options(engine, username="admin", password=PASSWORD, rp=RP, now=T0)
    with pytest.raises(SecurityKeyError, match="refusée"):
        login(engine, credential=clone.get(options, RP.origin), rp=RP, now=T0)


def test_password_is_still_required_and_throttled(engine: Engine, user: int) -> None:
    _enrol(engine, user, SoftKey())
    for _ in range(MAX_FAILURES):
        with pytest.raises(AuthError):
            login_options(engine, username="admin", password=WRONG, rp=RP, now=T0)
    with pytest.raises(LockedError):
        login_options(engine, username="admin", password=PASSWORD, rp=RP, now=T0)
    with pytest.raises(SecurityKeyError, match="Identifiants"):
        registration_options(
            engine, user_id=user, password=WRONG, rp=RP, now=T0 + timedelta(hours=1)
        )


def test_keys_can_be_removed_and_not_registered_twice(engine: Engine, user: int) -> None:
    key = SoftKey()
    key_id = _enrol(engine, user, key)
    options = registration_options(engine, user_id=user, password=PASSWORD, rp=RP, now=T0)
    assert json.loads(options)["excludeCredentials"]  # le navigateur refusera la même clé
    with pytest.raises(SecurityKeyError, match="Aucune clé"):
        remove(engine, user_id=user, key_id=key_id, now=T0)
        login_options(engine, username="admin", password=PASSWORD, rp=RP, now=T0)
    with pytest.raises(SecurityKeyError, match="introuvable"):
        remove(engine, user_id=user, key_id=key_id, now=T0)


def test_relying_party_requires_https() -> None:
    assert RelyingParty.from_base_url("https://pi.exemple.ts.net:8443/").origin == (
        "https://pi.exemple.ts.net:8443"
    )
    with pytest.raises(SecurityKeyError, match="HTTPS"):
        RelyingParty.from_base_url("http://pi.exemple.ts.net")


def test_enrolment_refusals(engine: Engine, user: int) -> None:
    create_user(engine, username="autre", password=PASSWORD, now=T0)
    with engine.connect() as connection:
        from app.models import AppUser  # noqa: PLC0415

        other = int(connection.scalar(select(AppUser.id).where(AppUser.username == "autre")) or 0)
    key = SoftKey()
    options = registration_options(engine, user_id=user, password=PASSWORD, rp=RP, now=T0)
    with pytest.raises(SecurityKeyError, match="autre utilisateur"):  # défi d'un autre compte
        register(
            engine, user_id=other, credential=key.create(options, RP.origin), name="x", rp=RP,
            now=T0,
        )  # fmt: skip
    options = registration_options(engine, user_id=user, password=PASSWORD, rp=RP, now=T0)
    with pytest.raises(SecurityKeyError, match="Clé refusée"):  # page d'hameçonnage
        register(
            engine, user_id=user, credential=key.create(options, "https://faux-site.example"),
            name="x", rp=RP, now=T0,
        )  # fmt: skip
    _enrol(engine, user, key)
    key.device.cred_init = lambda *_: None  # la même clé physique, présentée de nouveau
    with pytest.raises(SecurityKeyError, match="déjà enregistrée"):
        _enrol(engine, user, key)
    assert len(keys(engine, user)) == 1


def test_login_with_a_key_of_another_account_is_refused_and_counted(
    engine: Engine, user: int
) -> None:
    _enrol(engine, user, SoftKey())
    stranger = SoftKey()
    options = login_options(engine, username="admin", password=PASSWORD, rp=RP, now=T0)
    stranger.create(registration_options(engine, user_id=user, password=PASSWORD, rp=RP, now=T0),
                    RP.origin)  # fmt: skip
    with pytest.raises(SecurityKeyError, match="Clé inconnue"):
        login(engine, credential=stranger.get(options, RP.origin), rp=RP, now=T0)
    with engine.connect() as connection:
        actions = list(connection.scalars(select(AuditLog.action)))
    assert actions.count("login.failed") == 1  # compté par l'anti-force brute
