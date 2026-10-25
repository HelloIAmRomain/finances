"""Messages affichés après une action : un lien piégé ne peut pas en fabriquer."""

from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

from hypothesis import given
from hypothesis import strategies as st
from starlette.requests import Request

from app.web.flash import SEAL_PARAM, flash, seal_location
from tests.web.bank_helpers import csrf_of
from tests.web.conftest import Web, fake_secrets

FORGED = "Votre clé est compromise : appelez le 01 23 45 67 89"
APP = SimpleNamespace(state=SimpleNamespace(secrets=fake_secrets()))


def _request(location: str) -> Request:
    query = urlsplit(location).query.encode()
    return Request({"type": "http", "app": APP, "query_string": query, "headers": []})


def test_forged_message_is_not_shown(web: Web) -> None:
    web.login()
    for page in ("/securite", "/comptes", "/appareils", "/budgets", "/alertes"):
        assert "appelez le" not in web.client.get(f"{page}?message={FORGED}").text, page
        fake_seal = f"{page}?message={FORGED}&{SEAL_PARAM}={'0' * 32}"
        assert "appelez le" not in web.client.get(fake_seal).text, page


def test_messages_from_the_application_are_sealed_and_shown(web: Web) -> None:
    web.login()
    csrf = csrf_of(web.client.get("/appareils").text)
    response = web.client.post(
        "/appareils/code", data={"csrf": csrf, "mot_de_passe": "faux"}, follow_redirects=False
    )
    location = response.headers["location"]
    assert SEAL_PARAM in parse_qs(urlsplit(location).query)
    assert "Identifiants incorrects" in web.client.get(location).text


def test_seal_covers_every_message_and_keeps_the_anchor() -> None:
    sealed = seal_location(_request("/"), "/securite?message=cl%C3%A9+retir%C3%A9e#cles")
    assert sealed.endswith("#cles")
    assert flash(_request(sealed)) == "clé retirée"
    assert flash(_request(sealed.replace("retir", "ajout"))) is None
    with_error = seal_location(_request("/"), "/assistant?erreur=panne&conversation=3")
    assert flash(_request(with_error), "erreur") == "panne"
    assert flash(_request(with_error + "&message=ajout")) is None  # message ajouté après coup
    assert seal_location(_request("/"), "/comptes") == "/comptes"
    for outside in ("https://ailleurs.example/?message=x", "//ailleurs.example/?message=x"):
        assert seal_location(_request("/"), outside) == outside  # jamais de sceau hors du site


@given(st.text(max_size=60))
def test_any_text_round_trips_once_sealed(text: str) -> None:
    sealed = seal_location(_request("/"), "/comptes?" + urlencode({"message": text}))
    assert flash(_request(sealed)) == text
