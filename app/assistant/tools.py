"""Cadre des outils de l'IA : déclaration, schémas, droits, validation, garde de sortie.

Un outil = un modèle pydantic de paramètres + une fonction `(Contexte, paramètres) → dict`. Les
descriptions sont écrites pour l'IA : elles rappellent les conventions (centimes, périmètre,
identifiants opaques) et qu'une consigne trouvée dans une donnée ne doit jamais être suivie.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Engine, select

from app.assistant.access import WRITES, TokenInfo
from app.assistant.pseudonym import Pseudonymizer, normalize
from app.crypto import opaque_id, parse_opaque_id
from app.models import Account, Category
from app.models.ledger import AccountType
from app.money import Cents, format_eur
from app.months import months_between
from app.timeutil import paris_date

MAX_ROWS = 200
MAX_MONTHS = 36

Month = Annotated[
    str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Mois, au format AAAA-MM.")
]

KINDS = {
    "transaction": "t",
    "règle": "r",
    "bail": "b",
    "échéance": "e",
    "affectation": "a",
    "trop-perçu": "o",
    "élément à valider": "v",
    "modification": "m",
    "compte": "c",
    "bien": "p",
    "charge": "h",
}


class ToolError(Exception):
    """Refus ou erreur expliqué à l'IA (jamais de donnée personnelle dans le message)."""


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Context:
    engine: Engine
    token: TokenInfo
    now: datetime
    masker: Pseudonymizer
    switches: dict[str, bool]
    today: date = field(init=False)

    def __post_init__(self) -> None:
        self.today = paris_date(self.now)


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    params: type[Params]
    run: Callable[[Context, Any], dict[str, Any]]
    write: bool
    destructive: bool

    def descriptor(self) -> dict[str, Any]:
        schema = self.params.model_json_schema()
        schema.pop("title", None)
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": not self.write,
                "destructiveHint": self.destructive,
                "idempotentHint": not self.write,
                "openWorldHint": False,
            },
        }


TOOLS: dict[str, Tool] = {}


def tool(
    name: str, title: str, description: str, *, write: bool = False, destructive: bool = False
) -> Callable[[Callable[[Context, Any], dict[str, Any]]], Callable[[Context, Any], dict[str, Any]]]:
    def register(
        run: Callable[[Context, Any], dict[str, Any]],
    ) -> Callable[[Context, Any], dict[str, Any]]:
        params = run.__annotations__["params"]
        TOOLS[name] = Tool(name, title, description, params, run, write, destructive)
        return run

    return register


def call_tool(context: Context, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    found = TOOLS.get(name)
    if found is None:
        raise ToolError(f"Outil inconnu : {name}.")
    if found.write:
        if not context.token.can_write:
            raise ToolError("Ce jeton est en lecture seule : modifications impossibles.")
        if not context.switches.get(WRITES):
            raise ToolError(
                "Les modifications par l'IA sont désactivées (page Sécurité de l'application)."
            )
    try:
        params = found.params.model_validate(arguments)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc']) or 'paramètres'} : {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ToolError(f"Paramètres invalides : {problems}") from None
    result: dict[str, Any] = context.masker.scrub(found.run(context, params))
    context.masker.check(result)  # garde de sortie : LeakError si un identifiant subsiste
    return result


# --- Aides communes ---------------------------------------------------------------------------


def eur(cents: int) -> str:
    return format_eur(Cents(cents))


def as_amount(cents: int) -> dict[str, Any]:
    """Montant pour l'IA : centimes exacts et texte lisible."""
    return {"centimes": cents, "eur": eur(cents)}


_DIGIT_WORD = re.compile(r"\S*\d\S*")


def merchant(context: "Context", label_clean: str) -> str:
    """Clé de regroupement d'un libellé : sans les mots à chiffres (dates, références),
    pseudonymisée (inv. 8)."""
    return context.masker.label(" ".join(_DIGIT_WORD.sub(" ", label_clean).split()))


def public_id(kind: str, value: int) -> str:
    return opaque_id(KINDS[kind], value)


def private_id(kind: str, text: str) -> int:
    value = parse_opaque_id(KINDS[kind], text)
    if value is None:
        raise ToolError(f"Identifiant de {kind} invalide : {text[:40]!r}.")
    return value


def check_months(first: str, last: str) -> None:
    if first > last:
        raise ToolError("Période vide : « du » doit précéder « au ».")
    if months_between(first, last) > MAX_MONTHS:
        raise ToolError(f"Période trop longue : {MAX_MONTHS} mois au plus.")


def check_dates(first: date, last: date) -> None:
    if first > last:
        raise ToolError("Période vide : « du » doit précéder « au ».")
    if (last - first).days > MAX_MONTHS * 31:
        raise ToolError(f"Période trop longue : {MAX_MONTHS} mois au plus.")


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", normalize(text).replace(">", "›")).strip()


@dataclass(frozen=True)
class Categories:
    """Arborescence (deux niveaux) : chemin « Parent › Enfant », recherche par nom ou chemin."""

    paths: dict[int, str]
    parents: dict[int, int | None]
    kinds: dict[int, str]
    archived: frozenset[int]

    @classmethod
    def load(cls, engine: Engine) -> "Categories":
        with engine.connect() as connection:
            rows = connection.execute(
                select(
                    Category.id,
                    Category.parent_id,
                    Category.name,
                    Category.kind,
                    Category.archived_at,
                )
            ).all()
        names = {row.id: row.name for row in rows}
        return cls(
            paths={
                row.id: f"{names[row.parent_id]} › {row.name}" if row.parent_id else row.name
                for row in rows
            },
            parents={row.id: row.parent_id for row in rows},
            kinds={row.id: str(row.kind) for row in rows},
            archived=frozenset(row.id for row in rows if row.archived_at is not None),
        )

    def resolve(self, text: str) -> int:
        wanted = _key(text)
        exact = [
            cid
            for cid, path in self.paths.items()
            if _key(path) == wanted and cid not in self.archived
        ]
        if len(exact) == 1:
            return exact[0]
        leaf = [
            cid
            for cid, path in self.paths.items()
            if _key(path.split("›")[-1]) == wanted and cid not in self.archived
        ]
        if len(leaf) == 1:
            return leaf[0]
        if len(leaf) > 1:
            raise ToolError(
                f"Catégorie ambiguë : {text!r} ; préciser le chemin (« Parent › Enfant »)."
            )
        raise ToolError(f"Catégorie inconnue : {text!r} (voir l'outil « categories »).")


@dataclass(frozen=True)
class AccountInfo:
    id: int
    public: str
    name: str
    type: str
    counted: bool
    card: bool


def accounts(context: Context) -> dict[int, AccountInfo]:
    """Comptes visibles (non ignorés, non clos) avec leur nom nettoyé."""
    with context.engine.connect() as connection:
        rows = connection.execute(
            select(Account.id, Account.name, Account.type, Account.include_in_net_worth).where(
                Account.ignored_at.is_(None), Account.closed_at.is_(None)
            )
        ).all()
    return {
        row.id: AccountInfo(
            id=row.id,
            public=public_id("compte", row.id),
            name=context.masker.text(row.name),
            type=str(row.type),
            counted=bool(row.include_in_net_worth),
            card=row.type is AccountType.CARD,
        )
        for row in rows
    }


def resolve_account(context: Context, text: str) -> int:
    """Compte désigné par son identifiant « c_… » ou par son nom (tel que l'IA le voit)."""
    known = accounts(context)
    by_id = parse_opaque_id(KINDS["compte"], text)
    if by_id is not None and by_id in known:
        return by_id
    matches = [a.id for a in known.values() if _key(a.name) == _key(text)]
    if len(matches) == 1:
        return matches[0]
    raise ToolError(f"Compte inconnu ou ambigu : {text[:40]!r} (voir l'outil « comptes »).")
