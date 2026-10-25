"""Garde-fous d'architecture vérifiés sur le code source de app/."""

import ast
import re
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
SOURCES = sorted(APP.rglob("*.py"))


# Usages du type `float` admis, fichier par fichier, avec leur justification (jamais de l'argent).
FLOAT_TYPE_ALLOWED = {
    "connectors/requester.py": "signature de time.sleep (durée en secondes)",
}


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(APP.parent)))
def test_no_float_in_application_code(path: Path) -> None:
    """Inv. 2 : pas de float pour l'argent. Par prudence, aucun littéral décimal dans app/, et le
    type `float` seulement dans les fichiers listés ci-dessus avec leur justification."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    type_allowed = str(path.relative_to(APP)) in FLOAT_TYPE_ALLOWED
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == "float" and not type_allowed)
        or (isinstance(node, ast.Constant) and isinstance(node.value, float))
    ]
    assert offenders == [], f"float utilisé dans {path.name}, lignes {offenders}"


TEMPLATES = sorted((APP / "web" / "templates").glob("*.html"))


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_no_float_arithmetic_in_templates(path: Path) -> None:
    """Inv. 2 dans les gabarits aussi : « centimes / 100 » donne un float (« 850.0 ») ;
    utiliser les filtres `eur`, `saisie` ou `pourcent`."""
    text = path.read_text(encoding="utf-8")
    assert re.findall(r"\s/\s*100\b|\|\s*round\b", text) == [], path.name


FORBIDDEN_NETWORK_MODULES = {"requests", "urllib.request", "http.client", "aiohttp", "socket"}
HTTPX_FACTORIES = {
    "Client",
    "AsyncClient",
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "request",
    "stream",
}
HTTP_CLIENT_MODULE = APP / "net" / "client.py"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(APP.parent)))
def test_single_http_client(path: Path) -> None:
    """Inv. 1 : toute requête sortante passe par app/net/client.py (liste blanche)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            problems += [a.name for a in node.names if a.name in FORBIDDEN_NETWORK_MODULES]
        elif isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_NETWORK_MODULES:
            problems.append(node.module)
        elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
            problems += [a.name for a in node.names if a.name in HTTPX_FACTORIES]
        elif (
            path != HTTP_CLIENT_MODULE
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "httpx"
            and node.func.attr in HTTPX_FACTORIES
        ):
            problems.append(f"httpx.{node.func.attr}() (ligne {node.lineno})")
    assert problems == [], f"accès réseau hors du client unique dans {path.name} : {problems}"


def test_no_test_file_is_git_ignored() -> None:
    """Un fichier de test ou d'exemple ignoré par Git passe en local et manque en CI."""
    listed = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "tests"],  # noqa: S607
        cwd=APP.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    ignored = [
        name
        for name in listed
        if "__pycache__" not in name and ".hypothesis" not in name and ".pytest_cache" not in name
    ]
    assert ignored == []


MONEY_NAME = re.compile(r"amount|cents|montant|solde|balance|total|price|prix|loyer", re.IGNORECASE)


def _names(node: ast.AST) -> list[str]:
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)] + [
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    ]


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(APP.parent)))
def test_no_true_division_of_money(path: Path) -> None:
    """Inv. 2 : « / » produit un float. Division par une constante numérique (ex. `x / 100`) ou
    d'une valeur au nom monétaire interdite : utiliser `//`, `divmod`, `allocate` ou
    `round_to_cents` sur une Fraction. (Les chemins `Path / "nom"` restent permis.)"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        numeric_divisor = isinstance(node.right, ast.Constant) and isinstance(
            node.right.value, int | float
        )
        money_operand = any(MONEY_NAME.search(name) for name in _names(node))
        if numeric_divisor or money_operand:
            offenders.append(node.lineno)
    assert offenders == [], f"division « / » suspecte dans {path.name}, lignes {offenders}"


INTERFACE_MODULES = ("app.web", "fastapi", "starlette", "jinja2")


def _is_interface(module: str) -> bool:
    return any(module == m or module.startswith(f"{m}.") for m in INTERFACE_MODULES)


@pytest.mark.parametrize(
    "path",
    [p for p in SOURCES if p.relative_to(APP).parts[0] != "web"],
    ids=lambda p: str(p.relative_to(APP.parent)),
)
def test_services_do_not_depend_on_the_web_interface(path: Path) -> None:
    """Architecture en couches (CDC §10) : domaine et services ignorent l'interface web ; elle
    les appelle, jamais l'inverse (l'API, le serveur MCP ou l'outil d'administration s'en
    servent de la même façon)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = [
        name
        for node in ast.walk(tree)
        for name in (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
    ]
    offenders = [name for name in imported if _is_interface(name)]
    assert offenders == [], f"{path.name} dépend de l'interface web : {offenders}"
