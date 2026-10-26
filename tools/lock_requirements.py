"""Genera un requirements.txt con versiones exactas y hashes para todas las
plataformas y versiones de Python soportadas.

pip evalúa los markers de entorno (``sys_platform == "win32"``, etc.) contra
el intérprete que lo ejecuta, aunque se le pase ``--platform``. Por eso este
script resuelve cada plataforma por separado, recorre el árbol de dependencias
evaluando los markers del entorno destino y agrega lo que pip omitió.

Solo acepta wheels (``--only-binary=:all:``): no se ejecuta código de terceros
al generar el lock.

Uso (requiere red):
    python tools/lock_requirements.py [requirements.in] [requirements.txt]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name
except ImportError:
    sys.exit("Este script necesita pip instalado en el intérprete actual.")

ROOT = Path(__file__).resolve().parent.parent
INDEX_URL = "https://pypi.org/simple"
PYTHONS = ("3.12", "3.13", "3.14")

# sys_platform -> (tags de wheel aceptados, markers de entorno)
PLATFORMS: dict[str, tuple[tuple[str, ...], dict[str, str]]] = {
    "darwin": (
        ("macosx_11_0_arm64",),
        {"platform_system": "Darwin", "os_name": "posix", "platform_machine": "arm64"},
    ),
    "win32": (
        ("win_amd64",),
        {"platform_system": "Windows", "os_name": "nt", "platform_machine": "AMD64"},
    ),
}


@dataclass(frozen=True)
class Target:
    platform: str
    python: str

    @property
    def env(self) -> dict[str, str]:
        return {
            **PLATFORMS[self.platform][1],
            "sys_platform": self.platform,
            "python_version": self.python,
            "python_full_version": f"{self.python}.0",
            "implementation_name": "cpython",
            "implementation_version": f"{self.python}.0",
            "platform_python_implementation": "CPython",
        }

    def __str__(self) -> str:
        return f"{self.platform}/py{self.python}"


@dataclass
class Locked:
    name: str
    version: str
    hashes: set[str] = field(default_factory=set)
    targets: set[Target] = field(default_factory=set)
    via: set[str] = field(default_factory=set)


def read_roots(path: Path) -> list[Requirement]:
    roots = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            roots.append(Requirement(line))
    return roots


def without_marker(req: Requirement) -> str:
    extras = f"[{','.join(sorted(req.extras))}]" if req.extras else ""
    return f"{req.name}{extras}{req.specifier}"


def pip_resolve(target: Target, reqs: list[str]) -> dict[str, dict]:
    """Corre ``pip install --dry-run`` para el target y devuelve el reporte por paquete."""
    nodot = target.python.replace(".", "")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        cmd = [
            sys.executable, "-m", "pip", "install",
            "--dry-run", "--quiet", "--isolated", "--disable-pip-version-check",
            "--ignore-installed", "--only-binary=:all:",
            "--index-url", INDEX_URL,
            "--implementation", "cp",
            "--python-version", target.python,
            "--abi", f"cp{nodot}", "--abi", "abi3", "--abi", "none",
            "--target", str(Path(tmp) / "target"),
            "--report", str(report),
        ]
        for tag in PLATFORMS[target.platform][0]:
            cmd += ["--platform", tag]
        subprocess.run([*cmd, *reqs], check=True)
        data = json.loads(report.read_text(encoding="utf-8"))
    return {canonicalize_name(item["metadata"]["name"]): item for item in data["install"]}


def applies(req: Requirement, env: dict[str, str], extra: str) -> bool:
    return req.marker is None or req.marker.evaluate({**env, "extra": extra})


def walk(
    roots: list[Requirement], resolved: dict[str, dict], env: dict[str, str]
) -> tuple[dict[str, set[str]], list[Requirement]]:
    """Recorre el árbol con los markers del target.

    Devuelve ``{paquete: {quién lo pide}}`` y los requisitos que pip no
    resolvió (o resolvió con una versión que no los satisface).
    """
    via: dict[str, set[str]] = {}
    visited_extras: dict[str, set[str]] = {}
    missing: list[Requirement] = []
    pending = [(req, "requirements.in") for req in roots if applies(req, env, "")]
    while pending:
        req, parent = pending.pop()
        name = canonicalize_name(req.name)
        item = resolved.get(name)
        if item is None or not req.specifier.contains(item["metadata"]["version"], prereleases=True):
            missing.append(req)
            continue
        via.setdefault(name, set()).add(parent)
        seen = visited_extras.setdefault(name, set())
        new_extras = ({""} | set(req.extras)) - seen
        seen |= new_extras
        for line in item["metadata"].get("requires_dist") or []:
            dep = Requirement(line)
            if any(applies(dep, env, extra) for extra in new_extras):
                pending.append((dep, item["metadata"]["name"]))
    return via, missing


def lock_target(target: Target, roots: list[Requirement]) -> tuple[dict[str, dict], dict[str, set[str]]]:
    reqs = [without_marker(r) for r in roots if applies(r, target.env, "")]
    for _ in range(10):
        resolved = pip_resolve(target, reqs)
        via, missing = walk(roots, resolved, target.env)
        new = [s for s in map(without_marker, missing) if s not in reqs]
        if not missing:
            # Se descartan los paquetes que pip agregó solo por los markers del host.
            return {name: resolved[name] for name in via}, via
        if not new:
            sys.exit(f"[{target}] no se pudo satisfacer: {', '.join(map(str, missing))}")
        print(f"[{target}] agregando lo que pip omitió: {', '.join(new)}")
        reqs += new
    sys.exit(f"[{target}] la resolución no converge")


def marker_for(targets: set[Target]) -> str | None:
    """Marker mínimo que describe en qué targets hace falta un paquete."""
    full = set(PYTHONS)
    by_platform = {p: {t.python for t in targets if t.platform == p} for p in PLATFORMS}
    if all(pys == full for pys in by_platform.values()):
        return None
    groups = list(by_platform.values())
    if all(pys == groups[0] for pys in groups):
        return " or ".join(f'python_version == "{py}"' for py in sorted(groups[0]))
    clauses = []
    for platform, pys in by_platform.items():
        if pys == full:
            clauses.append(f'sys_platform == "{platform}"')
        else:
            clauses += [
                f'(sys_platform == "{platform}" and python_version == "{py}")' for py in sorted(pys)
            ]
    return " or ".join(clauses)


def render(locked: dict[str, Locked], source: Path, output: Path) -> str:
    platforms = ", ".join(f"{p} ({' '.join(tags)})" for p, (tags, _) in PLATFORMS.items())
    out = [
        f"# Generado por tools/lock_requirements.py a partir de {source.name}. No editar a mano.",
        f"# Plataformas: {platforms}",
        f"# Python: {', '.join(PYTHONS)}",
        "#",
        "# Instalar con:",
        f"#   python -m pip install --require-hashes --only-binary=:all: -r {output.name}",
        "",
    ]
    for key in sorted(locked):
        pkg = locked[key]
        marker = marker_for(pkg.targets)
        line = f"{pkg.name}=={pkg.version}" + (f" ; {marker}" if marker else "")
        out.append(" \\\n    ".join([line, *(f"--hash=sha256:{h}" for h in sorted(pkg.hashes))]))
        out.append(f"    # via {', '.join(sorted(pkg.via))}")
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", nargs="?", type=Path, default=ROOT / "requirements.in")
    parser.add_argument("output", nargs="?", type=Path, default=None)
    args = parser.parse_args()
    output = args.output or args.source.with_suffix(".txt")

    roots = read_roots(args.source)
    locked: dict[str, Locked] = {}
    for platform in PLATFORMS:
        for python in PYTHONS:
            target = Target(platform, python)
            print(f"Resolviendo {target}...")
            resolved, via = lock_target(target, roots)
            for name, item in resolved.items():
                meta = item["metadata"]
                pkg = locked.setdefault(name, Locked(meta["name"], meta["version"]))
                if pkg.version != meta["version"]:
                    sys.exit(
                        f"{meta['name']}: {pkg.version} en otros targets y {meta['version']} en "
                        f"{target}. Acotá la versión en {args.source.name}."
                    )
                pkg.hashes.add(item["download_info"]["archive_info"]["hashes"]["sha256"])
                pkg.targets.add(target)
                pkg.via |= via[name]

    output.write_text(render(locked, args.source, output), encoding="utf-8", newline="\n")
    print(f"{len(locked)} paquetes escritos en {output}")


if __name__ == "__main__":
    main()
