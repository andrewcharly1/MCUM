# MCUM — Install / Instalación

**🇬🇧 English** · [🇪🇸 Español](#-español)

---

## 🇬🇧 English

### One command (run inside your project folder)
```bash
npx mcum-orchestrator init
```

**Step by step, what happens:**
1. **Permanent install** into `<your-folder>/MCUM` (npx ephemeral cache is detected and the package relocates itself into your workspace).
2. **Dependencies** — installs any missing Python (`psycopg`, `pgvector`, `fastembed`) and the Node MCP bridge (`@modelcontextprotocol/sdk`). Already-present ones are skipped.
3. **Database** — you are asked for your **PostgreSQL password (twice, to confirm)**. It is used **only to CREATE a dedicated, isolated database** (`mcum_<folder>`). The schema (pgvector + HNSW) and a minimal seed are applied. Works without `psql` (falls back to psycopg).
4. **Health check** — verifies node, Python deps, and that PostgreSQL is reachable.
5. **Agent registration** — writes `.mcp.json` / `opencode.json` for the detected agents in this folder.
6. **Governance** — writes `CLAUDE.md` + `AGENTS.md` so MCUM is mandatory for every task.

Then **restart your agent**. Ask it to call `mcum_local_health` to confirm.

### Requirements
- Node 18+, Python 3.10+. **Nothing else** — if you have no PostgreSQL and no Docker, MCUM runs a portable, persistent PostgreSQL automatically. Docker → uses pgvector. Your own PostgreSQL → add `--db-prompt`.

### Uninstall
```bash
npx mcum-orchestrator uninstall --workspace .
```
Stops the embedded DB, deletes its data, unregisters agents, removes the install (this workspace only). `--keep-db` keeps the data.

### Per-OS notes
| | Windows | Linux | macOS |
|---|---------|-------|-------|
| Python launcher | `py` | `python3` | `python3` |
| Command | `npx mcum-orchestrator init` | same | same |

The command is identical on all three; the Python launcher is detected automatically.

### Non-interactive (CI / scripts)
```bash
npx mcum-orchestrator init --db-password "<PASS>" --db-name mcum_app --create-db
```

### Troubleshooting
- **"PostgreSQL not reachable"** → your password was wrong or PostgreSQL isn't running. Re-run and re-enter it, or use Docker.
- **Agent doesn't see MCUM** → make sure you installed inside the workspace (not via a one-off you cancelled) and **restarted** the agent.

---

## 🇪🇸 Español

### Un comando (ejecútalo dentro de la carpeta de tu proyecto)
```bash
npx mcum-orchestrator init
```

**Paso a paso, qué ocurre:**
1. **Instalación permanente** en `<tu-carpeta>/MCUM` (detecta el caché temporal de npx y se reubica en tu workspace).
2. **Dependencias** — instala lo que falte de Python (`psycopg`, `pgvector`, `fastembed`) y el puente MCP de Node (`@modelcontextprotocol/sdk`). Lo ya instalado se omite.
3. **Base de datos** — te pide tu **clave de PostgreSQL (dos veces, para confirmar)**. Se usa **solo para CREAR una base dedicada y aislada** (`mcum_<carpeta>`). Se aplica el esquema (pgvector + HNSW) y un seed mínimo. Funciona sin `psql` (usa psycopg como respaldo).
4. **Chequeo de salud** — verifica node, deps de Python y que PostgreSQL responda.
5. **Registro de agentes** — escribe `.mcp.json` / `opencode.json` para los agentes detectados en esta carpeta.
6. **Gobernanza** — escribe `CLAUDE.md` + `AGENTS.md` para que MCUM sea obligatorio en cada tarea.

Luego **reinicia tu agente**. Pídele que llame `mcum_local_health` para confirmar.

### Requisitos
- Node 18+, Python 3.10+. **Nada más** — si no tienes PostgreSQL ni Docker, MCUM corre un PostgreSQL portable y persistente automáticamente. Con Docker → usa pgvector. Con tu propio PostgreSQL → agrega `--db-prompt`.

### Desinstalar
```bash
npx mcum-orchestrator uninstall --workspace .
```
Detiene la BD embebida, borra su data, des-registra agentes, elimina la instalación (solo este workspace). `--keep-db` conserva los datos.

### Notas por sistema operativo
| | Windows | Linux | macOS |
|---|---------|-------|-------|
| Lanzador Python | `py` | `python3` | `python3` |
| Comando | `npx mcum-orchestrator init` | igual | igual |

El comando es idéntico en los tres; el lanzador de Python se detecta solo.

### No interactivo (CI / scripts)
```bash
npx mcum-orchestrator init --db-password "<CLAVE>" --db-name mcum_app --create-db
```

### Solución de problemas
- **"PostgreSQL not reachable"** → la clave fue incorrecta o PostgreSQL no está corriendo. Repite e ingrésala de nuevo, o usa Docker.
- **El agente no ve MCUM** → asegúrate de haber instalado dentro del workspace y de **reiniciar** el agente.
