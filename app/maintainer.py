"""
Core token maintenance logic (Python port of vps_token_maintain.sh).
Adds: backup folder rotation (402 → backup), CPA API reload support.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("maintainer")

LOG_DIR = Path("/data/logs")
STATE_DIR = Path("/data/state")
INBOX_DIR = Path("/data/inbox")
WORK_DIR = Path("/data/work")


@dataclass
class MaintainConfig:
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
    # CPA settings
    cpa_api_url: str = ""
    cpa_auth_url: str = ""
    cpa_auth_fallback_url: str = ""
    cpa_token: str = ""

    @property
    def repos(self) -> list[str]:
        repos = [r.strip() for r in self.repo_list.replace(";", ",").split(",") if r.strip()]
        return repos

    @property
    def auth_path(self) -> Path:
        return Path(self.auth_dir)

    @property
    def backup_path(self) -> Path:
        return Path(self.backup_dir)

    @property
    def age_key_path(self) -> Path:
        return Path(self.age_identity_path)


class TokenMaintainer:
    """Main orchestrator. Mirrors step1/step2/step3 of the bash script."""

    def __init__(self, cfg: MaintainConfig):
        self.cfg = cfg
        self._run_id = time.strftime("%Y%m%d-%H%M%S")
        for d in [LOG_DIR, STATE_DIR, INBOX_DIR, WORK_DIR, cfg.auth_path, cfg.backup_path]:
            d.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"run-{self._run_id}.log"
        logging.basicConfig(
            level=logging.INFO,
            format="[%(asctime)s] %(levelname)s %(message)s",
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(),
            ],
            force=True,
        )

    def _count_auths(self) -> int:
        return len(list(self.cfg.auth_path.glob("*.json")))

    def _count_backup(self) -> int:
        return len(list(self.cfg.backup_path.glob("*.json")))

    # ------------------------------------------------------------------
    # Step 1  –  Sync GitHub Releases (incremental)
    # ------------------------------------------------------------------
    async def _sync_releases(self) -> int:
        """Returns total new tokens added."""
        added = 0
        async with httpx.AsyncClient(timeout=30) as client:
            for repo in self.cfg.repos:
                added += await self._sync_repo(client, repo)
        return added

    async def _sync_repo(self, client: httpx.AsyncClient, repo: str) -> int:
        state_file = STATE_DIR / f"state-{repo.replace('/', '_')}.json"
        last_processed = ""
        if state_file.exists():
            try:
                last_processed = json.loads(state_file.read_text()).get("last_processed_published_at", "")
            except Exception:
                pass

        logger.info(f"sync repo={repo} last_processed={last_processed or '<none>'}")
        resp = await client.get(f"https://api.github.com/repos/{repo}/releases?per_page=50")
        resp.raise_for_status()
        releases = resp.json()

        candidates = sorted(
            [
                {"tag": r["tag_name"], "ts": r.get("published_at") or r.get("created_at", "")}
                for r in releases
                if r["tag_name"].startswith("tokens-")
            ],
            key=lambda x: x["ts"],
        )

        added = 0
        processed = 0
        for c in candidates:
            tag, ts = c["tag"], c["ts"]
            if last_processed and ts <= last_processed:
                continue
            if processed >= self.cfg.max_per_run:
                break

            newly = await self._download_and_apply(client, repo, tag)
            added += newly
            processed += 1
            last_processed = ts

            state_file.write_text(
                json.dumps(
                    {
                        "last_processed_tag": tag,
                        "last_processed_published_at": ts,
                        "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    },
                    indent=2,
                )
            )

        return added

    async def _download_and_apply(self, client: httpx.AsyncClient, repo: str, tag: str) -> int:
        safe_repo = repo.replace("/", "_")
        asset_name = "tokens.zip.age" if self.cfg.use_age else "tokens.zip"
        base_url = f"https://github.com/{repo}/releases/download/{tag}"

        manifest_url = f"{base_url}/manifest.json"
        asset_url = f"{base_url}/{asset_name}"

        # Download manifest
        manifest_resp = await client.get(manifest_url)
        manifest_resp.raise_for_status()
        manifest = manifest_resp.json()
        expected_sha = manifest.get("sha256", "")
        if not expected_sha:
            raise ValueError(f"manifest missing sha256 for {repo}@{tag}")

        # Download asset
        inbox_path = INBOX_DIR / f"tokens-{safe_repo}-{tag}.{asset_name.split('.')[-1]}"
        if self.cfg.use_age:
            inbox_path = INBOX_DIR / f"tokens-{safe_repo}-{tag}.zip.age"
        else:
            inbox_path = INBOX_DIR / f"tokens-{safe_repo}-{tag}.zip"

        async with client.stream("GET", asset_url) as r:
            r.raise_for_status()
            with open(inbox_path, "wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)

        # Verify sha256
        sha256 = hashlib.sha256(inbox_path.read_bytes()).hexdigest()
        if sha256 != expected_sha:
            inbox_path.unlink(missing_ok=True)
            raise ValueError(f"sha256 mismatch for {repo}@{tag}: expected={expected_sha} got={sha256}")

        logger.info(f"sha256 ok repo={repo} tag={tag}")

        # Decrypt if needed
        zip_path = WORK_DIR / f"tokens-{safe_repo}-{tag}.zip"
        if self.cfg.use_age:
            result = subprocess.run(
                ["age", "-d", "-i", str(self.cfg.age_key_path), "-o", str(zip_path), str(inbox_path)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"age decryption failed: {result.stderr}")
        else:
            shutil.copy2(inbox_path, zip_path)

        # Unzip
        unzip_dir = WORK_DIR / f"unzipped-{safe_repo}-{tag}"
        unzip_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(unzip_dir)

        src_dir = unzip_dir / "codex"
        if not src_dir.exists():
            raise FileNotFoundError(f"zip missing codex/ directory for {repo}@{tag}")

        # Copy-no-overwrite into auth_dir
        added = 0
        for jf in src_dir.glob("*.json"):
            dest = self.cfg.auth_path / jf.name
            if not dest.exists():
                shutil.copy2(jf, dest)
                os.chmod(dest, 0o600)
                added += 1

        # Cleanup
        zip_path.unlink(missing_ok=True)
        shutil.rmtree(unzip_dir, ignore_errors=True)
        inbox_path.unlink(missing_ok=True)

        logger.info(f"sync done repo={repo} tag={tag} added={added}")
        return added

    # ------------------------------------------------------------------
    # Step 2  –  Full scan
    # ------------------------------------------------------------------
    async def _full_scan(self) -> dict[str, Any]:
        files = sorted(self.cfg.auth_path.glob("*.json"))
        stats = {"ok": 0, "invalid_401": 0, "no_quota_402": 0, "other": 0, "skip": 0, "deleted_401": 0, "moved_402": 0}
        items: list[dict] = []

        async with httpx.AsyncClient(timeout=self.cfg.timeout_sec) as client:
            for f in files:
                item = await self._check_token(client, f, stats)
                items.append(item)
                if self.cfg.sleep_sec > 0:
                    await asyncio.sleep(self.cfg.sleep_sec)

        return {"stats": stats, "items": items}

    async def _check_token(
        self, client: httpx.AsyncClient, f: Path, stats: dict[str, Any]
    ) -> dict[str, Any]:
        name = f.name
        try:
            data = json.loads(f.read_text())
        except Exception:
            stats["skip"] += 1
            return {"name": name, "result": "SKIP", "reason": "invalid json"}

        access = data.get("access_token") or data.get("accessToken", "")
        acc = data.get("account_id") or data.get("chatgpt_account_id") or data.get("chatgptAccountId", "")

        if not access or not acc:
            stats["skip"] += 1
            return {"name": name, "result": "SKIP", "reason": "missing fields"}

        headers = {
            "accept": "application/json",
            "user-agent": "codex_cli_rs/universal (Windows)",
            "authorization": f"Bearer {access}",
            "chatgpt-account-id": acc,
        }

        try:
            resp = await client.get("https://chatgpt.com/backend-api/wham/usage", headers=headers)
            code = resp.status_code
        except Exception as e:
            stats["other"] += 1
            return {"name": name, "result": "OTHER", "reason": str(e)}

        if code == 200:
            stats["ok"] += 1
            return {"name": name, "result": "OK", "reason": "200"}
        elif code == 401:
            stats["invalid_401"] += 1
            stats["deleted_401"] += 1
            f.unlink(missing_ok=True)
            logger.info(f"deleted 401 token: {name}")
            return {"name": name, "result": "INVALID_401", "reason": "401 – deleted"}
        elif code == 402:
            stats["no_quota_402"] += 1
            # Move to backup folder instead of deleting
            dest = self.cfg.backup_path / name
            if not dest.exists():
                shutil.move(str(f), dest)
                os.chmod(dest, 0o600)
                stats["moved_402"] += 1
                logger.info(f"moved 402 token to backup: {name}")
            else:
                f.unlink(missing_ok=True)
                logger.info(f"removed duplicate 402 token: {name}")
            return {"name": name, "result": "NO_QUOTA_402", "reason": "402 – moved to backup"}
        else:
            stats["other"] += 1
            return {"name": name, "result": "OTHER", "reason": f"http_{code}"}

    # ------------------------------------------------------------------
    # Step 3  –  Telegram notification
    # ------------------------------------------------------------------
    async def _send_tg(self, msg: str) -> None:
        if not self.cfg.tg_bot_token:
            logger.info("TG_BOT_TOKEN empty; skip tg send")
            return
        if not self.cfg.tg_chat_id:
            logger.warning("TG_BOT_TOKEN set but TG_CHAT_ID empty")
            return
        url = f"https://api.telegram.org/bot{self.cfg.tg_bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, data={"chat_id": self.cfg.tg_chat_id, "text": msg})
                if resp.status_code == 200:
                    logger.info("tg sent")
                else:
                    logger.warning(f"tg send failed: {resp.status_code}")
        except Exception as e:
            logger.warning(f"tg send exception: {e}")

    # ------------------------------------------------------------------
    # Step 4  –  Reload CPA (optional)
    # ------------------------------------------------------------------
    async def _reload_cpa(self) -> None:
        """Call CPA reload endpoint if configured. Falls back to fallback URL."""
        if not self.cfg.cpa_api_url or not self.cfg.cpa_token:
            logger.info("CPA reload skipped (cpa_api_url or cpa_token not configured)")
            return

        headers = {"Authorization": f"Bearer {self.cfg.cpa_token}", "Content-Type": "application/json"}
        urls = [u for u in [self.cfg.cpa_auth_url, self.cfg.cpa_auth_fallback_url] if u]

        async with httpx.AsyncClient(timeout=15, base_url=self.cfg.cpa_api_url) as client:
            for endpoint in urls:
                try:
                    resp = await client.post(endpoint, headers=headers)
                    if resp.status_code < 300:
                        logger.info(f"CPA reload OK via {endpoint}: {resp.status_code}")
                        return
                    else:
                        logger.warning(f"CPA reload {endpoint} returned {resp.status_code}; trying fallback")
                except Exception as e:
                    logger.warning(f"CPA reload {endpoint} error: {e}; trying fallback")

        logger.error("CPA reload failed on all endpoints")

    # ------------------------------------------------------------------
    # Main orchestration
    # ------------------------------------------------------------------
    async def run(self) -> dict[str, Any]:
        logger.info(f"=== run started run_id={self._run_id} ===")

        pre_auths = self._count_auths()
        logger.info(f"pre_auths={pre_auths} pre_backup={self._count_backup()}")

        # Step 1: sync
        added = 0
        if self.cfg.repos:
            try:
                added = await self._sync_releases()
            except Exception as e:
                logger.error(f"sync error: {e}")
        else:
            logger.warning("No repos configured; skipping sync")

        post_sync = self._count_auths()
        logger.info(f"post_sync_auths={post_sync} added={added}")

        # Step 2: full scan
        scan_result = await self._full_scan()
        stats = scan_result["stats"]

        post_scan = self._count_auths()
        logger.info(f"post_scan_auths={post_scan} stats={stats}")

        # Step 4: reload CPA
        await self._reload_cpa()

        summary = {
            "run_id": self._run_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "added": added,
            "pre_auths": pre_auths,
            "post_sync_total": post_sync,
            "remain": post_scan,
            "backup": self._count_backup(),
            **stats,
        }

        logger.info(f"summary={json.dumps(summary)}")

        # Step 3: TG
        msg = (
            f"token-maintain run\n"
            f"新增: {added}\n"
            f"同步后: {post_sync}\n"
            f"检查: total={pre_auths} ok={stats['ok']} 401={stats['invalid_401']} 402={stats['no_quota_402']} other={stats['other']}\n"
            f"删除401: {stats['deleted_401']}  移至备份402: {stats['moved_402']}\n"
            f"剩余: {post_scan}  备份: {self._count_backup()}"
        )
        await self._send_tg(msg)

        logger.info("=== run done ===")
        return summary
