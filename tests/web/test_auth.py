"""Authentification forte, sessions, CSRF et journal des connexions (NF-SEC-04, NF-SEC-06)."""

from datetime import timedelta
from urllib.parse import urljoin, urlsplit

import pyotp
import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Engine, select

from app.auth.users import AuthError, close_session, confirm_password, create_user, totp_uri
from app.models import AppUser, AuditLog
from app.web.routes.auth import safe_next
from tests.web.conftest import PASSWORD, T0, USERNAME, Web


def _actions(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return list(connection.scalars(select(AuditLog.action).order_by(AuditLog.id)))


def test_pages_require_login(web: Web) -> None:
    response = web.client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/connexion?suivant=/"


def test_health_stays_public(web: Web) -> None:
    assert web.client.get("/health").json() == {"status": "ok"}


def test_login_sets_a_hardened_session_cookie(web: Web) -> None:
    response = web.login()
    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert web.client.get("/").status_code == 200
    assert _actions(web.engine)[-1] == "login.success"


@pytest.mark.parametrize("wrong", ["password", "code"])
def test_wrong_factor_gives_one_generic_error(web: Web, wrong: str) -> None:
    kwargs = {"password": "mauvais-mot-de-passe"} if wrong == "password" else {"code": "000000"}
    response = web.login(**kwargs)
    assert response.status_code == 401
    assert "Identifiants incorrects." in response.text
    assert _actions(web.engine)[-1] == "login.failed"


def test_unknown_user_is_indistinguishable(web: Web) -> None:
    response = web.client.post(
        "/connexion",
        data={"identifiant": "inconnu", "mot_de_passe": PASSWORD, "code": web.code()},
    )
    assert response.status_code == 401
    assert "Identifiants incorrects." in response.text


def test_lockout_after_five_failures_then_release(web: Web) -> None:
    for _ in range(5):
        web.login(code="000000")
    blocked = web.login()
    assert blocked.status_code == 401 and "15 minutes" in blocked.text
    web.clock.current += timedelta(minutes=16)
    assert web.login().status_code == 303


def test_idle_and_absolute_expiry(web: Web) -> None:
    web.login()
    web.clock.current += timedelta(hours=11)
    assert web.client.get("/").status_code == 200  # activité : la session est prolongée
    web.clock.current += timedelta(hours=12, minutes=1)
    assert web.client.get("/", follow_redirects=False).status_code == 303
    web.clock.current = T0
    web.login()
    for _ in range(16):  # activité régulière (176 h), mais 7 jours au plus
        web.clock.current += timedelta(hours=11)
        web.client.get("/")
    assert web.client.get("/", follow_redirects=False).status_code == 303


def test_logout_requires_csrf_and_revokes(web: Web) -> None:
    web.login()
    assert web.client.post("/deconnexion", data={"csrf": "faux"}).status_code == 403
    page = web.client.get("/").text
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    response = web.client.post("/deconnexion", data={"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert web.client.get("/", follow_redirects=False).status_code == 303
    assert _actions(web.engine)[-1] == "logout"


def test_open_redirect_is_refused(web: Web) -> None:
    for target in ("https://evil.example/", "//evil.example/", "/\\evil", "/\t/evil.example"):
        assert web.login(suivant=target).headers["location"] == "/"
        web.client.cookies.clear()


@pytest.mark.parametrize(
    "target",
    ["/\t/evil.example", "/\n/evil.example", "/\r/evil.example", "/ /evil", "/\x7f", "/\x00x"],
)
def test_safe_next_refuses_control_characters(target: str) -> None:
    assert safe_next(target) == "/"


def test_safe_next_keeps_internal_paths() -> None:
    assert safe_next("/loyers/3?mois=2026-10#bail") == "/loyers/3?mois=2026-10#bail"
    assert safe_next("/catégories") == "/catégories"
    assert safe_next("/a!b~c") == "/a!b~c"  # premier et dernier caractères imprimables


def test_login_link_cannot_carry_a_page_message() -> None:
    """Lien piégé « /connexion?suivant=/securite?message=… » : après connexion, le texte serait
    scellé puis affiché comme un avertissement de l'application. Il est retiré."""
    target = "/securite?message=Votre+clé+est+compromise&onglet=cles&erreur=x&sceau=0#cles"
    assert safe_next(target) == "/securite?onglet=cles#cles"


@given(st.text(max_size=40))
def test_safe_next_never_leaves_the_site(target: str) -> None:
    # Comme un navigateur (WHATWG) : tabulations et retours à la ligne retirés, puis résolution.
    cleaned = "".join(char for char in safe_next(target) if char not in "\t\n\r")
    resolved = urlsplit(urljoin("https://pi.example/", cleaned))
    assert (resolved.scheme, resolved.netloc) == ("https", "pi.example")


def test_totp_code_cannot_be_replayed(web: Web) -> None:
    code = web.code()
    assert web.login(code=code).status_code == 303
    web.client.cookies.clear()
    replay = web.client.post(
        "/connexion",
        data={"identifiant": USERNAME, "mot_de_passe": PASSWORD, "code": code},
        follow_redirects=False,
    )
    assert replay.status_code == 401
    web.clock.current += timedelta(seconds=30)  # code suivant : accepté
    next_code = pyotp.TOTP(web.totp_secret).at(web.clock.current)
    response = web.client.post(
        "/connexion",
        data={"identifiant": USERNAME, "mot_de_passe": PASSWORD, "code": next_code},
        follow_redirects=False,
    )
    assert response.status_code == 303


@pytest.mark.parametrize("offset", [-1, 1])
def test_totp_tolerates_one_step_of_clock_drift(web: Web, offset: int) -> None:
    code = pyotp.TOTP(web.totp_secret).at(web.clock.current, counter_offset=offset)
    assert web.login(code=code).status_code == 303


@pytest.mark.parametrize("code", ["abcdef", "12345", "1234567", "12 34 5"])
def test_malformed_totp_codes_are_refused(web: Web, code: str) -> None:
    assert web.login(code=code).status_code == 401


def test_passwords_hashed_and_totp_secret_encrypted(web: Web) -> None:
    with web.engine.connect() as connection:
        user = connection.execute(select(AppUser.password_hash)).scalar_one()
        raw: bytes = connection.exec_driver_sql("SELECT totp_secret_enc FROM app_user").scalar_one()
    assert user.startswith("$argon2id$")
    assert web.totp_secret.encode() not in raw


def test_create_user_rules(engine: Engine) -> None:
    with pytest.raises(ValueError, match="trop court"):
        create_user(engine, username="x", password="court", now=T0)  # noqa: S106
    create_user(engine, username="admin", password=PASSWORD, now=T0)
    with pytest.raises(ValueError, match="existe déjà"):
        create_user(engine, username="admin", password=PASSWORD, now=T0)


def test_totp_uri_for_authenticator_apps() -> None:
    uri = totp_uri(USERNAME, pyotp.random_base32())
    assert uri.startswith("otpauth://totp/Finances:admin?") and "issuer=Finances" in uri


def test_logout_without_or_with_unknown_session_is_harmless(engine: Engine) -> None:
    close_session(engine, None, now=T0)
    close_session(engine, "jeton-inconnu", now=T0)  # rien à fermer, pas d'erreur
    with pytest.raises(AuthError):
        confirm_password(engine, user_id=999, password=PASSWORD, now=T0)


def test_unauthenticated_htmx_requests_are_sent_to_the_login_page(web: Web) -> None:
    response = web.client.get(
        "/transactions", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert response.status_code == 401 and response.headers["HX-Redirect"] == "/connexion"
