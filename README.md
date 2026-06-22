# MCUM — Motor Cerebral Ultra Multiversal

> **PostgreSQL-native orchestration brain & memory for any MCP agent** (Claude Code, Codex, OpenCode, Antigravity, OpenClaw…). 100% local. One command installs it.
>
> **Cerebro de orquestación y memoria, nativo en PostgreSQL, para cualquier agente MCP.** 100% local. Se instala con un comando.

**🇬🇧 English** · [🇪🇸 Español](#-español)

---

## 🇬🇧 English

### Install (one command)

Open a terminal **inside your project folder** and run:

```bash
npx mcum-orchestrator init
```

That's it. The command will:

1. **Install MCUM into your workspace** (`<your-folder>/MCUM`) — permanent, not a temp cache.
2. **Ask for your PostgreSQL password** (entered twice to confirm) **only to CREATE a dedicated, isolated database** for this workspace. Your other data stays untouched.
3. **Apply the schema** and a minimal seed (a few example skills; memory starts blank).
4. **Register your installed agents** (Claude Code, OpenCode, …) for this folder.
5. **Write `CLAUDE.md` + `AGENTS.md`** so every agent uses MCUM for **every** task (intake → memory → record).

Then **restart your agent** in that folder. It will auto-detect MCUM and record every task.

### Requirements — just Node + Python
- **Node** 18+ and **Python** 3.10+. **Nothing else.**
- **No PostgreSQL or Docker needed:** if neither is found, MCUM downloads and runs a **portable, persistent PostgreSQL** for you (a private DB per workspace; survives reboots/agent restarts; no window).
- If you DO have Docker → it uses `pgvector` (HNSW, faster). If you have your own PostgreSQL → `npx mcum-orchestrator init --db-prompt` to use it.

### Uninstall (one command)
```bash
npx mcum-orchestrator uninstall --workspace .
```
Stops the embedded PostgreSQL, deletes its data, unregisters the agents and removes the install — **only this workspace on this machine**. (Add `--keep-db` to keep the database data.)

### What is MCUM?
An audit + orchestration + persistent-memory layer: 30 `mcum_*` MCP tools, semantic memory (pgvector/HNSW), pattern learning, self-improving skills (SISL), 7 quality gates. The agent never talks to the DB directly: **agent → Node bridge → Python brain → PostgreSQL**.

### Docs
- [INSTALL.md](INSTALL.md) — step by step (EN/ES) · [ARCHITECTURE.md](ARCHITECTURE.md) · [CONFIGURACION.md](CONFIGURACION.md) · [EMBEDDING_BACKENDS.md](EMBEDDING_BACKENDS.md)

### License
[MIT](LICENSE).

---

## 🇪🇸 Español

### Instalación (un solo comando)

Abre una terminal **dentro de la carpeta de tu proyecto** y ejecuta:

```bash
npx mcum-orchestrator init
```

Eso es todo. El comando:

1. **Instala MCUM en tu workspace** (`<tu-carpeta>/MCUM`) — permanente, no en un caché temporal.
2. **Te pide tu clave de PostgreSQL** (se ingresa dos veces para confirmar) **solo para CREAR una base de datos dedicada y aislada** para este workspace. Tus otros datos no se tocan.
3. **Aplica el esquema** y un seed mínimo (algunos skills de ejemplo; la memoria arranca en blanco).
4. **Registra tus agentes instalados** (Claude Code, OpenCode, …) para esta carpeta.
5. **Escribe `CLAUDE.md` + `AGENTS.md`** para que cada agente use MCUM en **cada** tarea (intake → memoria → registro).

Luego **reinicia tu agente** en esa carpeta. Detectará MCUM automáticamente y registrará cada tarea.

### Requisitos — solo Node + Python
- **Node** 18+ y **Python** 3.10+. **Nada más.**
- **No necesitas PostgreSQL ni Docker:** si no hay ninguno, MCUM descarga y corre un **PostgreSQL portable y persistente** por ti (una BD privada por workspace; sobrevive reinicios/cierres del agente; sin ventana).
- Si tienes Docker → usa `pgvector` (HNSW, más rápido). Si tienes tu propio PostgreSQL → `npx mcum-orchestrator init --db-prompt` para usarlo.

### Desinstalar (un comando)
```bash
npx mcum-orchestrator uninstall --workspace .
```
Detiene el PostgreSQL embebido, borra su data, des-registra los agentes y elimina la instalación — **solo este workspace en esta máquina**. (Agrega `--keep-db` para conservar los datos de la base.)

### ¿Qué es MCUM?
Una capa de auditoría + orquestación + memoria persistente: 30 tools MCP `mcum_*`, memoria semántica (pgvector/HNSW), aprendizaje de patrones, auto-mejora de skills (SISL), 7 compuertas de calidad. El agente nunca habla con la BD directo: **agente → puente Node → cerebro Python → PostgreSQL**.

### Documentación
- [INSTALL.md](INSTALL.md) — paso a paso (EN/ES) · [ARCHITECTURE.md](ARCHITECTURE.md) · [CONFIGURACION.md](CONFIGURACION.md) · [EMBEDDING_BACKENDS.md](EMBEDDING_BACKENDS.md)

### Licencia
[MIT](LICENSE).
