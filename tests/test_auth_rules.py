"""Bornes de l'authentification et de l'appairage : durées, seuils, ordre des codes (NF-SEC-04).

Chaque test vise une limite exacte (à la seconde ou au caractère près) : une erreur d'une
unité dans une comparaison ouvrirait une session ou un appairage qui devrait être refusé.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from argon2 import PasswordHasher
from sqlalchemy import Engine, select, update

from app.auth import users
from app.auth.devices import (
    CODE_LIFETIME,
    FAILURE_WINDOW,
    IDLE_EXPIRY,
    MAX_FAILURES,
    MAX_NAME,
    PairingError,
    create_pairing_code,
    device_user,
    devices,
    pair,
    revoke_device,
)
from app.auth.users import (
    ABSOLUTE_TIMEOUT,
    IDLE_TIMEOUT,
    LOCK_WINDOW,
    MIN_PASSWORD_LENGTH,
    AuthError,
    LockedError,
    authenticate,
    close_session,
    create_user,
    open_session,
    record_failure,
    session_user,
)
from app.models import AppUser, Device

T0 = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
NAME = "admin"
PASSWORD = "phrase-de-passe-de-test"  # noqa: S105 (factice)


def _user(engine: Engine) -> tuple[int, str]:
    secret = create_user(engine, username=NAME, password=PASSWORD, now=T0)
    with engine.connect() as connection:
        return connection.execute(select(AppUser.id)).scalar_one(), secret


def _login(engine: Engine, secret: str, now: datetime, offset: int = 0) -> int:
    code = pyotp.TOTP(secret).at(now, counter_offset=offset)
    return authenticate(engine, username=NAME, password=PASSWORD, totp_code=code, now=now)


# --- Mot de passe ---------------------------------------------------------------------------


def test_password_length_boundary(engine: Engine) -> None:
    with pytest.raises(ValueError, match="trop court"):
        create_user(engine, username="a", password="x" * (MIN_PASSWORD_LENGTH - 1), now=T0)
    create_user(engine, username="b", password="x" * MIN_PASSWORD_LENGTH, now=T0)
    assert MIN_PASSWORD_LENGTH == 12  # recommandation ANSSI pour un mot de passe seul facteur


def test_old_password_hash_is_upgraded_at_login(engine: Engine) -> None:
    user_id, secret = _user(engine)
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    with engine.begin() as connection:
        connection.execute(update(AppUser).values(password_hash=weak))
    _login(engine, secret, T0)
    with engine.connect() as connection:
        user = connection.execute(select(AppUser).where(AppUser.id == user_id)).one()
    assert user.password_hash != weak and not users.HASHER.check_needs_rehash(user.password_hash)
    assert user.last_login_at == T0


def test_login_without_password_never_rehashes(engine: Engine) -> None:
    """Connexion par clé de sécurité : pas de mot de passe saisi, rien à recalculer."""
    _user(engine)
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    with engine.begin() as connection:
        connection.execute(update(AppUser).values(password_hash=weak))
    with engine.connect() as connection:
        user = connection.execute(select(AppUser)).one()
    users.record_success(engine, user, password=None, method="webauthn", now=T0)
    with engine.connect() as connection:
        assert connection.execute(select(AppUser.password_hash)).scalar_one() == weak


def test_lockout_window_boundary(engine: Engine) -> None:
    _, secret = _user(engine)
    for _ in range(users.MAX_FAILURES):
        record_failure(engine, NAME, now=T0)
    with pytest.raises(LockedError):
        _login(engine, secret, T0 + LOCK_WINDOW - timedelta(seconds=1))
    assert _login(engine, secret, T0 + LOCK_WINDOW)  # échecs vieux de 15 min : oubliés
    assert LOCK_WINDOW == timedelta(minutes=15)  # annoncé dans le message de blocage


def test_parallel_guesses_never_exceed_the_lockout(engine: Engine) -> None:
    """20 essais simultanés : 5 vérifiés au plus, les autres bloqués (le compteur ne peut pas
    être lu par tous avant qu'un échec soit enregistré)."""
    _user(engine)
    verified = 0

    def guess(_: int) -> str:
        nonlocal verified
        try:
            users.check_password(engine, username=NAME, password="mauvais", now=T0)  # noqa: S106
        except LockedError:
            return "bloqué"
        except AuthError:
            verified += 1
            return "refusé"
        return "accepté"

    with ThreadPoolExecutor(max_workers=20) as pool:
        outcomes = list(pool.map(guess, range(20)))
    assert verified == users.MAX_FAILURES
    assert outcomes.count("bloqué") == 20 - users.MAX_FAILURES and "accepté" not in outcomes


# --- Code TOTP ------------------------------------------------------------------------------


def test_code_of_the_next_step_burns_the_current_one(engine: Engine) -> None:
    """Un code « en avance » (horloge du téléphone) consomme son pas : le code courant, plus
    ancien, ne peut plus servir ensuite."""
    _, secret = _user(engine)
    _login(engine, secret, T0, offset=1)
    with pytest.raises(AuthError):
        _login(engine, secret, T0)


def test_totp_without_secret_is_refused() -> None:
    assert users._totp_step(None, "123456", T0) is None


# --- Sessions -------------------------------------------------------------------------------


def test_session_idle_boundary_and_extension(engine: Engine) -> None:
    user_id, _ = _user(engine)
    token = open_session(engine, user_id, now=T0)
    assert session_user(engine, token, now=T0 + IDLE_TIMEOUT) == user_id  # pile 12 h : encore
    later = T0 + IDLE_TIMEOUT + IDLE_TIMEOUT  # 12 h après la dernière activité
    assert session_user(engine, token, now=later) == user_id
    assert session_user(engine, token, now=later + IDLE_TIMEOUT + timedelta(seconds=1)) is None


def test_session_absolute_boundary(engine: Engine) -> None:
    user_id, _ = _user(engine)
    token = open_session(engine, user_id, now=T0)
    now = T0
    while now + IDLE_TIMEOUT < T0 + ABSOLUTE_TIMEOUT:
        now += IDLE_TIMEOUT
        assert session_user(engine, token, now=now) == user_id
    assert session_user(engine, token, now=T0 + ABSOLUTE_TIMEOUT - timedelta(seconds=1))
    assert session_user(engine, token, now=T0 + ABSOLUTE_TIMEOUT) is None


def test_logout_closes_only_its_own_session(engine: Engine) -> None:
    user_id, _ = _user(engine)
    laptop, phone = (open_session(engine, user_id, now=T0) for _ in range(2))
    close_session(engine, laptop, now=T0)
    assert session_user(engine, laptop, now=T0) is None  # cookie rejoué : refusé
    assert session_user(engine, phone, now=T0) == user_id


# --- Appairage d'un téléphone ---------------------------------------------------------------


def _code(engine: Engine, user_id: int, now: datetime = T0) -> str:
    return create_pairing_code(engine, user_id=user_id, password=PASSWORD, now=now)


def test_new_pairing_code_cancels_the_previous_one(engine: Engine) -> None:
    user_id, _ = _user(engine)
    first = _code(engine, user_id)
    second = _code(engine, user_id)
    with pytest.raises(PairingError, match="expiré"):
        pair(engine, code=first, device_name="Pixel", now=T0)
    assert pair(engine, code=second, device_name="Pixel", now=T0).token


def test_pairing_code_lifetime_boundary(engine: Engine) -> None:
    user_id, _ = _user(engine)
    code = _code(engine, user_id)
    with pytest.raises(PairingError):
        pair(engine, code=code, device_name="Pixel", now=T0 + CODE_LIFETIME)
    code = _code(engine, user_id)
    pair(engine, code=code, device_name="Pixel", now=T0 + CODE_LIFETIME - timedelta(seconds=1))
    assert CODE_LIFETIME == timedelta(minutes=10)  # annoncé sur la page « Appareils »


def test_pairing_brute_force_boundary(engine: Engine) -> None:
    assert (MAX_FAILURES, FAILURE_WINDOW) == (10, timedelta(minutes=10))  # cf. docstring du module
    user_id, _ = _user(engine)
    for _ in range(MAX_FAILURES - 1):
        with pytest.raises(PairingError, match="inconnu"):
            pair(engine, code="AAAA-AAAA", device_name="x", now=T0)
    code = _code(engine, user_id)
    pair(engine, code=code, device_name="Pixel", now=T0)  # 9 échecs : encore permis
    with pytest.raises(PairingError, match="inconnu"):
        pair(engine, code="AAAA-AAAA", device_name="x", now=T0)
    code = _code(engine, user_id)
    with pytest.raises(PairingError, match="Trop d'essais"):
        pair(engine, code=code, device_name="Pixel", now=T0 + FAILURE_WINDOW - timedelta(seconds=1))
    later = T0 + FAILURE_WINDOW  # échecs vieux de 10 min : oubliés
    pair(engine, code=_code(engine, user_id, later), device_name="Pixel", now=later)


def test_device_name_is_cleaned_and_truncated(engine: Engine) -> None:
    user_id, _ = _user(engine)
    long = "  Pixel   " + "x" * 100
    pair(engine, code=_code(engine, user_id), device_name=long, now=T0)
    pair(engine, code=_code(engine, user_id), device_name="   ", now=T0)
    with engine.connect() as connection:
        names = list(connection.scalars(select(Device.name).order_by(Device.id)))
    assert names == [("Pixel " + "x" * 100)[:MAX_NAME], "Téléphone"]
    assert len(names[0]) == MAX_NAME == 64


def test_device_last_seen_is_refreshed_at_most_every_five_minutes(engine: Engine) -> None:
    user_id, _ = _user(engine)
    paired = pair(engine, code=_code(engine, user_id), device_name="Pixel", now=T0)

    def seen() -> datetime | None:
        with engine.connect() as connection:
            return connection.execute(select(Device.last_seen_at)).scalar_one()

    device_user(engine, paired.token, now=T0 + timedelta(minutes=5))
    assert seen() == T0  # pile 5 min : pas d'écriture
    device_user(engine, paired.token, now=T0 + timedelta(minutes=5, seconds=1))
    assert seen() == T0 + timedelta(minutes=5, seconds=1)


def test_device_idle_expiry_boundary(engine: Engine) -> None:
    assert IDLE_EXPIRY == timedelta(days=90)  # annoncé sur la page « Appareils »
    user_id, _ = _user(engine)
    kept = pair(engine, code=_code(engine, user_id), device_name="Pixel", now=T0)
    lost = pair(engine, code=_code(engine, user_id), device_name="Ancien", now=T0)
    assert device_user(engine, kept.token, now=T0 + IDLE_EXPIRY) == (kept.device_id, user_id)
    later = T0 + IDLE_EXPIRY + timedelta(seconds=1)
    assert device_user(engine, lost.token, now=later) is None
    revoke_device(engine, lost.device_id, actor_user_id=user_id, now=later)
    far = T0 + 3 * IDLE_EXPIRY
    state = {d.name: d.expired(far) for d in devices(engine)}
    assert state == {"Pixel": True, "Ancien": False}  # révoqué : affiché comme tel, pas expiré
