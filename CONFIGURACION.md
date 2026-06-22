# MCUM — Configuration / Configuración

**🇬🇧 English** · [🇪🇸 Español](#-español)

All config lives in `.env` (copied from `.env.example`). `.env` is gitignored — never commit it.
Toda la configuración vive en `.env` (copia de `.env.example`). `.env` está en `.gitignore` — nunca lo subas.

---

## 🇬🇧 English

### Change the database password
The installer prompts for it and creates a dedicated DB. To change it later:
1. In PostgreSQL: `ALTER USER postgres WITH PASSWORD 'new';`
2. In `<workspace>/MCUM/.env`: update `DB_PASSWORD` and `DATABASE_URL`.
3. Restart your agent.

### Key variables (`.env`)
| Variable | Default | Purpose |
|----------|---------|---------|
| `DB_PASSWORD` / `DATABASE_URL` | (set at install) | PostgreSQL credentials. |
| `DB_NAME` | `mcum_<workspace>` | Dedicated, isolated database. |
| `MCUM_EMBEDDING_BACKEND` | `onnx` | `onnx` \| `sentence-transformers` \| `hash`. |
| `MCUM_EMBEDDING_CACHE_DIR` | per-OS | Model cache. |
| `HF_HUB_OFFLINE` | `1` | Block the Hugging Face network round-trip. |
| `MCUM_GRAPH_MAX_CODE_PROJECTION_NODES` | `15000` | Skip code projection for oversized graphs. |

### Embedding backend
```bash
py mcum_embedding.py status | use onnx | bench onnx      # Windows
python3 mcum_embedding.py status | use onnx | bench onnx # Linux/macOS
```
Switching backends never re-embeds (vectors are identical). See [EMBEDDING_BACKENDS.md](EMBEDDING_BACKENDS.md).

### Database — 3 ways
| Way | Command | Needs |
|-----|---------|-------|
| Your PostgreSQL (default) | `npx mcum-orchestrator init` (prompts password) | PostgreSQL + pgvector |
| Docker | `docker compose up -d` inside `<workspace>/MCUM` | Docker |
| Embedded | `node db/embedded_pg.mjs start` | nothing (no pgvector) |

---

## 🇪🇸 Español

### Cambiar la clave de la base de datos
El instalador la pide y crea una BD dedicada. Para cambiarla luego:
1. En PostgreSQL: `ALTER USER postgres WITH PASSWORD 'nueva';`
2. En `<workspace>/MCUM/.env`: actualiza `DB_PASSWORD` y `DATABASE_URL`.
3. Reinicia tu agente.

### Variables principales (`.env`)
| Variable | Default | Para qué |
|----------|---------|----------|
| `DB_PASSWORD` / `DATABASE_URL` | (al instalar) | Credenciales de PostgreSQL. |
| `DB_NAME` | `mcum_<workspace>` | Base dedicada y aislada. |
| `MCUM_EMBEDDING_BACKEND` | `onnx` | `onnx` \| `sentence-transformers` \| `hash`. |
| `MCUM_EMBEDDING_CACHE_DIR` | por SO | Caché del modelo. |
| `HF_HUB_OFFLINE` | `1` | Bloquea el round-trip de red al Hugging Face Hub. |
| `MCUM_GRAPH_MAX_CODE_PROJECTION_NODES` | `15000` | Salta la proyección de código en grafos enormes. |

### Backend de embeddings
```bash
py mcum_embedding.py status | use onnx | bench onnx      # Windows
python3 mcum_embedding.py status | use onnx | bench onnx # Linux/macOS
```
Cambiar de backend nunca re-embebe (los vectores son idénticos). Ver [EMBEDDING_BACKENDS.md](EMBEDDING_BACKENDS.md).

### Base de datos — 3 formas
| Forma | Comando | Requiere |
|-------|---------|----------|
| Tu PostgreSQL (default) | `npx mcum-orchestrator init` (pide clave) | PostgreSQL + pgvector |
| Docker | `docker compose up -d` dentro de `<workspace>/MCUM` | Docker |
| Embebida | `node db/embedded_pg.mjs start` | nada (sin pgvector) |
