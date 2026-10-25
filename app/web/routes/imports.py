"""Page « Import » : envoi d'un relevé, aperçu, confirmation (COL-02)."""

import base64
import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from app.connectors.file_import.profiles import BUILTIN_PROFILES
from app.ledger.imports import MAX_FILE_BYTES, OFX_FORMAT, ImportFileError, import_file, read_pages
from app.models import Account
from app.web.deps import CurrentUser, current_user, engine_of, now_of, templates_of

router = APIRouter()
PREVIEW_ROWS = 20
FORMATS = {OFX_FORMAT: "OFX (tous établissements)"} | {
    name: f"CSV {profile.institution}" for name, profile in BUILTIN_PROFILES.items()
}


def _render(
    request: Request, template: str, context: dict[str, object], status: int = 200
) -> Response:
    templates = templates_of(request)
    with engine_of(request).connect() as connection:
        accounts = connection.execute(
            select(Account.id, Account.name)
            .where(Account.closed_at.is_(None), Account.ignored_at.is_(None))
            .order_by(Account.name)
        ).all()
    return templates.TemplateResponse(
        request, template, {"accounts": accounts, "formats": FORMATS, **context}, status_code=status
    )


@router.get("/import", response_class=HTMLResponse)
def import_form(request: Request, user: Annotated[CurrentUser, Depends(current_user)]) -> Response:
    return _render(request, "import.html", {"user": user, "erreur": None})


@router.post("/import/apercu", response_class=HTMLResponse)
async def preview(
    request: Request,
    *,
    user: Annotated[CurrentUser, Depends(current_user)],
    compte: Annotated[int, Form()],
    format: Annotated[str, Form()],
    fichier: UploadFile,
) -> Response:
    content = await fichier.read(MAX_FILE_BYTES + 1)
    try:
        _, pages = read_pages(content, format)
    except ImportFileError as exc:
        return _render(request, "import.html", {"user": user, "erreur": str(exc)}, status=422)
    transactions = [tx for page in pages for tx in page.items]
    return _render(
        request,
        "import_preview.html",
        {
            "user": user,
            "compte": compte,
            "format": format,
            "nom_fichier": fichier.filename or "releve",
            "contenu": base64.b64encode(content).decode(),
            "empreinte": hashlib.sha256(content).hexdigest(),
            "apercu": transactions[:PREVIEW_ROWS],
            "total": len(transactions),
            "somme": sum(tx.amount for tx in transactions),
        },
    )


@router.post("/import/confirmer", response_class=HTMLResponse)
def confirm(
    request: Request,
    *,
    user: Annotated[CurrentUser, Depends(current_user)],
    compte: Annotated[int, Form()],
    format: Annotated[str, Form()],
    nom_fichier: Annotated[str, Form()],
    contenu: Annotated[str, Form()],
    empreinte: Annotated[str, Form()],
) -> Response:
    content = base64.b64decode(contenu, validate=True)
    if hashlib.sha256(content).hexdigest() != empreinte:
        return _render(
            request,
            "import.html",
            {"user": user, "erreur": "Fichier altéré entre l'aperçu et la confirmation."},
            status=422,
        )
    try:
        outcome = import_file(
            engine_of(request),
            account_id=compte,
            content=content,
            file_format=format,
            file_name=nom_fichier[:255],
            now=now_of(request),
        )
    except ImportFileError as exc:
        return _render(request, "import.html", {"user": user, "erreur": str(exc)}, status=422)
    return _render(request, "import_done.html", {"user": user, "resultat": outcome})
