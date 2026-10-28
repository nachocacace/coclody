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

Es la carpeta donde Claude Code va a trabajar. Con la configuración que trae CoClody, sus herramientas de archivos están limitadas a esta carpeta. Igual, **CoClody no es una caja de arena del sistema operativo**: lo que Claude puede tocar depende de las herramientas que tenga permitidas (ver [Qué puede tocar Claude](#qué-puede-tocar-claude)). No pongas en esta carpeta nada que no le darías a Claude.

1. Abrí el Explorador de archivos y entrá a tu carpeta de usuario (`C:\Users\<tu usuario>`).
2. Creá una carpeta nueva, por ejemplo `CoClody-trabajo`.
3. Para copiar su ruta: hacé clic derecho en la carpeta manteniendo apretada la tecla **Shift** → **Copiar como ruta**.

No uses tu carpeta de usuario entera ni el disco `C:\` completo: CoClody no lo permite.

### Paso 5: bajar CoClody

1. Entrá a la [página de descargas](https://github.com/nachocacace/coclody/releases/latest) y bajá `coclody.exe` y `SHA256SUMS.txt`.
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
   github_repo = "nachocacace/coclody"
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
- Si Claude necesita algo, te manda la pregunta: **respondé directamente a ese mensaje** (mantené apretado el mensaje → Responder) o usá `/responder`. Con eso la tarea sigue sola. Si escribís el texto suelto en el chat, sin Responder, CoClody no lo toma como respuesta: te explica cómo contestar.

### Botones

Para no escribir comandos desde el celular:

- **Menú de comandos**: el botón **Menú** junto al cuadro de texto lista los comandos desde que CoClody arranca, sin escribir nada. Solo lo ven los chats de la lista blanca.
- **Teclado fijo**: **➕ Nueva tarea**, **📋 Ver cola** y **❓ Ayuda**. Viene en cada mensaje del bot que no trae otros botones, así que queda siempre a mano. La primera vez que abrís el bot, Telegram muestra **Iniciar**: tocalo, porque un bot no puede mandarte nada (ni el teclado) antes de que le escribas. Con *Nueva tarea*, el bot te pide el título: escribilo como respuesta a ese mensaje (Telegram abre la respuesta solo). Las líneas debajo del título son la descripción.
- **En cada tarea**: el mensaje de creación, `/estado` y `/lista` traen **✖ Cancelar** en las tareas activas y **↻ Reintentar** en las que fallaron. El aviso de una tarea fallida también trae **↻ Reintentar**.
- **Estados**: ⏳ en cola · ⚙️ ejecutando · ❓ esperando respuesta · ✅ terminada · ❌ falló · 🚫 cancelada. Son los mismos en `/lista`, `/estado` y los avisos.
- **Preguntas**: toda pregunta trae **✖ Cancelar** para la tarea que pregunta. Si Claude la marca como de sí o no, trae además **Sí** y **No**. Igual podés responder con texto si querés aclarar algo.
- Si tocás *Nueva tarea* y CoClody se reinicia antes de que mandes el título, tu respuesta igual crea la tarea.
- Nada de lo que escribís vos ni de lo que escribe Claude se convierte en link (`README.md`, URLs, `@usuario`, `#etiqueta`, `/comando`): se ve igual, pero no se puede tocar. Tampoco los comandos que sugiere el propio bot: escribilos o usá el botón **Menú**.
- CoClody nunca te escribe primero, salvo para avisarte de tus tareas.

## Actualizaciones

Si activaste el paso 7, CoClody busca una versión nueva una vez por día. Si encuentra una:

1. Verifica que esté **firmada** por quien publica CoClody, que sea **más nueva** que la tuya y que el archivo descargado esté intacto. Si algo no cierra, no instala nada.
2. La instala y lo anota en la ventana de CoClody y en `coclody.log` (no te escribe por Telegram: CoClody solo te escribe por tus tareas).
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
| La ventana de CoClody dice `El polling de Telegram se detuvo` | CoClody dejó de recibir mensajes (casi siempre porque el token se revocó o cambió). Queda abierto pero en pausa: no empieza tareas nuevas ni manda avisos. Corré `coclody.exe check`, corregí el token con `coclody.exe setup` y volvé a abrir CoClody. |
| El bot no contesta nada | ¿Está abierta la ventana de CoClody? Fijate si dice `El polling de Telegram se detuvo` (fila anterior). ¿Le escribís desde la misma cuenta de Telegram cuyo chat ID cargaste? Los mensajes de otras cuentas se ignoran a propósito. |
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

## Qué puede tocar Claude

**CoClody no es una caja de arena del sistema operativo.** Claude Code corre con tus permisos de usuario. Lo único que decide qué puede tocar es la lista `allowed_tools` de `config.toml`, que CoClody le pasa a Claude Code (`--tools` + `--allowedTools`, con todo lo demás denegado).

**Lo que trae por defecto:**

```
allowed_tools = ["Read(./**)", "Glob(./**)", "Grep(./**)", "Edit(./**)", "Write(./**)"]
```

`(./**)` limita cada herramienta a la carpeta de trabajo. Esto se verificó con Claude Code real (versión 2.1.304, en macOS):

- **Sin `(./**)`**, Claude leyó un archivo de afuera, creó otro y editó un tercero, sin que nadie lo aprobara.
- **Con `(./**)`**, se denegaron las rutas de afuera, las rutas con `../` y los symlinks que apuntan afuera, y adentro todo siguió funcionando.

Es un comportamiento de Claude Code, no de CoClody: puede cambiar con una actualización del CLI.

**Ampliar es explícito y cada ampliación abre algo concreto:**

| Si agregás o cambiás | Le estás dando a Claude |
|---|---|
| `Read`, `Glob` o `Grep` sin `(./**)` | Leer **cualquier archivo** que vos puedas leer: claves SSH, documentos, credenciales guardadas en archivos. |
| `Edit` o `Write` sin `(./**)` | Crear y modificar **cualquier archivo tuyo**, incluidos los que se ejecutan solos al iniciar sesión. Equivale a ejecutar código en tu PC. |
| `Bash(...)`, aunque esté acotado | Ejecutar comandos con tus permisos. **Si además Edit o Write están habilitados, se puede escalar a cualquier comando**: muchas herramientas leen configuración del propio proyecto y ejecutan lo que dice ahí (por ejemplo `git` con la configuración del repo, `npm`, `make` o `pytest` con sus archivos de proyecto). Tratá cualquier Bash como acceso total. |
| Otras herramientas (`WebFetch`, `WebSearch`, etc.) | Lo que esa herramienta haga; por ejemplo, red, que sirve para sacar datos de tu PC. |

`coclody check` lista como `[aviso]` cada herramienta que amplía el default, y CoClody lo deja registrado en el log al arrancar.

**Las instrucciones también pueden venir de los archivos.** Si le pedís a Claude que procese archivos que no escribiste vos (PDFs, repos bajados de internet), su contenido puede intentar darle órdenes. Con el default, lo peor que puede pasar queda dentro de la carpeta de trabajo; con herramientas ampliadas, no.

## Supuestos de seguridad

Estos son supuestos explícitos del diseño. Si alguno no se cumple en tu caso, CoClody no es seguro para ese uso.

1. **Un bot por persona, y la lista blanca es de confianza total.** Cada persona tiene su propio bot con una lista de un solo chat: el suyo. Cualquier chat de `allowed_chat_ids` puede ver, crear, responder, cancelar y reintentar *cualquier* tarea. No hay permisos por tarea ni por usuario.
2. **Solo chats privados.** Grupos y canales se rechazan siempre, aunque su ID esté en la lista, porque en un grupo cualquier miembro podría dar órdenes. Cada rechazo se registra en `security.log` (IDs, tipo de chat y motivo, nunca el texto del mensaje).
3. **Claude Code carga tu `CLAUDE.md` y tus skills personales.** CoClody no carga ningún `settings.json` (ni del proyecto ni tuyo, vía `--setting-sources ""`) ni servidores MCP, pero Claude Code igual lee tu `~/.claude/CLAUDE.md` y tus skills personales. Evitarlo requiere el modo `--bare`, que solo autentica con `ANTHROPIC_API_KEY` (la API paga), y CoClody no lo usa a propósito. Revisá que lo que tengas ahí sea apropiado para tareas que llegan por Telegram.
4. **No es una caja de arena del sistema operativo.** Lo que Claude puede tocar depende solo de `allowed_tools` (ver [Qué puede tocar Claude](#qué-puede-tocar-claude)). Lo que sí garantiza CoClody:
   - el directorio de trabajo es `workspace_root`, verificado después de resolver symlinks;
   - las herramientas disponibles y preaprobadas son solo las de `allowed_tools`, y todo lo demás se deniega (`--permission-mode dontAsk`, `--permission-prompts none`);
   - el texto de las tareas va por stdin, nunca como argumento, así que no puede inyectar flags;
   - solo se procesan mensajes nuevos escritos por vos: los editados, los reenviados y los enviados a través de otro bot se ignoran; si el control de acceso falla por un error, deniega;
   - los botones pasan por el mismo control que los mensajes: el chat del mensaje con el botón y la persona que lo tocó tienen que estar en la lista blanca (y ser el mismo chat privado); si no, se registra en `security.log` y no se toca la base. Los datos de cada botón se validan con un formato estricto, y un Sí/No solo vale en el mensaje que envió esa pregunta a ese chat.
5. **El token del bot vive solo en el llavero del sistema.** No está en el código, en el repo ni en la config, y se redacta de todos los logs.
6. **Sin API paga.** Del entorno de Claude Code se quitan `ANTHROPIC_*` y `CLAUDE_CODE_USE_*`, para que siempre use tu sesión de Claude Code.
7. **Actualizaciones firmadas.** Solo se instala un release cuyo manifiesto esté firmado (Ed25519) con la clave pública embebida en la app, que sea de una versión estrictamente mayor y cuyo archivo coincida en tamaño y SHA-256. La clave privada vive solo en el llavero de quien publica, nunca en el repo ni en GitHub Actions; quien la tenga puede publicar actualizaciones que las instalaciones existentes van a aceptar.

---

## Orquestador (modo v2)

> En construcción, por etapas. Hoy (etapa 5) el cerebro **ya ejecuta** (con freno de mano por defecto): proyectos, worktrees, workers en paralelo, reglas y contexto, y el cerebro decidiendo por eventos. Faltan los resúmenes periódicos y la web. Con `[orquestador] enabled = false` (el default), CoClody funciona exactamente como antes.

Un **cerebro** (otra instancia de Claude Code que solo decide) coordina varios **workers** de Claude Code que trabajan en paralelo, cada uno en su propio worktree de git del **proyecto activo**.

### Proyectos

Un proyecto es un repo de git dentro de tu carpeta de trabajo, con sus worktrees, reglas y contexto. Hay uno activo por vez.

```
coclody proyecto agregar web /ruta/al/repo --activar
coclody proyecto listar
coclody proyecto activar web
```

Desde Telegram: `/proyectos` y `/proyecto <nombre>`. Los proyectos se **agregan** solo desde la computadora: la carpeta raíz decide qué puede tocar Claude, así que no se elige desde un chat. No se puede cambiar de proyecto con tareas ejecutándose.

Al activar un proyecto, CoClody crea los worktrees que falten, dentro de la carpeta del proyecto:

```
<raíz>/.coclody/worktrees/w1   rama coclody/w1
<raíz>/.coclody/worktrees/w2   rama coclody/w2
...
```

Las ramas salen de la rama en la que esté la raíz al registrar el proyecto (la "rama base"). `.coclody/` se agrega a `.git/info/exclude` (local, no versionado). CoClody **nunca borra** un worktree: si bajás `workers`, el que sobra queda en disco y deja de usarse. El merge a tu rama principal lo hacés vos.

### Reglas y contexto

Cada proyecto tiene sus **reglas** (cómo querés que se resuelvan las cosas) y su **contexto** (qué conviene saber del proyecto). Son la base de conocimiento que, más adelante, recibirá el cerebro.

- **Reglas** (`/reglas`, o `coclody reglas`): las escribís y editás solo vos. `/reglas agregar <texto>`, `/reglas on|off <id>` para activarlas o no, `/reglas quitar <id>`. El cerebro nunca las toca (garantizado por construcción: ninguna parte de su código puede escribirlas, y un test lo verifica).
- **Contexto** (`/contexto`, o `coclody contexto`): lo que agregás vos entra aprobado. El cerebro solo podrá **proponer borradores**, que te llegan por Telegram con botones **✓ Aprobar / ✗ Rechazar** y no cuentan hasta que los aprobás. `/contexto agregar <texto>` agrega uno tuyo; `/contexto archivar <id>` saca de circulación uno aprobado (sin borrarlo).
- Las reglas habilitadas y el contexto aprobado tienen un tope de tamaño (6000 y 10000 caracteres): si una entrada no entra, se rechaza al escribirla o aprobarla, con un mensaje claro, en vez de recortar en silencio.

### Cómo trabajan los workers

- `/nueva w2 <título>` crea la tarea en el proyecto activo y la pone en la cola de w2. Con `/nueva <título>` (o el botón ➕) el bot te muestra un botón por worker para elegir. Repartir tareas es mecánico: no pasa por el cerebro ni gasta nada.
- Cada worker toma las tareas de **su** cola, de a una, en paralelo con los demás. `/lista` muestra el worker de cada tarea (`#3 [w2] …`).
- Claude Code corre con su carpeta de trabajo en el worktree del worker, con las herramientas de archivos acotadas a esa carpeta y, si `worker_commands` tiene algo, `Bash` solo para esos comandos.
- Al terminar, **CoClody** (no Claude) commitea todo lo que cambió en la rama del worker, sin hooks, con el título de la tarea y el RESUMEN de Claude. El aviso de la tarea dice el commit, cuántos archivos y líneas `+/−`, y avisa si es un **cambio grande** (`big_change_files` / `big_change_lines`).
- Antes de cada tarea, el HEAD del worker se guarda en `refs/coclody/backup/<worker>/…`: aunque algo reescriba la rama, lo anterior se recupera.
- Después de cada tarea, CoClody verifica que el worktree siga en su rama, que su HEAD no se haya movido por fuera de CoClody, que ninguna rama que no sea de un worker (`main`, la base, otras) haya cambiado o desaparecido, y que la lista de worktrees sea la misma. Si algo no cierra, la tarea falla con el motivo, y **ese worker se detiene** (no toma más tareas) hasta que revises y reinicies CoClody.
- Con el orquestador activado no corre el worker de siempre: una tarea vieja sin worker queda en cola sin ejecutarse (cancelala o asignala de nuevo con `/nueva`).

### El cerebro (simulacro)

`/cerebro` corre el **cerebro** una vez y te muestra qué haría, **sin ejecutar nada**. Es otra instancia de Claude Code, dedicada solo a decidir: recibe las reglas, el contexto aprobado, los últimos resúmenes de los workers y la cola, y responde un JSON con decisiones (encolar una tarea, preguntarte algo, responder la pregunta de un worker, proponer contexto, o esperar).

- **Simulacro inocuo por construcción.** En esta etapa no existe ningún camino de código que lleve a cabo una decisión: todas se guardan en la auditoría con estado `dry_run`. Podés dejarlo corriendo y revisar después qué habría hecho.
- **Sin herramientas.** El cerebro corre con `--tools ""` (no puede leer, escribir ni ejecutar nada; verificado con Claude real) y en una carpeta vacía fuera de los repos, para no cargar un `CLAUDE.md` del proyecto.
- **Los resúmenes son datos, no órdenes.** Entran al prompt dentro de un bloque delimitado con un marcador aleatorio por invocación (que se borra si aparece en el propio contenido), y las instrucciones fijas le ordenan tratarlos como información. Además, su salida se valida contra un **esquema cerrado, todo o nada**: si algo no cierra (JSON inválido, una clave de más, una acción que no existe, un worker inexistente), se descarta la respuesta entera y queda registrada como inválida. El esquema no puede expresar nada peligroso: no hay forma de pedir un comando, una ruta o un cambio de configuración.
- **Auditoría reconstruible.** Cada corrida guarda el prompt exacto que vio el modelo (con las reglas, el contexto y los resúmenes inline), su SHA-256, los ids de lo que entró, la salida cruda y cada decisión con su JSON (`brain_runs` y `brain_decisions`).

### Cómo decide y ejecuta el cerebro

El cerebro se invoca por **eventos** (un worker quedó libre, una tarea falló, un worker preguntó, respondiste algo, o lo llamaste con `/cerebro`), con un rato de espera para juntar eventos antes de correr. Tiene topes de invocaciones (por hora y por día): al llegar al tope, pausa y te avisa, no muere en silencio. Si devuelve algo inválido varias veces seguidas, espera cada vez más y al final se detiene y te avisa.

Hay tres modos, por config:

- **Simulacro** (`brain_dry_run = true`, el default): decide y te muestra qué haría, **no ejecuta nada**.
- **Freno de mano** (`brain_dry_run = false`, `brain_approval = true`): la primera vez que ejecuta de verdad. Cada decisión te llega por Telegram con botones **✓ Aprobar / ✗ Rechazar**, y se ejecuta recién cuando la aprobás. Nada se ejecuta sin tu visto bueno.
- **Autónomo** (`brain_approval = false`): cada decisión se ejecuta en el momento. Activalo recién cuando confíes.

Lo que una decisión puede hacer al ejecutarse: encolar una tarea para un worker (respetando los topes de cola y de tareas autónomas por día), responderle una pregunta a un worker, proponerte un borrador de contexto (que seguís aprobando vos), o preguntarte algo (te llega como pregunta del cerebro, respondés con reply). Nunca más que eso: el esquema cerrado no puede expresar un comando, una ruta ni un cambio de configuración. Cada corrida y cada decisión quedan en la auditoría.

### Qué está garantizado y cómo

| Garantía | Cómo | Tipo |
|---|---|---|
| Un worker no sale de la raíz del proyecto | Los worktrees están dentro de la raíz, y la raíz dentro de `workspace_root` (verificado después de resolver symlinks; `.coclody` no puede ser un symlink) | Por construcción |
| Un worker no toca el worktree de otro | Su carpeta de trabajo es su worktree y sus herramientas de archivos están acotadas a `./**` (el aislamiento de `./**` está verificado con Claude Code real solo en macOS); los worktrees son carpetas hermanas, nunca una dentro de otra | Por construcción |
| Los workers no ejecutan git | git no puede estar en `worker_commands` (la config lo rechaza); los commits los hace CoClody | Por construcción, **salvo** lo de la fila siguiente |
| Ni push, ni reset, ni rebase, ni borrar ramas o worktrees | Sin `worker_commands` (no hay Bash): por construcción, los workers solo leen y editan. Con comandos (tests, linters, build): esos comandos corren código del proyecto (un `conftest.py`, un script de `package.json`), que un worker puede escribir y ejecutar lo que sea. Por eso solo se permiten **con el sandbox de Claude Code** (macOS/Linux; en Windows la config los rechaza): el sandbox confina a Bash al worktree, sin leer el `.env` ni el home ni los otros worktrees, y sin red. CoClody además verifica las invariantes de git después de cada tarea y guarda ramas de respaldo. Verificado con Claude Code real en macOS, incluido el corte de red (un comando sandboxeado no logró conectarse ni a un listener local). | Construcción sin comandos; con comandos, el sandbox contiene y git queda **por instrucción** |
| Un worker no escribe el `.git`, `.gitattributes` ni `.gitmodules` | Reglas de denegación (`--disallowedTools`) que ganan a lo preaprobado: en un worktree `.git` es un puntero que, editado, ejecutaría código en el próximo git. Verificado con Claude Code real. | Por construcción |
| Sin secretos alcanzables desde un comando del worker | Con sandbox: el home queda denegado por defecto (SSH, llavero, datos de CoClody) y se reabre solo el worktree; la raíz del proyecto (con su `.env` y los worktrees vecinos) se deniega explícitamente; la red se corta. Sin comandos no hay forma de leer fuera del worktree. | Por construcción (con sandbox o sin comandos) |
| El git de CoClody no ejecuta código del repo | Cada llamada lleva `core.hooksPath` a una carpeta vacía (si tiene algo, no se ejecuta git), `core.fsmonitor=false`, sin paginador, y no hereda variables `GIT_*` | Por construcción |
| El git de CoClody no habla con un remoto | `protocol.allow=never` en cada llamada | Por construcción |
| `main` y las ramas protegidas no se tocan | Las ramas de los workers son siempre `coclody/<worker>`; `main`, `master` y la rama base se rechazan | Por construcción |
| El cerebro no puede cambiar config, reglas, lista blanca ni nada de CoClody | Corre sin herramientas y su única salida es un JSON con esquema cerrado, sin acciones para eso *(etapa 4)* | Por construcción |
| El cerebro trata los resúmenes de los workers como datos | Delimitados con un marcador aleatorio por invocación y la orden en su prompt *(etapa 4)* | **Solo instrucción** (la validación del esquema acota el daño) |

`worker_commands` acepta solo prefijos simples (`pytest`, `python -m pytest`, `npm test`, `ruff check .`): sin comillas, `;`, `|`, `&`, `$`, redirecciones, y nunca git, shells, red, `sudo`, `env`, `xargs`, `rm` ni un intérprete suelto (`python` sin `-m` o script). Esa lista es la *intención*; la contención real la pone el sandbox. Por eso **solo se permiten comandos en macOS y Linux** (sandbox disponible); en Windows la config los rechaza y los workers quedan sin Bash. La **red está cortada** dentro del sandbox: si un build la necesita (bajar dependencias), va a fallar; corré ese paso vos antes. `coclody check` avisa cuando la lista no está vacía y verifica que git tenga identidad (`user.name`/`user.email`), sin la cual los commits de los workers fallarían.

Otras contenciones:

- **Límite de uso de la suscripción.** Si Claude Code reporta el límite, la tarea no falla: vuelve a la cola, CoClody pausa a todos los workers, te avisa por Telegram y reintenta tras un enfriamiento. Una tarea que lo toca una y otra vez se marca como fallida al llegar a `max_task_runs` ejecuciones.
- **Un worker que se detiene** (por una invariante de git violada) no toma más tareas hasta que lo reactivás con `/worker <id> reanudar`, sin reiniciar todo. `/worker` lista el estado de cada uno.
- **La computadora no se duerme** mientras el orquestador está activo (`caffeinate` en macOS, `SetThreadExecutionState` en Windows), para no cortar una tarea a la mitad.
- **Archivos sensibles.** Si un commit toca CI (`.github/workflows`), configs que ejecutan código (`conftest.py`, `package.json`, `Makefile`…), `.claude`/editor o dependencias, el aviso de la tarea lo marca para que lo revises con cuidado antes de mergear.

---

## Referencia de configuración

`config.toml` está en `%LOCALAPPDATA%\CoClody\` (Windows) o en `~/Library/Application Support/CoClody/` (macOS).

| Clave | Por defecto | |
|---|---|---|
| `telegram.allowed_chat_ids` | (obligatoria) | Chat IDs autorizados. Ver supuestos de seguridad. |
| `claude.workspace_root` | (obligatoria) | Carpeta de trabajo. Tiene que existir; no puede ser tu carpeta personal ni contener los datos de CoClody. |
| `claude.executable` | `"claude"` | Nombre en el PATH o ruta absoluta al ejecutable de Claude Code. |
| `claude.allowed_tools` | `["Read(./**)", "Glob(./**)", "Grep(./**)", "Edit(./**)", "Write(./**)"]` | Herramientas que Claude Code puede usar, limitadas a la carpeta de trabajo. Antes de ampliarla, leé [Qué puede tocar Claude](#qué-puede-tocar-claude). `"Bash"` sin acotar no se admite. |
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

Huecos de tests conocidos (el código existe, pero no hay un test que lo ejercite de punta a punta):

- **Bucles de fondo armados.** El worker, el notificador y las actualizaciones se prueban por separado. Solo `tests/test_shutdown.py` corre `run_polling` de verdad, y únicamente para el apagado.
- **Mensajes que no son texto.** Un audio o una foto mandados como respuesta a una pregunta reciben "No entendí", sin orientar.
- **Límite de envíos de Telegram (`RetryAfter`).** No está probado, y el notificador reintenta a los 3 segundos sin respetar la espera que pide Telegram.
- **Payloads sintéticos.** Los updates de prueba se arman a mano con la forma de la Bot API. Lo ideal es guardar un update real, sin el texto, como fixture.

Otros:

- **Vigilancia del polling con un atributo interno.** Si el polling muere (p. ej. `InvalidToken`), python-telegram-bot no para la aplicación ni lo expone. `polling_failure()` mira la tarea interna `Updater._Updater__polling_task`; si una versión futura la renombra, se registra un aviso ("No se puede vigilar el polling") y un test lo detecta. Al detectarlo, se registra un ERROR una sola vez y el worker y el notificador quedan en pausa hasta reiniciar.

- `questions.tg_chat_id`, `questions.tg_message_id` y `repo.set_question_message()` quedaron sin uso desde la migración v3: las respuestas por reply se resuelven con `notification_deliveries`. Se mantienen para no migrar solo para quitarlas.
- **Proceso huérfano en macOS/Linux.** Claude Code corre en una sesión propia. Si CoClody muere de golpe o se cierra la Terminal donde corre, Claude y sus procesos siguen sin timeout. Al reiniciar, la tarea se marca como fallida, pero el proceso viejo puede seguir trabajando en la misma carpeta. En Windows no pasa: el Job Object mata el árbol cuando CoClody termina.
- **Aislamiento sin verificar en Windows.** Las reglas `(./**)` se verificaron con Claude Code real solo en macOS. Las pruebas automáticas del Job Object (que los nietos mueren) corren solo en Windows, en CI.
- **No hay rotación de la clave de firma.** `keygen` nunca reemplaza la clave existente, y las instalaciones solo aceptan la clave pública que tienen embebida. Si la clave privada se pierde o se compromete, hay que publicar una versión con una clave nueva y distribuirla a mano.
