# CoClody

Orquestador personal de tareas para [Claude Code](https://claude.com/claude-code), controlado desde Telegram.

- Guardás tareas desde el celular (`/nueva`), CoClody se las asigna a Claude Code de a una, y te avisa cuando terminan, fallan o necesitan un dato.
- Todo corre en tu computadora: SQLite local, sin servicios en la nube ni API paga. Claude Code usa tu propia cuenta.
- Funciona en macOS y Windows.

## Requisitos

- **Claude Code** instalado y con sesión iniciada (`claude` tiene que funcionar en una terminal).
- Un **bot de Telegram** propio: se crea hablando con [@BotFather](https://t.me/BotFather), que te da el token.
- Tu **chat ID** de Telegram: escribile a [@userinfobot](https://t.me/userinfobot) y te lo responde.
- Una **carpeta de trabajo** que ya exista: es lo único donde Claude Code va a trabajar.

## Instalación

### Windows (ejecutable)

1. Bajá `coclody.exe` del último release en GitHub y verificá su SHA-256 contra `SHA256SUMS.txt`:
   ```
   certutil -hashfile coclody.exe SHA256
   ```
2. La primera vez Windows puede mostrar "Windows protegió su PC" (SmartScreen): el ejecutable no está firmado con un certificado comercial. Elegí **Más información → Ejecutar de todas formas**.
3. Abrilo con doble clic. Si todavía no hay configuración, arranca el asistente de configuración.

### Desde el código (desarrollo, o macOS)

```
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-build.txt
.venv/bin/python -m coclody
```

En Windows, `.venv\Scripts\python` en lugar de `.venv/bin/python`.

## Configuración

```
coclody setup
```

El asistente pregunta tu chat ID y la carpeta de trabajo, crea `config.toml` y guarda el token del bot en el llavero del sistema (Keychain en macOS, Administrador de credenciales en Windows). El token nunca se escribe en un archivo ni se muestra. Si ya hay un `config.toml`, no lo modifica: lo valida y te muestra qué corregir.

Después:

```
coclody check   # verifica config, llavero, Claude Code y base, sin arrancar nada
coclody run     # arranca el bot (Ctrl+C para salir)
coclody update  # busca una versión nueva firmada y, en el ejecutable, la instala
```

Sin subcomando, `coclody` corre `setup` si falta la configuración, o `run` si ya está.

### Dónde quedan los datos

| | macOS | Windows |
|---|---|---|
| Config y base | `~/Library/Application Support/CoClody/` | `%LOCALAPPDATA%\CoClody\` |
| Logs | `~/Library/Logs/CoClody/` | `%LOCALAPPDATA%\CoClody\logs\` |

`coclody.log` es el log general y `security.log` registra los mensajes rechazados. En macOS las carpetas quedan con permisos 0700 y los archivos con 0600.

Para probar sin tocar tu perfil real, definí `COCLODY_DATA_DIR` con una ruta absoluta: config, base y logs van todos ahí. No puede ser la raíz del disco, ni tu carpeta personal, ni una carpeta que la contenga.

### Opciones de `config.toml`

| Clave | Por defecto | |
|---|---|---|
| `telegram.allowed_chat_ids` | (obligatoria) | Lista de chat IDs autorizados. Ver supuestos de seguridad. |
| `claude.workspace_root` | (obligatoria) | Carpeta de trabajo. Tiene que existir; no puede ser tu carpeta personal ni contener los datos de CoClody. |
| `claude.executable` | `"claude"` | Nombre en el PATH o ruta absoluta. |
| `claude.allowed_tools` | `["Read", "Glob", "Grep", "Edit", "Write"]` | Herramientas que Claude Code puede usar. `"Bash"` sin acotar no se admite: usá p. ej. `"Bash(git status:*)"`. |
| `claude.timeout_minutes` | `60` | Tiempo máximo por ejecución. |
| `claude.max_stdout_bytes` / `max_stderr_bytes` | `2000000` / `200000` | Cuánta salida se guarda por ejecución. |
| `updates.enabled` | `false` | Buscar e instalar actualizaciones automáticamente. |
| `updates.github_repo` | (sin valor) | Repo público con los releases, `"usuario/repo"`. Obligatorio si `enabled = true`. |
| `updates.check_interval_hours` | `24` | Cada cuánto se busca una versión nueva. |

## Uso desde Telegram

| Comando | |
|---|---|
| `/nueva <título>` | Crea una tarea y la pone en cola. Las líneas siguientes al título son la descripción. |
| `/lista` | Tareas activas y las últimas terminadas. |
| `/estado <id>` | Detalle, pregunta pendiente, última ejecución y eventos. |
| `/responder <id> <texto>` | Responde la pregunta pendiente. También podés responder directamente al mensaje de la pregunta. |
| `/cancelar <id>` | Cancela una tarea; si está corriendo, mata el proceso. |
| `/reintentar <id>` | Vuelve a poner en cola una tarea que falló. |

Recibís un mensaje cuando una tarea termina, falla o necesita un dato. El resto de los eventos queda en `/estado`.

Cuando Claude Code necesita algo que no sabe, termina su respuesta con una línea `COCLODY_PREGUNTA: …`; la tarea queda esperando tu respuesta y después sigue la misma sesión.

Al arrancar, CoClody descarta los mensajes que llegaron mientras estaba apagado: un comando de hace horas no dispara una tarea sin que lo esperes.

## Actualizaciones

Con `updates.enabled = true`, CoClody busca cada `check_interval_hours` el último release **publicado** del repo (los borradores no cuentan). Antes de instalar verifica, en este orden, y ante cualquier falla no instala nada:

1. El release trae `manifest.json` y `manifest.json.sig`.
2. La firma Ed25519 del manifiesto es válida con la clave pública embebida en la app (`coclody/updater/pubkey.py`).
3. El manifiesto es para esta plataforma, su versión coincide con el tag del release y es **mayor** que la instalada (nunca se instala una versión igual ni más vieja).
4. El archivo descargado tiene exactamente el tamaño y el SHA-256 firmados.

Después renombra el ejecutable actual a `coclody.exe.old` (Windows permite renombrar un `.exe` en uso), deja el nuevo en su lugar y te avisa por Telegram. La versión nueva se usa al reiniciar CoClody, y ese arranque borra el `.old`.

- **Sin internet** (o con GitHub caído) no pasa nada: la app sigue igual y reintenta en el próximo intervalo. Una descarga cortada a la mitad se descarta entera.
- Las descargas solo usan HTTPS, también en las redirecciones.
- Desde el código fuente solo se informa que hay una versión nueva: no se reemplaza nada.
- Mientras `pubkey.py` no tenga clave, el updater no instala nada (`coclody check` lo muestra como `[aviso]`).

## Supuestos de seguridad

Estos son supuestos explícitos del diseño. Si alguno no se cumple en tu caso, CoClody no es seguro para ese uso.

1. **Un bot por persona, y la lista blanca es de confianza total.** La idea es que cada persona tenga su propio bot con una lista de un solo chat: el suyo. Cualquier chat de `allowed_chat_ids` puede ver, crear, responder, cancelar y reintentar *cualquier* tarea. No hay permisos por tarea ni por usuario.
2. **Solo chats privados.** Grupos y canales se rechazan siempre, aunque su ID esté en la lista, porque en un grupo cualquier miembro podría dar órdenes. Cada rechazo se registra en `security.log` (IDs, tipo de chat y motivo, nunca el texto del mensaje).
3. **Claude Code carga tu `CLAUDE.md` y tus skills personales.** CoClody no carga ningún `settings.json` (ni del proyecto ni tuyo, vía `--setting-sources ""`) ni servidores MCP, pero Claude Code igual lee tu `~/.claude/CLAUDE.md` y tus skills personales. Evitarlo requiere el modo `--bare`, que solo autentica con `ANTHROPIC_API_KEY` (la API paga), y CoClody no lo usa a propósito. Revisá que lo que tengas ahí sea apropiado para tareas que llegan por Telegram.
4. **No es un sandbox del sistema operativo.** Claude Code corre con tus permisos de usuario. Los límites son:
   - directorio de trabajo dentro de `workspace_root`, verificado después de resolver symlinks;
   - herramientas disponibles y preaprobadas solo las de `allowed_tools` (`--tools` + `--allowedTools`);
   - todo lo no preaprobado se deniega (`--permission-mode dontAsk`, `--permission-prompts none`);
   - el texto de las tareas va por stdin, nunca como argumento: no puede inyectar flags.

   Si habilitás herramientas de Bash, eso puede salir de la carpeta de trabajo.
5. **El token del bot vive solo en el llavero del sistema.** No está en el código, en el repo ni en la config, y se redacta de todos los logs.
6. **Sin API paga.** Del entorno de Claude Code se quitan `ANTHROPIC_*` y `CLAUDE_CODE_USE_*`, para que siempre use tu sesión de Claude Code.
7. **La clave privada de releases vive solo en el llavero de quien firma.** Nunca en el repo ni en GitHub Actions. Quien tenga esa clave puede publicar actualizaciones que las instalaciones existentes van a aceptar.

## Versiones de Python

- **Python 3.12 es el mínimo soportado.** El ejecutable de Windows se arma en GitHub Actions (`.github/workflows/build.yml`) con un parche exacto: **3.12.10**, que es donde importa que la versión coincida. Es el último 3.12 con binarios de Windows y macOS en Actions: desde 3.12.11 la rama solo recibe parches de seguridad en código fuente, así que el ejecutable no incluye esos parches.
- El desarrollo local puede usar una versión más nueva (hoy 3.14). Los locks cubren 3.12, 3.13 y 3.14 en macOS arm64 y Windows x64.

## Desarrollo

```
.venv/bin/python -m pytest
```

- Dependencias: `requirements.in` (runtime) y `requirements-build.in` (más PyInstaller y pytest). Los locks con versiones exactas y hashes se regeneran con:
  ```
  python tools/lock_requirements.py
  python tools/lock_requirements.py requirements-build.in
  ```
  El script resuelve cada plataforma y versión de Python por separado, porque pip evalúa los markers contra la máquina donde corre.
- Ejecutable local: `pyinstaller --clean --noconfirm coclody.spec` (deja `dist/coclody`). El punto de entrada es `run_coclody.py`, fuera del paquete a propósito.

## Releases

### Una sola vez: generar la clave de firma

```
python tools/sign_release.py keygen
```

Guarda la clave privada en el llavero del sistema (servicio `coclody-release`) e imprime la clave pública. Pegala en `PUBLIC_KEY_B64` de `coclody/updater/pubkey.py` y commiteala: es pública. Las instalaciones solo aceptan releases firmados con la clave que tienen embebida. Por eso **la primera versión con clave se distribuye a mano**, y recién las siguientes llegan por actualización automática. `keygen` nunca reemplaza una clave existente.

### Cada release

1. Subir `__version__` en `coclody/__init__.py` y crear el tag `vX.Y.Z` con el mismo número (CI falla si no coinciden).
2. GitHub Actions corre los tests en macOS y Windows, arma `coclody.exe` con Python 3.12.10, le hace una prueba de humo y crea un **release en borrador** con `coclody.exe`, `SHA256SUMS.txt` y `manifest.json` **sin firmar**.
3. Localmente, con los archivos bajados del borrador:
   ```
   python tools/sign_release.py sign manifest.json coclody.exe
   gh release upload vX.Y.Z manifest.json.sig
   ```
   `sign` verifica que el `.exe` coincide con el manifiesto y que la clave del llavero es la embebida en la app antes de firmar.
4. Publicar el release. Desde ese momento las instalaciones lo ven.

`python tools/sign_release.py verify manifest.json manifest.json.sig` verifica una firma con la clave embebida.

## Deuda técnica

- `questions.tg_chat_id`, `questions.tg_message_id` y `repo.set_question_message()` quedaron sin uso desde la migración v3: las respuestas por reply se resuelven con `notification_deliveries`. Se mantienen para no migrar solo para quitarlas.
- `coclody/updater/http.py` usa `certifi`, que llega como dependencia transitiva de python-telegram-bot (vía httpx) y no está declarada directamente en `requirements.in`.
