"""Assistant intégré par l'API v1 (application Android) et par la page web « Assistant »."""

from typing import Any

import httpx
from pydantic import SecretStr

from app.assistant.access import ENABLED, set_switch
from app.assistant.runs import RunRegistry
from tests.executors import ManualExecutor
from tests.web.conftest import T0, Web
from tests.web.test_api import _paired


def _reply(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"content": [{"type": "text", "text": "Tout va bien."}], "usage": {}}
    )


def _ready(web: Web) -> None:
    web.app.state.secrets = web.app.state.secrets.model_copy(
        update={"anthropic_api_key": SecretStr("cle-factice")}
    )
    web.app.state.ai_transport = httpx.MockTransport(_reply)
    set_switch(web.engine, ENABLED, True, actor_user_id=1, now=T0)


def test_api_assistant(web: Web) -> None:
    web.login()
    headers = _paired(web)
    web.client.cookies.clear()  # l'application n'a pas de cookie
    refused = web.client.post("/api/v1/assistant", json={"texte": "Bonjour"}, headers=headers)
    assert refused.status_code == 400 and "désactivé" in refused.json()["detail"]
    _ready(web)
    answer: dict[str, Any] = web.client.post(
        "/api/v1/assistant", json={"texte": "Bonjour"}, headers=headers
    ).json()
    assert (
        answer["reponse"] == "Tout va bien."
        and answer["outils"] == []
        and answer["modifications"] == []
    )
    again = web.client.post(
        "/api/v1/assistant",
        json={"texte": "Et ensuite ?", "conversation": answer["conversation"]},
        headers=headers,
    )
    assert again.json()["conversation"] == answer["conversation"]
    assert web.client.post("/api/v1/assistant", json={"texte": "x"}).status_code == 401


def test_web_assistant_page(web: Web) -> None:
    web.login()
    page = web.client.get("/assistant").text
    assert "assistant.js" in page and "Exemples" in page
    csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    refused = web.client.post(
        "/assistant", data={"csrf": csrf, "texte": "Bonjour"}, follow_redirects=True
    )
    assert "désactivé" in refused.text
    _ready(web)
    done = web.client.post(
        "/assistant", data={"csrf": csrf, "texte": "Bonjour"}, follow_redirects=True
    )
    assert "<strong>Vous</strong><p>Bonjour</p>" in done.text and "Tout va bien." in done.text
    assert "Nouvelle conversation" in done.text
    assert "introuvable" in web.client.get("/assistant?conversation=999").text


def test_assistant_settings_form(web: Web) -> None:
    web.login()
    page = web.client.get("/securite").text
    assert "Anthropic — clé absente" in page
    csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    web.client.post(
        "/securite/ia/assistant",
        data={"csrf": csrf, "fournisseur": "openai", "modele": "gpt-test", "plafond": "500000"},
    )
    page = web.client.get("/securite").text
    assert (
        'value="openai" selected' in page
        and 'value="gpt-test"' in page
        and 'value="500000"' in page
    )
    bad = web.client.post(
        "/securite/ia/assistant",
        data={"csrf": csrf, "fournisseur": "autre", "plafond": "1"},
        follow_redirects=True,
    )
    assert "Fournisseur inconnu" in bad.text


def _reply_markdown(request: httpx.Request) -> httpx.Response:
    text = (
        "## Bilan\n\n- **Courses** : 120 €\n\n| Mois | Total |\n|---|---|\n| 09 | 120 € |\n\n"
        "<script>x</script>"
    )
    return httpx.Response(200, json={"content": [{"type": "text", "text": text}], "usage": {}})


def test_web_conversations_markdown_rename_delete(web: Web) -> None:
    web.login()
    _ready(web)
    web.app.state.ai_transport = httpx.MockTransport(_reply_markdown)
    csrf = web.client.get("/assistant").text.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    page = web.client.post(
        "/assistant", data={"csrf": csrf, "texte": "Bilan du mois ?"}, follow_redirects=True
    ).text
    assert "<h2>Bilan</h2>" in page and "<strong>Courses</strong>" in page and "<table>" in page
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
    conversation = page.split('action="/assistant/', 1)[1].split("/", 1)[0]
    assert 'aria-current="page"' in page and "Bilan du mois ?" in page
    web.client.post(
        f"/assistant/{conversation}/renommer", data={"csrf": csrf, "titre": "Septembre"}
    )
    assert "Septembre" in web.client.get(f"/assistant?conversation={conversation}").text
    bad = web.client.post(
        f"/assistant/{conversation}/renommer",
        data={"csrf": csrf, "titre": " "},
        follow_redirects=True,
    )
    assert "Titre vide" in bad.text
    pinned = web.client.post(
        f"/assistant/{conversation}/epingler",
        data={"csrf": csrf, "epingle": "true", "retour": "0"},
        follow_redirects=False,
    )
    assert pinned.headers["location"] == "/assistant"
    page = web.client.get(f"/assistant?conversation={conversation}").text
    assert 'class="epinglee"' in page and "Désépingler" in page
    unpinned = web.client.post(
        f"/assistant/{conversation}/epingler",
        data={"csrf": csrf, "epingle": "false"},
        follow_redirects=False,
    )
    assert unpinned.headers["location"] == f"/assistant?conversation={conversation}"
    assert "Épingler en tête" in web.client.get("/assistant").text
    missing = web.client.post(
        "/assistant/999/epingler", data={"csrf": csrf, "epingle": "true"}, follow_redirects=True
    )
    assert "introuvable" in missing.text
    done = web.client.post(
        f"/assistant/{conversation}/supprimer", data={"csrf": csrf}, follow_redirects=True
    )
    assert "Conversation supprimée" in done.text and "Aucune conversation." in done.text
    again = web.client.post(
        f"/assistant/{conversation}/supprimer", data={"csrf": csrf}, follow_redirects=True
    )
    assert "introuvable" in again.text


def test_api_conversations(web: Web) -> None:
    web.login()
    headers = _paired(web)
    web.client.cookies.clear()
    _ready(web)
    first = web.client.post("/api/v1/assistant", json={"texte": "Bonjour"}, headers=headers).json()
    assert first["titre"] == "Bonjour"
    listed = web.client.get("/api/v1/assistant/conversations", headers=headers).json()
    assert [c["id"] for c in listed] == [first["conversation"]] and listed[0]["titre"] == "Bonjour"
    detail = web.client.get(
        f"/api/v1/assistant/conversations/{first['conversation']}", headers=headers
    ).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1] == {"role": "assistant", "texte": "Tout va bien.", "outils": []}
    renamed = web.client.post(
        f"/api/v1/assistant/conversations/{first['conversation']}/renommer",
        json={"titre": "Salut"},
        headers=headers,
    )
    assert renamed.json()["titre"] == "Salut"
    assert (
        web.client.post(
            f"/api/v1/assistant/conversations/{first['conversation']}/renommer",
            json={"titre": ""},
            headers=headers,
        ).status_code
        == 400
    )
    assert listed[0]["epinglee"] is False and renamed.json()["epinglee"] is False
    pinned = web.client.post(
        f"/api/v1/assistant/conversations/{first['conversation']}/epingler",
        json={"epinglee": True},
        headers=headers,
    )
    assert pinned.status_code == 200 and pinned.json()["epinglee"] is True
    listed = web.client.get("/api/v1/assistant/conversations", headers=headers).json()
    assert listed[0]["epinglee"] is True
    assert (
        web.client.post(
            "/api/v1/assistant/conversations/999/epingler",
            json={"epinglee": True},
            headers=headers,
        ).status_code
        == 404
    )
    assert web.client.delete(
        f"/api/v1/assistant/conversations/{first['conversation']}", headers=headers
    ).json() == {"message": "Conversation supprimée"}
    assert (
        web.client.get(
            f"/api/v1/assistant/conversations/{first['conversation']}", headers=headers
        ).status_code
        == 404
    )
    assert (
        web.client.delete(
            f"/api/v1/assistant/conversations/{first['conversation']}", headers=headers
        ).status_code
        == 404
    )


def _manual(web: Web) -> ManualExecutor:
    executor = ManualExecutor()
    web.app.state.assistant_runs = RunRegistry(executor)
    return executor


def test_web_message_returns_at_once_then_answer_appears(web: Web) -> None:
    web.login()
    _ready(web)
    web.app.state.ai_transport = httpx.MockTransport(_reply_markdown)
    executor = _manual(web)
    csrf = web.client.get("/assistant").text.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    headers = {"X-CSRF-Token": csrf}
    started = web.client.post("/assistant/messages", json={"texte": "Bilan ?"}, headers=headers)
    assert started.status_code == 202
    turn, conversation = started.json()["tour"], started.json()["conversation"]
    page = web.client.get(f"/assistant?conversation={conversation}").text
    assert f'data-tour="{turn}"' in page and "L'assistant réfléchit" in page  # en cours
    assert '<noscript><meta http-equiv="refresh" content="3"></noscript>' in page
    assert "Pas de réponse" not in page  # pas « interrompue » pendant la réponse
    pending = web.client.get(f"/assistant/tours/{turn}").json()
    assert pending["etat"] == "en_cours" and pending["html"] == ""
    busy = web.client.post(
        "/assistant/messages",
        json={"texte": "Encore", "conversation": conversation},
        headers=headers,
    )
    assert busy.status_code == 409 and "en cours" in busy.json()["erreur"]
    executor.run_all()
    done = web.client.get(f"/assistant/tours/{turn}?depuis=0").json()
    assert done["etat"] == "termine" and "<h2>Bilan</h2>" in done["html"]
    assert "&lt;script&gt;" in done["html"] and "<script>" not in done["html"]
    assert [e["type"] for e in done["evenements"]] == ["fin"]
    assert web.client.get("/assistant/tours/inconnu").status_code == 404
    assert (
        web.client.post("/assistant/messages", json={"texte": " "}, headers=headers).status_code
        == 400
    )
    assert web.client.post("/assistant/messages", content=b"x", headers=headers).status_code == 400
    assert web.client.post("/assistant/messages", json={"texte": "x"}).status_code == 403  # CSRF


def test_web_form_without_javascript_does_not_wait(web: Web) -> None:
    web.login()
    _ready(web)
    executor = _manual(web)
    csrf = web.client.get("/assistant").text.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    sent = web.client.post(
        "/assistant", data={"csrf": csrf, "texte": "Bonjour"}, follow_redirects=False
    )
    assert sent.status_code == 303
    page = web.client.get(sent.headers["location"]).text
    assert "<p>Bonjour</p>" in page and "data-tour=" in page
    again = web.client.post(
        "/assistant",
        data={
            "csrf": csrf,
            "texte": "Encore",
            "conversation": page.split('name="conversation" value="', 1)[1].split('"', 1)[0],
        },
        follow_redirects=True,
    )
    assert "Réponse en cours" in again.text
    executor.run_all()
    assert "Tout va bien." in web.client.get(sent.headers["location"]).text


def test_api_async_turn(web: Web) -> None:
    web.login()
    headers = _paired(web)
    web.client.cookies.clear()
    _ready(web)
    executor = _manual(web)
    started = web.client.post(
        "/api/v1/assistant/messages", json={"texte": "Bonjour"}, headers=headers
    )
    assert started.status_code == 202 and started.json()["titre"] == "Bonjour"
    turn = started.json()["tour"]
    assert (
        web.client.get(f"/api/v1/assistant/tours/{turn}", headers=headers).json()["etat"]
        == "en_cours"
    )
    busy = web.client.post(
        "/api/v1/assistant/messages",
        json={"texte": "x", "conversation": started.json()["conversation"]},
        headers=headers,
    )
    assert busy.status_code == 409
    executor.run_all()
    done = web.client.get(f"/api/v1/assistant/tours/{turn}", headers=headers).json()
    assert done["etat"] == "termine" and done["reponse"] == "Tout va bien." and "html" not in done
    assert web.client.get("/api/v1/assistant/tours/inconnu", headers=headers).status_code == 404
    refused = web.client.post("/api/v1/assistant/messages", json={"texte": ""}, headers=headers)
    assert refused.status_code == 400


def test_live_answer_setting(web: Web) -> None:
    from app.assistant.access import STREAM, assistant_settings  # noqa: PLC0415

    web.login()
    page = web.client.get("/securite").text
    assert (
        "Afficher la réponse au fur et à mesure" in page
        and 'name="en_direct" value="1" checked' in page
    )
    csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    web.client.post(
        "/securite/ia/assistant", data={"csrf": csrf, "fournisseur": "anthropic", "plafond": "1000"}
    )
    assert assistant_settings(web.engine)[STREAM] is False


def test_web_delete_from_list_keeps_the_open_conversation(web: Web) -> None:
    web.login()
    _ready(web)
    web.app.state.ai_transport = httpx.MockTransport(_reply_markdown)
    csrf = web.client.get("/assistant").text.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    ids = []
    for question in ("Première ?", "Seconde ?"):
        page = web.client.post(
            "/assistant", data={"csrf": csrf, "texte": question}, follow_redirects=False
        )
        ids.append(int(page.headers["location"].split("conversation=", 1)[1].split("#")[0]))
    done = web.client.post(
        f"/assistant/{ids[0]}/supprimer",
        data={"csrf": csrf, "retour": str(ids[1])},
        follow_redirects=False,
    )
    assert f"conversation={ids[1]}" in done.headers["location"]
