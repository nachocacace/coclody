# CoClody

CoClody te deja darle tareas a [Claude Code](https://claude.com/claude-code) desde Telegram, en el celular, mientras tu computadora hace el trabajo.

- Le mandás una tarea a tu bot (por ejemplo, `/nueva Ordená las fotos de la carpeta viajes por año`).
- CoClody se la pasa a Claude Code, que trabaja en una carpeta de tu PC que vos elegís.
- Te avisa por Telegram cuando termina, si falló, o si necesita que le contestes algo.

Todo corre en tu computadora: no hay servidores ni costos extra. Claude Code usa tu propia cuenta de Claude.

> **Importante:** cada persona usa **su propio bot** y su propia instalación. No compartas tu bot ni tu token con nadie.

---

## Instalación en Windows, paso a paso

Vas a necesitar:

- Una PC con Windows 10 u 11 de 64 bits.
- Una cuenta de Claude con acceso a Claude Code.
- Telegram en el celular.
- Unos 20 minutos.

### Paso 1: instalar Claude Code e iniciar sesión

1. Instalá Claude Code siguiendo las instrucciones oficiales para Windows en [claude.com/claude-code](https://claude.com/claude-code).
2. Abrí **PowerShell**: tocá la tecla Windows, escribí `PowerShell` y apretá Enter.
3. Escribí esto y apretá Enter:
   ```
   claude --version
   ```
   Tiene que mostrar un número de versión. Si dice que no reconoce el comando, cerrá PowerShell, volvé a abrirlo y probá de nuevo; si sigue sin andar, reiniciá la PC.
4. Escribí `claude` y apretá Enter. Seguí los pasos para iniciar sesión con tu cuenta. Cuando termine, escribí `/exit` para salir.

### Paso 2: crear tu bot de Telegram

1. En Telegram, buscá **@BotFather** (tiene una tilde azul de verificado) y abrí el chat.
2. Mandale `/newbot`.
3. Te pide un **nombre** (el que vas a ver en el chat, por ejemplo `Mi CoClody`) y después un **usuario**, que tiene que terminar en `bot` (por ejemplo `micoclody_juan_bot`).
4. Te responde con un **token**, algo como `123456789:AAH...`. Ese token es la llave de tu bot: **no se lo pases a nadie**. Lo vas a usar en el paso 6.
5. Tocá el link a tu bot que te dio BotFather y apretá **Iniciar** (o mandale `/start`). Si no lo hacés, el bot no te puede escribir.

### Paso 3: averiguar tu chat ID

1. En Telegram, buscá **@userinfobot** y mandale cualquier mensaje.
2. Te responde con tu **Id**, un número como `123456789`. Anotalo.

### Paso 4: crear la carpeta de trabajo

Es la carpeta donde Claude Code va a poder leer y modificar archivos. **No puede ir a ningún otro lado** (salvo que habilites herramientas extra; ver [Supuestos de seguridad](#supuestos-de-seguridad)).

1. Abrí el Explorador de archivos y entrá a tu carpeta de usuario (`C:\Users\<tu usuario>`).
2. Creá una carpeta nueva, por ejemplo `CoClody-trabajo`.
3. Para copiar su ruta: hacé clic derecho en la carpeta manteniendo apretada la tecla **Shift** → **Copiar como ruta**.

No uses tu carpeta de usuario entera ni el disco `C:\` completo: CoClody no lo permite.

### Paso 5: bajar CoClody

1. Entrá a la [página de descargas](https://github.com/USUARIO/coclody/releases/latest) y bajá `coclody.exe` y `SHA256SUMS.txt`.
2. Creá la carpeta `C:\Users\<tu usuario>\CoClody` y mové `coclody.exe` ahí. No lo dejes en Descargas: las actualizaciones reemplazan el archivo en su lugar.
3. (Recomendado) Verificá que el archivo es el original. En PowerShell:
   ```
   Get-FileHash "$env:USERPROFILE\CoClody\coclody.exe"
   ```
   El número largo que aparece en `Hash` tiene que ser igual al de `SHA256SUMS.txt` (abrilo con el Bloc de notas). Mayúsculas o minúsculas da igual. Si no coincide, **no lo abras**.

### Paso 6: configurar

1. Hacé doble clic en `coclody.exe`.
2. La primera vez Windows puede mostrar **"Windows protegió su PC"**. Pasa porque el programa no está firmado con un certificado comercial. Tocá **Más información** → **Ejecutar de todas formas**.
3. Se abre una ventana negra con el asistente. Te pide:
   - **Chat ID**: el número del paso 3.
   - **Carpeta de trabajo**: pegá la ruta del paso 4 con clic derecho (puede tener comillas, no importa).
   - **Token**: pegá el token del paso 2 con clic derecho. **No se ve nada mientras lo pegás; es normal.** Apretá Enter.
4. Si algo está mal, te dice qué y te lo vuelve a preguntar. Al final dice **Listo**. Apretá Enter para cerrar.

El token queda guardado en el **Administrador de credenciales** de Windows, no en un archivo.

### Paso 7: activar las actualizaciones automáticas (recomendado)

1. Apretá **Windows + R**, pegá esto y apretá Enter:
   ```
   notepad %LOCALAPPDATA%\CoClody\config.toml
   ```
2. Al final del archivo, en la parte `[updates]`, cambiá `enabled = false` por `enabled = true`.
3. En la línea `# github_repo = ""`, borrá el `#` del principio y completala así:
   ```
   github_repo = "USUARIO/coclody"
   ```
4. Guardá (Ctrl+G o Ctrl+S) y cerrá el Bloc de notas.

### Paso 8: verificar

En PowerShell:

```
& "$env:USERPROFILE\CoClody\coclody.exe" check
```

Las líneas que empiezan con corchetes tienen que decir `[ok]`. Si alguna dice `[ERROR]`, buscala en [Problemas frecuentes](#problemas-frecuentes). Si dice `[aviso] Sin clave pública de releases`, tu versión no puede actualizarse sola: avisale a quien te pasó CoClody.

### Paso 9: arrancar

Hacé doble clic en `coclody.exe`. Se abre una ventana negra: **mientras esa ventana esté abierta, el bot funciona**.

- Para apagarlo, cerrá la ventana (o apretá Ctrl+C adentro).
- Si la PC está apagada o suspendida, el bot no responde.
- Los mensajes que le mandes mientras está apagado **se descartan** al arrancar (así una orden vieja no se ejecuta sin que la esperes). Mandalos de nuevo.

**Para que arranque solo al prender la PC (opcional):** apretá Windows + R, escribí `shell:startup` y Enter. En la carpeta que se abre, hacé clic derecho → **Nuevo** → **Acceso directo** y elegí `coclody.exe`.

---

## Cómo se usa

Escribile a tu bot en Telegram:

| Comando | Qué hace |
|---|---|
| `/nueva <título>` | Crea una tarea y la pone en cola. Si escribís más líneas debajo del título, son la descripción. |
| `/lista` | Muestra las tareas activas y las últimas terminadas. |
| `/estado <número>` | Muestra el detalle de una tarea: en qué está, qué preguntó, qué respondió Claude. |
| `/responder <número> <texto>` | Contesta la pregunta pendiente de una tarea. |
| `/cancelar <número>` | Cancela una tarea. Si se está ejecutando, la corta. |
| `/reintentar <número>` | Vuelve a poner en cola una tarea que falló. |
| `/ayuda` | Muestra esta lista. |

Ejemplo:

```
/nueva Resumí los PDFs de la carpeta facturas
Hacé una tabla con fecha, proveedor y monto en un archivo resumen.csv
```

- Las tareas se hacen **de a una**, en orden.
- Te llega un mensaje cuando una tarea **termina**, **falla** o **necesita un dato**.
- Si Claude necesita algo, te manda la pregunta: **respondé directamente a ese mensaje** (mantené apretado el mensaje → Responder) o usá `/responder`. Con eso la tarea sigue sola.

## Actualizaciones

Si activaste el paso 7, CoClody busca una versión nueva una vez por día. Si encuentra una:

1. Verifica que esté **firmada** por quien publica CoClody, que sea **más nueva** que la tuya y que el archivo descargado esté intacto. Si algo no cierra, no instala nada.
2. La instala y te avisa por Telegram.
3. **Se aplica la próxima vez que abras CoClody.** Cerralo y abrilo cuando quieras.

Sin internet no pasa nada: sigue funcionando y lo intenta más tarde. Para buscar a mano:

```
& "$env:USERPROFILE\CoClody\coclody.exe" update
```

## Problemas frecuentes

| Qué pasa | Qué hacer |
|---|---|
| "Windows protegió su PC" | Normal la primera vez: **Más información → Ejecutar de todas formas**. |
| El antivirus borra o bloquea `coclody.exe` | Es un falso positivo común en programas hechos con Python. Verificá el hash (paso 5) y restauralo desde la cuarentena del antivirus. Avisale a quien te pasó CoClody. |
| La ventana se abre y se cierra enseguida | Abrilo desde PowerShell para ver el mensaje: `& "$env:USERPROFILE\CoClody\coclody.exe"` |
| `Falta telegram_bot_token en el llavero` | Corré `& "$env:USERPROFILE\CoClody\coclody.exe" setup` y cargá el token de nuevo. |
| `no se encontró 'claude' en el PATH` | Claude Code no está instalado o Windows no lo encuentra. Repetí el paso 1 y reiniciá la PC. |
| El bot no contesta nada | ¿Está abierta la ventana de CoClody? ¿Le escribís desde la misma cuenta de Telegram cuyo chat ID cargaste? Los mensajes de otras cuentas se ignoran a propósito. |
| No me llegan los avisos | Abrí el chat con tu bot y apretá **Iniciar** (paso 2, punto 5). |
| Quiero cambiar el token | Corré `coclody.exe setup` (como arriba) y contestá `s` cuando pregunte si querés reemplazarlo. |
| Quiero cambiar el chat ID o la carpeta | Editá `%LOCALAPPDATA%\CoClody\config.toml` con el Bloc de notas (como en el paso 7). |
| `hay una actualización anterior pendiente de reinicio` | Cerrá CoClody y volvé a abrirlo. |
| Una tarea falló | Mirá el motivo con `/estado <número>` y, si querés, `/reintentar <número>`. |

Los registros (logs) están en `%LOCALAPPDATA%\CoClody\logs\`. `coclody.log` es el general y `security.log` lista los mensajes rechazados. Ninguno contiene tu token.

## Desinstalar

1. Cerrá CoClody y borrá la carpeta `C:\Users\<tu usuario>\CoClody` y el acceso directo de inicio, si lo creaste.
2. Borrá `%LOCALAPPDATA%\CoClody` (configuración, base de datos y logs).
3. Abrí el **Administrador de credenciales** → **Credenciales de Windows** y eliminá la entrada que contiene `coclody`.
4. (Opcional) En Telegram, mandale `/deletebot` a @BotFather.

---

## Supuestos de seguridad

Estos son supuestos explícitos del diseño. Si alguno no se cumple en tu caso, CoClody no es seguro para ese uso.

1. **Un bot por persona, y la lista blanca es de confianza total.** Cada persona tiene su propio bot con una lista de un solo chat: el suyo. Cualquier chat de `allowed_chat_ids` puede ver, crear, responder, cancelar y reintentar *cualquier* tarea. No hay permisos por tarea ni por usuario.
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
7. **Actualizaciones firmadas.** Solo se instala un release cuyo manifiesto esté firmado (Ed25519) con la clave pública embebida en la app, que sea de una versión estrictamente mayor y cuyo archivo coincida en tamaño y SHA-256. La clave privada vive solo en el llavero de quien publica, nunca en el repo ni en GitHub Actions; quien la tenga puede publicar actualizaciones que las instalaciones existentes van a aceptar.

---

## Referencia de configuración

`config.toml` está en `%LOCALAPPDATA%\CoClody\` (Windows) o en `~/Library/Application Support/CoClody/` (macOS).

| Clave | Por defecto | |
|---|---|---|
| `telegram.allowed_chat_ids` | (obligatoria) | Chat IDs autorizados. Ver supuestos de seguridad. |
| `claude.workspace_root` | (obligatoria) | Carpeta de trabajo. Tiene que existir; no puede ser tu carpeta personal ni contener los datos de CoClody. |
| `claude.executable` | `"claude"` | Nombre en el PATH o ruta absoluta al ejecutable de Claude Code. |
| `claude.allowed_tools` | `["Read", "Glob", "Grep", "Edit", "Write"]` | Herramientas que Claude Code puede usar. `"Bash"` sin acotar no se admite: usá p. ej. `"Bash(git status:*)"`. |
| `claude.timeout_minutes` | `60` | Tiempo máximo por ejecución. |
| `claude.max_stdout_bytes` / `max_stderr_bytes` | `2000000` / `200000` | Cuánta salida se guarda por ejecución. |
| `updates.enabled` | `false` | Buscar e instalar actualizaciones automáticamente. |
| `updates.github_repo` | (sin valor) | Repo público con los releases, `"usuario/repo"`. Obligatorio si `enabled = true`. |
| `updates.check_interval_hours` | `24` | Cada cuánto se busca una versión nueva. |

Dónde quedan los datos:

| | Windows | macOS |
|---|---|---|
| Config y base | `%LOCALAPPDATA%\CoClody\` | `~/Library/Application Support/CoClody/` |
| Logs | `%LOCALAPPDATA%\CoClody\logs\` | `~/Library/Logs/CoClody/` |

Para probar sin tocar tu perfil real, definí `COCLODY_DATA_DIR` con una ruta absoluta: config, base y logs van todos ahí. No puede ser la raíz del disco, ni tu carpeta personal, ni una carpeta que la contenga.

Comandos de la línea de comandos:

```
coclody          # sin configuración: setup; con configuración: run
coclody setup    # crea la configuración y guarda el token
coclody check    # verifica config, llavero, Claude Code, base y clave de releases
coclody run      # arranca el bot
coclody update   # busca (y en el ejecutable, instala) una versión nueva firmada
```

---

## Para desarrolladores

### Correr desde el código (macOS o Windows)

```
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-build.txt
.venv/bin/python -m pytest
.venv/bin/python -m coclody
```

En Windows, usá `.venv\Scripts\python` en lugar de `.venv/bin/python`.

Desde el código, las actualizaciones solo avisan que hay una versión nueva: no se reemplaza nada.

### Versiones de Python

- **Mínimo soportado desde el código: Python 3.12.** CI corre los tests con 3.12.10 (el último 3.12 con binarios de Windows y macOS en Actions) y con la versión del build.
- **El ejecutable se arma con Python 3.14** (parche exacto en `.github/workflows/build.yml`). El intérprete embebido corre en las máquinas de los usuarios, así que tiene que ser de una rama que siga recibiendo parches con binarios: solo la rama en "bugfix" los publica. 3.12 y 3.13 ya están en "security" y solo publican código fuente. **Cuando 3.14 pase a "security" (oct. 2027), el build tiene que pasar a 3.15** (ver [RELEASE.md](RELEASE.md)).
- El desarrollo local puede usar cualquier versión entre 3.12 y 3.14: los locks cubren 3.12, 3.13 y 3.14 en macOS arm64 y Windows x64.

### Dependencias

Runtime: python-telegram-bot, keyring, PyNaCl y certifi (`requirements.in`). Build y tests: además PyInstaller y pytest (`requirements-build.in`). Los locks con versiones exactas y hashes se regeneran con:

```
python tools/lock_requirements.py
python tools/lock_requirements.py requirements-build.in
```

El script resuelve cada plataforma y versión de Python por separado, porque pip evalúa los markers contra la máquina donde corre.

### Ejecutable local

```
pyinstaller --clean --noconfirm coclody.spec
```

Deja `dist/coclody`. El punto de entrada es `run_coclody.py`, fuera del paquete a propósito.

### Releases

El proceso completo, con comandos exactos, está en [RELEASE.md](RELEASE.md).

### Deuda técnica

- `questions.tg_chat_id`, `questions.tg_message_id` y `repo.set_question_message()` quedaron sin uso desde la migración v3: las respuestas por reply se resuelven con `notification_deliveries`. Se mantienen para no migrar solo para quitarlas.
- **No hay rotación de la clave de firma.** `keygen` nunca reemplaza la clave existente, y las instalaciones solo aceptan la clave pública que tienen embebida. Si la clave privada se pierde o se compromete, hay que publicar una versión con una clave nueva y distribuirla a mano.
