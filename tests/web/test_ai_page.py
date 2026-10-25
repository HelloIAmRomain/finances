"""Page Sécurité › Accès IA, extension MCP du PC et page « Modifications par l'IA »."""

import io
import json
import zipfile
from collections.abc import Sequence

from sqlalchemy import select

from app.assistant.access import ENABLED, WRITES, authenticate, switches
from app.assistant.changes import save, tracked
from app.categorize.seed import seed_categories
from app.categorize.service import create_rule
from app.models import AiTerm, AuditLog
from tests.web.conftest import PASSWORD, T0, Web


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def test_switches_and_tokens(web: Web) -> None:
    web.login()
    page = web.client.get("/securite").text
    assert "Accès IA depuis le PC" in page and "Aucun jeton." in page
    csrf = _csrf(page)
    web.client.post("/securite/ia/interrupteurs", data={"csrf": csrf, "acces": "1"})
    assert switches(web.engine) == {ENABLED: True, WRITES: False}
    refused = web.client.post(
        "/securite/ia/jetons", data={"csrf": csrf, "mot_de_passe": "x"}, follow_redirects=True
    )
    assert "Identifiants incorrects" in refused.text
    created = web.client.post(
        "/securite/ia/jetons",
        data={"csrf": csrf, "mot_de_passe": PASSWORD, "nom": "PC", "ecriture": "1"},
    )
    assert created.headers["cache-control"] == "no-store"
    token = created.text.split('id="nouveau-jeton">', 1)[1].split("<", 1)[0]
    info = authenticate(web.engine, token, now=T0)
    assert info is not None and info.can_write and info.name == "PC"
    page = web.client.get("/securite").text
    assert token not in page and "lecture + écriture" in page  # plus jamais affiché
    web.client.post(f"/securite/ia/jetons/{info.id}/revoquer", data={"csrf": csrf})
    assert authenticate(web.engine, token, now=T0) is None
    again = web.client.post(
        f"/securite/ia/jetons/{info.id}/revoquer", data={"csrf": csrf}, follow_redirects=True
    )
    assert "déjà révoqué" in again.text


def test_name_lists_are_encrypted_and_previewed(web: Web) -> None:
    web.login()
    csrf = _csrf(web.client.get("/securite").text)
    web.client.post(
        "/securite/ia/termes", data={"csrf": csrf, "type": "personne", "valeur": "Zoé Garnier"}
    )
    duplicate = web.client.post(
        "/securite/ia/termes",
        data={"csrf": csrf, "type": "personne", "valeur": "zoe garnier"},
        follow_redirects=True,
    )
    assert "Déjà dans la liste" in duplicate.text
    with web.engine.connect() as connection:
        raw: bytes = connection.exec_driver_sql("SELECT value_enc FROM ai_term").scalar_one()
        details: Sequence[dict[str, str]] = connection.scalars(
            select(AuditLog.details).where(AuditLog.action == "ai.term_added")
        ).all()
    assert b"Garnier" not in raw and details == [{"type": "personne"}]
    page = web.client.get("/securite").text
    assert "Zoé Garnier" in page
    bad = web.client.post(
        "/securite/ia/termes",
        data={"csrf": csrf, "type": "autre", "valeur": "x"},
        follow_redirects=True,
    )
    assert "Type inconnu" in bad.text
    with web.engine.connect() as connection:
        term_id = connection.scalar(select(AiTerm.id))
    web.client.post(f"/securite/ia/termes/{term_id}/supprimer", data={"csrf": csrf})
    assert "Liste vide." in web.client.get("/securite").text
    missing = web.client.post(
        f"/securite/ia/termes/{term_id}/supprimer", data={"csrf": csrf}, follow_redirects=True
    )
    assert "introuvable" in missing.text


def test_extension_bundle(web: Web) -> None:
    web.login()
    response = web.client.get("/ia/extension.mcpb")
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        assert sorted(bundle.namelist()) == ["manifest.json", "server/index.js"]
        manifest = json.loads(bundle.read("manifest.json"))
    assert manifest["user_config"]["adresse"]["default"] == "https://finances.invalid/mcp"
    assert manifest["user_config"]["jeton"]["sensitive"] is True
    assert len(manifest["tools"]) == 40 and len(manifest["prompts"]) == 6
    web.client.cookies.clear()
    assert web.client.get("/ia/extension.mcpb", follow_redirects=False).status_code == 303


def test_changes_page_lists_and_undoes(web: Web) -> None:
    web.login()
    assert "Aucune modification" in web.client.get("/ia/modifications").text
    seed_categories(web.engine)
    outcome = tracked(
        web.engine,
        lambda engine: create_rule(
            engine,
            name="Test",
            priority=1,
            conditions={"direction": "debit"},
            category_id=1,
            actor_user_id=None,
            now=T0,
        ),
    )
    change_id = save(
        web.engine,
        outcome.changes,
        token_id=None,
        actor_user_id=None,
        tool="creer_regle",
        summary="Règle « Test » créée",
        now=T0,
    )
    page = web.client.get(f"/ia/modifications?detail={change_id}").text
    assert "Règle « Test » créée" in page and "règle : 1 ajout" in page
    csrf = _csrf(page)
    done = web.client.post(
        f"/ia/modifications/{change_id}/annuler", data={"csrf": csrf}, follow_redirects=True
    )
    assert "Modification annulée" in done.text and "annulée le" in done.text
    again = web.client.post(
        f"/ia/modifications/{change_id}/annuler", data={"csrf": csrf}, follow_redirects=True
    )
    assert "déjà annulée" in again.text
    assert web.client.get("/ia/modifications?detail=999").status_code == 200


def test_internal_assistant_token_is_not_listed(web: Web) -> None:
    from app.assistant.access import internal_token, tokens  # noqa: PLC0415

    info = internal_token(web.engine, 1, now=T0)
    assert internal_token(web.engine, 1, now=T0) == info  # une seule fois
    assert tokens(web.engine) == [] and [
        t.id for t in tokens(web.engine, include_internal=True)
    ] == [info.id]
    assert authenticate(web.engine, "interne:assistant-integre", now=T0) is None
