# WSL + Windows PostgreSQL for MCUM

This setup lets the Windows MCUM and the Ubuntu/OpenClaw MCUM share the same PostgreSQL memory.

## What is already solved

- MCUM now rewrites `localhost` database URLs automatically when running inside WSL.
- OpenClaw can point to the same MCUM codebase through a symlink instead of a manual copy.
- MCUM skill catalog metadata now preserves paths per runtime, so Windows and WSL do not overwrite each other permanently.

## Remaining host requirement

Windows Firewall must allow TCP `5432` from the WSL subnet.

Run this script as Administrator in Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File ".agent\skills\MCUM\integrations\windows_wsl_postgres_firewall.ps1"
```

## WSL runtime bootstrap

Create the MCUM venv and install the runtime dependencies:

```bash
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv
python3 -m venv ~/.openclaw/workspace/mcum-venv
~/.openclaw/workspace/mcum-venv/bin/python -m pip install --upgrade pip
~/.openclaw/workspace/mcum-venv/bin/python -m pip install psycopg[binary] psycopg_pool python-dotenv pgvector
```

## OpenClaw symlink

Point OpenClaw to the workspace MCUM instead of copying files:

```bash
sudo rm -rf /usr/lib/node_modules/openclaw/skills/MCUM
sudo ln -s "/mnt/c/Users/dev/workspace/.agent/skills/MCUM" \
  /usr/lib/node_modules/openclaw/skills/MCUM
```

## Validation

```bash
~/.openclaw/workspace/mcum-venv/bin/python - <<'PY'
import sys
sys.path.insert(0, '/usr/lib/node_modules/openclaw/skills')
from MCUM.db.connection import health_check
print(health_check())
PY
```

Expected result after the firewall rule is applied:

```text
{
  "connected": true,
  "schemas": {"core_brain": true, "project_registry": true},
  "pgvector": true,
  "pgvector_installed": true
}
```
