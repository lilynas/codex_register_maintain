"""
scheduler.py – runs the token maintain job on a fixed interval.
Can be used standalone (python scheduler.py) or alongside the WebUI.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger("scheduler")

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/data/config.json"))
INTERVAL_HOURS = float(os.environ.get("SCHEDULE_HOURS", "1"))


def _load_config():
    defaults = {
        "repo_list": "", "auth_dir": "/data/auths", "backup_dir": "/data/auths_backup",
        "use_age": True, "age_identity_path": "/data/age.key",
        "tg_bot_token": "", "tg_chat_id": "",
        "max_per_run": 20, "sleep_sec": 0.2, "timeout_sec": 12,
        "cpa_api_url": "", "cpa_auth_url": "", "cpa_auth_fallback_url": "", "cpa_token": "",
    }
    if CONFIG_FILE.exists():
        try:
            return {**defaults, **json.loads(CONFIG_FILE.read_text())}
        except Exception:
            pass
    return defaults


async def _run_once():
    from app.maintainer import MaintainConfig, TokenMaintainer
    cfg_dict = _load_config()
    cfg = MaintainConfig(**cfg_dict)
    maintainer = TokenMaintainer(cfg)
    summary = await maintainer.run()
    logger.info(f"schedule run done: {summary}")


async def main():
    logger.info(f"scheduler started, interval={INTERVAL_HOURS}h")
    while True:
        try:
            await _run_once()
        except Exception as e:
            logger.error(f"scheduler run error: {e}", exc_info=True)
        await asyncio.sleep(INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    asyncio.run(main())
