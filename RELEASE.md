# Checklist de lanzamiento

Pasos exactos para publicar CoClody. Los comandos son para macOS (la máquina donde vive la clave de firma), desde la raíz del repo y con el venv del proyecto.

**Regla que no se rompe:** la clave privada de firma vive solo en tu llavero (servicio `coclody-release`). Nunca va al repo, a GitHub Actions ni a ningún otro lado. CI genera el manifiesto **sin firmar**; la firma se hace acá.

Reemplazá `USUARIO` por tu usuario de GitHub en todos los comandos.

---

## A. Una sola vez

### 1. Herramientas

- [ ] [GitHub CLI](https://cli.github.com/) instalado y con sesión: `gh auth status`
- [ ] Venv del proyecto funcionando: `.venv/bin/python -m pytest -q`

### 2. Generar la clave de firma

```
.venv/bin/python tools/sign_release.py keygen
```

- [ ] Guarda la clave privada en el llavero e imprime una línea en base64: la **clave pública**.
- [ ] `keygen` nunca reemplaza una clave existente. Si dice "Ya hay una clave de firma", usá esa.
- [ ] **Hacé un respaldo de la clave privada** (no hay rotación: si la perdés, las instalaciones existentes no aceptan más actualizaciones; ver "Deuda técnica" en el README). En Acceso a Llaveros, buscá `coclody-release` y guardala en un gestor de contraseñas. Nunca en el repo ni en la nube sin cifrar.

### 3. Embeber la clave pública

- [ ] Pegala en `coclody/updater/pubkey.py`:
  ```python
  PUBLIC_KEY_B64 = "la-clave-publica-en-base64="
  ```
- [ ] Verificá que `check` ya no muestra el aviso:
  ```
  .venv/bin/python -m coclody check | grep "clave pública"
  ```
  Tiene que decir `[ok] Clave pública de releases configurada`.
- [ ] `.venv/bin/python -m pytest -q` en verde (un test verifica que la clave pegada sea válida).

### 4. Completar los links del README

- [ ] En `README.md`, reemplazá las dos apariciones de `USUARIO/coclody` (link de descargas y `github_repo` del paso 7) por tu repo real.

### 5. Commit y repo público

```
git add coclody/updater/pubkey.py README.md
git commit -m "Clave pública de releases y links del repo"
gh repo create USUARIO/coclody --public --source . --remote origin --push
```

- [ ] El repo tiene que ser **público**: la app baja los releases sin credenciales.
- [ ] En GitHub → Actions, el workflow `build` corre sobre `main` y termina en verde (tests en Windows y macOS con 3.12.10 y 3.14.10, más el build del `.exe`). Seguilo con `gh run watch`.

---

## B. Cada release

### 1. Versión

- [ ] Subí la versión en **los dos** lugares (un test falla si no coinciden):
  - `coclody/__init__.py` → `__version__ = "X.Y.Z"`
  - `pyproject.toml` → `version = "X.Y.Z"`
- [ ] La versión tiene que ser **mayor** que la última publicada: las instalaciones ignoran versiones iguales o más viejas.
- [ ] `.venv/bin/python -m pytest -q` en verde.

### 2. Tag

```
git commit -am "Versión X.Y.Z"
git tag -a vX.Y.Z -m "CoClody X.Y.Z"
git push origin main vX.Y.Z
gh run watch
```

- [ ] El workflow termina en verde y crea un **release en borrador** con `coclody.exe`, `SHA256SUMS.txt` y `manifest.json` (sin firmar). Si el tag no coincide con `__version__`, el workflow falla a propósito.

### 3. Bajar y verificar

```
mkdir -p ~/coclody-releases/vX.Y.Z && cd ~/coclody-releases/vX.Y.Z
gh release download vX.Y.Z --repo USUARIO/coclody --pattern coclody.exe --pattern manifest.json --pattern SHA256SUMS.txt
shasum -a 256 -c SHA256SUMS.txt
cat manifest.json
```

- [ ] `shasum` dice `coclody.exe: OK`.
- [ ] En `manifest.json`: `version` es `X.Y.Z`, `platform` es `windows-x64` y `sha256` coincide con `SHA256SUMS.txt`.
- [ ] Trabajá fuera del repo (como en `~/coclody-releases/`) para no commitear binarios por accidente.

### 4. Firmar

Desde la raíz del repo:

```
.venv/bin/python tools/sign_release.py sign ~/coclody-releases/vX.Y.Z/manifest.json ~/coclody-releases/vX.Y.Z/coclody.exe
.venv/bin/python tools/sign_release.py verify ~/coclody-releases/vX.Y.Z/manifest.json ~/coclody-releases/vX.Y.Z/manifest.json.sig
```

- [ ] `sign` se niega si el `.exe` no coincide con el manifiesto o si la clave del llavero no es la embebida en `pubkey.py`. Si se niega, **no publiques**: averiguá por qué.
- [ ] `verify` dice `Firma válida (versión X.Y.Z).`

### 5. Subir la firma y publicar

```
gh release upload vX.Y.Z ~/coclody-releases/vX.Y.Z/manifest.json.sig --repo USUARIO/coclody
gh release view vX.Y.Z --repo USUARIO/coclody
gh release edit vX.Y.Z --draft=false --repo USUARIO/coclody
```

- [ ] Antes de publicar, `gh release view` lista los cuatro archivos: `coclody.exe`, `SHA256SUMS.txt`, `manifest.json` y `manifest.json.sig`. **Sin `manifest.json.sig` las instalaciones rechazan el release.**
- [ ] Publicado: desde ahora las instalaciones con actualizaciones activadas lo ven (en hasta `check_interval_hours`, o al instante con `coclody update`).

### 6. Después de publicar

- [ ] **Primer release con clave:** las instalaciones anteriores no tienen la clave pública embebida, así que no se actualizan solas. Pasales el link de descargas para que bajen esta versión a mano (README, paso 5). Las siguientes ya llegan solas.
- [ ] En una máquina con una versión anterior y actualizaciones activadas: `coclody.exe update` → `Se instaló la versión X.Y.Z; se aplica al reiniciar CoClody.` Al reabrir CoClody, `coclody.exe --version` muestra `X.Y.Z` y `coclody.exe.old` desaparece.

---

## C. Mantenimiento

- [ ] **Python del build.** Solo la rama de Python en "bugfix" publica binarios para Windows. Revisá cada tanto el estado en [devguide.python.org/versions](https://devguide.python.org/versions/). Cuando 3.14 pase a "security" (oct. 2027):
  1. En `.github/workflows/build.yml`, cambiá `PYTHON_BUILD_VERSION` y la entrada de la matriz de tests a la última 3.15.x que tenga build de Windows x64 y macOS arm64 en [actions/python-versions](https://github.com/actions/python-versions/blob/main/versions-manifest.json).
  2. Agregá `"3.15"` a `PYTHONS` en `tools/lock_requirements.py` y regenerá los dos locks.
  3. Actualizá la sección "Versiones de Python" del README.
- [ ] **Parche de Python.** Para tomar un parche nuevo de la misma rama, alcanza con cambiar `PYTHON_BUILD_VERSION` (y la entrada de la matriz) y sacar un release.
- [ ] **Dependencias.** Para actualizarlas: regenerá los locks con `tools/lock_requirements.py`, corré los tests y revisá el diff de `requirements*.txt` antes de commitear.
- [ ] **Actions.** Están fijadas por SHA de commit. Para actualizarlas, obtené el SHA del tag nuevo (`git ls-remote https://github.com/actions/checkout refs/tags/vN.N.N`) y reemplazalo junto con el comentario de la versión.
