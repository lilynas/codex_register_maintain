"""
codex_register_maintain WebUI
FastAPI backend serving the dashboard and API endpoints.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.maintainer import MaintainConfig, TokenMaintainer

ROOT = Path(__file__).parent
CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/data/config.json"))

app = FastAPI(title="codex_register_maintain WebUI", version="1.0.0")

templates = Jinja2Templates(directory=str(ROOT / "templates"))

# -------------------------
# Persist config to disk
# -------------------------

def _default_config() -> dict[str, Any]:
    return {
        "repo_list": "",
        "auth_dir": "/data/auths",
        "backup_dir": "/data/auths_backup",
        "use_age": True,
        "age_identity_path": "/data/age.key",
        "tg_bot_token": "",
        "tg_chat_id": "",
        "max_per_run": 20,
        "sleep_sec": 0.2,
        "timeout_sec": 12,
        # CPA settings
        "cpa_api_url": "",
        "cpa_auth_url": "",
        "cpa_auth_fallback_url": "",
        "cpa_token": "",
    }


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return {**_default_config(), **json.loads(CONFIG_FILE.read_text())}
        except Exception:
            pass
    return _default_config()


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


# -------------------------
# State
# -------------------------

_last_run_summary: dict[str, Any] | None = None
_run_lock = asyncio.Lock()
_is_running = False


# -------------------------
# Routes
# -------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    cfg = load_config()
    return templates.TemplateResponse("index.html", {"request": request, "config": cfg})


class ConfigModel(BaseModel):
    repo_list: str = ""
    auth_dir: str = "/data/auths"
    backup_dir: str = "/data/auths_backup"
    use_age: bool = True
    age_identity_path: str = "/data/age.key"
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    max_per_run: int = 20
    sleep_sec: float = 0.2
    timeout_sec: int = 12
    cpa_api_url: str = ""
    cpa_auth_url: str = ""
    cpa_auth_fallback_url: str = ""
    cpa_token: str = ""


@app.get("/api/config")
async def get_config():
    return load_config()


@app.post("/api/config")
async def set_config(body: ConfigModel):
    cfg = body.model_dump()
    save_config(cfg)
    return {"ok": True}


@app.get("/api/status")
async def get_status():
    cfg = load_config()
    auth_dir = Path(cfg["auth_dir"])
    backup_dir = Path(cfg["backup_dir"])

    auth_count = len(list(auth_dir.glob("*.json"))) if auth_dir.exists() else 0
    backup_count = len(list(backup_dir.glob("*.json"))) if backup_dir.exists() else 0

    return {
        "is_running": _is_running,
        "last_run_summary": _last_run_summary,
        "auth_count": auth_count,
        "backup_count": backup_count,
    }


@app.post("/api/run")
async def trigger_run(background_tasks: BackgroundTasks):
    global _is_running
    if _is_running:
        raise HTTPException(status_code=409, detail="A run is already in progress")

    cfg = load_config()
    background_tasks.add_task(_do_run, cfg)
    return {"ok": True, "message": "Run started in background"}


async def _do_run(cfg: dict[str, Any]):
    global _is_running, _last_run_summary
    async with _run_lock:
        _is_running = True
        try:
            mc = MaintainConfig(**cfg)
            maintainer = TokenMaintainer(mc)
            summary = await maintainer.run()
            _last_run_summary = summary
        except Exception as e:
            _last_run_summary = {"error": str(e)}
        finally:
            _is_running = False


@app.get("/api/logs")
async def get_logs():
    """Return recent log lines from the latest run log file."""
    cfg = load_config()
    log_dir = Path("/data/logs")
    if not log_dir.exists():
        return {"lines": []}
    log_files = sorted(log_dir.glob("run-*.log"), reverse=True)
    if not log_files:
        return {"lines": []}
    latest = log_files[0]
    try:
        lines = latest.read_text(errors="replace").splitlines()[-200:]
        return {"lines": lines, "file": latest.name}
    except Exception as e:
        return {"lines": [f"Error reading log: {e}"]}


@app.get("/api/tokens")
async def list_tokens():
    """List all tokens in auth_dir with their status from the last report."""
    cfg = load_config()
    auth_dir = Path(cfg["auth_dir"])
    backup_dir = Path(cfg["backup_dir"])

    def _collect(d: Path, kind: str):
        if not d.exists():
            return []
        results = []
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                acc = data.get("account_id") or data.get("chatgpt_account_id") or data.get("chatgptAccountId", "")
                results.append({"name": f.name, "account_id": acc, "kind": kind})
            except Exception:
                results.append({"name": f.name, "account_id": "", "kind": kind})
        return results

    return {
        "active": _collect(auth_dir, "active"),
        "backup": _collect(backup_dir, "backup"),
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
