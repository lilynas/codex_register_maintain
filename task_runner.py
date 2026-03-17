#!/usr/bin/env python3
"""
task_runner.py - 业务注册脚本入口
该脚本由 GitHub Action 定时启动。
要求：运行后在 codex/ 目录下生成若干 .json 文件（Token 文件）。
"""

import json
import os
import re
import time
import random
import secrets
import hashlib
import base64
import threading
import argparse
from dataclasses import dataclass
from typing import Any, Dict, Optional
import urllib.parse
import logging

from curl_cffi import requests as _raw_requests

logging.basicConfig(
    level=getattr(logging, os.getenv("OPENAI_REG_LOG_LEVEL", "DEBUG").upper(), logging.DEBUG),
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)

class _StdLogger:
    def __init__(self) -> None:
        self._logger = logging.getLogger("openai_reg")

    def _fmt(self, message: str, *args: Any) -> str:
        return message.format(*args) if args else message

    def debug(self, message: str, *args: Any) -> None:
        self._logger.debug(self._fmt(message, *args))

    def info(self, message: str, *args: Any) -> None:
        self._logger.info(self._fmt(message, *args))

    def warning(self, message: str, *args: Any) -> None:
        self._logger.warning(self._fmt(message, *args))

    def error(self, message: str, *args: Any) -> None:
        self._logger.error(self._fmt(message, *args))

    def exception(self, message: str, *args: Any) -> None:
        self._logger.exception(self._fmt(message, *args))

logger = _StdLogger()

def _is_tls_error(e: Exception) -> bool:
    """判断是否为 TLS/SSL/curl 握手错误"""
    msg = str(e)
    return "TLS" in msg or "SSL" in msg or "curl" in msg.lower()

def _request_with_tls_retry(method: str, url: str, *, session=None, max_retries: int = 3, **kwargs):
    """带 TLS 快速重试的请求包装器，重试间隔 0.5s"""
    caller = session if session else _raw_requests
    for attempt in range(1, max_retries + 1):
        try:
            return getattr(caller, method)(url, **kwargs)
        except Exception as e:
            if _is_tls_error(e) and attempt < max_retries:
                logger.warning(
                    "TLS 快速重试: method={} url={} attempt={}/{} error={}",
                    method.upper(),
                    url[:80],
                    attempt,
                    max_retries,
                    e,
                )
                time.sleep(0.5 * attempt)
                continue
            raise

class _RetrySession:
    """包装 curl_cffi Session，所有请求自动带 TLS 重试"""
    def __init__(self, **kwargs):
        self._s = _raw_requests.Session(**kwargs)

    def __getattr__(self, name):
        return getattr(self._s, name)

    def get(self, url, **kwargs):
        return _request_with_tls_retry("get", url, session=self._s, **kwargs)

    def post(self, url, **kwargs):
        return _request_with_tls_retry("post", url, session=self._s, **kwargs)

class requests:
    """带 TLS 重试的 requests 命名空间"""
    Session = _RetrySession

    @staticmethod
    def get(url, **kwargs):
        return _request_with_tls_retry("get", url, **kwargs)

    @staticmethod
    def post(url, **kwargs):
        return _request_with_tls_retry("post", url, **kwargs)

# ==========================================
# tempmail.ing 免费临时邮箱 API
# ==========================================

TEMPMAIL_BASE = "https://api.tempmail.ing/api"
TEMPMAIL_HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://tempmail.ing",
    "referer": "https://tempmail.ing/",
}

_GENERATE_MIN_INTERVAL = 15.0
_GENERATE_MAX_RETRIES = 3
_GENERATE_RETRY_BACKOFF = 10.0
_INBOX_POLL_INTERVAL = 4.0
_INBOX_POLL_TIMEOUT = 150.0
_RATE_LIMIT_WAIT = 5.0
_DOMAIN_BLACKLIST: set[str] = {"animatimg.com", "tempmail.ing"}
_BLACKLIST_MAX_RETRIES = 5
_last_generate_ts = 0.0
_generate_lock = threading.Lock()

def _rate_limited_generate(proxies: Any = None) -> dict:
    global _last_generate_ts
    for attempt in range(1, _GENERATE_MAX_RETRIES + 1):
        with _generate_lock:
            now = time.monotonic()
            wait = _GENERATE_MIN_INTERVAL - (now - _last_generate_ts)
            if wait > 0:
                time.sleep(wait)
            _last_generate_ts = time.monotonic()
        try:
            resp = requests.post(f"{TEMPMAIL_BASE}/generate", headers=TEMPMAIL_HEADERS, json={"duration": 10}, proxies=proxies, impersonate="chrome", timeout=15)
            if resp.status_code == 429:
                time.sleep(_RATE_LIMIT_WAIT + _GENERATE_RETRY_BACKOFF * attempt)
                continue
            data = resp.json()
            if resp.status_code == 200 and data.get("success"):
                return data
        except Exception as e:
            logger.warning("tempmail /generate 异常: {}", e)
        time.sleep(_GENERATE_RETRY_BACKOFF * attempt)
    return {}

_temp_password = ""

def get_email_and_token(proxies: Any = None) -> tuple:
    global _temp_password
    for bl_attempt in range(1, _BLACKLIST_MAX_RETRIES + 1):
        data = _rate_limited_generate(proxies)
        if not data: continue
        email = str((data.get("email") or {}).get("address") or "").strip()
        if not email: continue
        domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        if domain in _DOMAIN_BLACKLIST: continue
        _temp_password = ""
        return email, email, ""
    return "", "", ""

def _mail_sender(msg: Dict[str, Any]) -> str:
    return " ".join(str(part or "").strip() for part in [msg.get("from"), msg.get("sender"), msg.get("from_address"), msg.get("from_name")] if str(part or "").strip())

def _mail_content(msg: Dict[str, Any]) -> str:
    return "\n".join(str(part or "") for part in [msg.get("subject"), msg.get("text"), msg.get("content"), msg.get("body"), msg.get("html")] if str(part or ""))

def _looks_like_openai_mail(msg: Dict[str, Any]) -> bool:
    haystack = f"{_mail_sender(msg)}\n{_mail_content(msg)}".lower()
    return any(keyword in haystack for keyword in ("openai", "chatgpt", "otp@tm1.openai.com"))

def get_oai_code(token: str, email: str, proxies: Any = None) -> str:
    regex = r"(?<!\d)(\d{6})(?!\d)"
    seen_ids: set[str] = set()
    encoded_email = urllib.parse.quote(email, safe="")
    deadline = time.monotonic() + _INBOX_POLL_TIMEOUT
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{TEMPMAIL_BASE}/emails/{encoded_email}", headers={"accept": "*/*", "referer": "https://tempmail.ing/"}, proxies=proxies, impersonate="chrome", timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for msg in data.get("emails") or []:
                    msg_id = str(msg.get("id") or msg.get("messageId") or "").strip()
                    if not msg_id or msg_id in seen_ids: continue
                    seen_ids.add(msg_id)
                    if _looks_like_openai_mail(msg):
                        m = re.search(regex, _mail_content(msg))
                        if m: return m.group(1)
        except Exception: pass
        time.sleep(_INBOX_POLL_INTERVAL)
    return ""

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_SCOPE = "openid email profile offline_access"

def _b64url_no_pad(raw: bytes) -> str: return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
def _sha256_b64url_no_pad(s: str) -> str: return _b64url_no_pad(hashlib.sha256(s.encode("ascii")).digest())
def _random_state(nbytes: int = 16) -> str: return secrets.token_urlsafe(nbytes)
def _pkce_verifier() -> str: return secrets.token_urlsafe(64)

def _parse_callback_url(callback_url: str) -> Dict[str, Any]:
    candidate = callback_url.strip()
    parsed = urllib.parse.urlparse(candidate)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)
    for k, v in fragment.items():
        if k not in query: query[k] = v
    def get1(k: str) -> str: return (query.get(k, [""])[0] or "").strip()
    return {"code": get1("code"), "state": get1("state"), "error": get1("error"), "error_description": get1("error_description")}

def _jwt_claims_no_verify(id_token: str) -> Dict[str, Any]:
    if not id_token or id_token.count(".") < 2: return {}
    payload_b64 = id_token.split(".")[1]
    pad = "=" * ((4 - (len(payload_b64) % 4)) % 4)
    try: return json.loads(base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii")).decode("utf-8"))
    except Exception: return {}

def _decode_jwt_segment(seg: str) -> Dict[str, Any]:
    pad = "=" * ((4 - (len(seg) % 4)) % 4)
    try: return json.loads(base64.urlsafe_b64decode((seg + pad).encode("ascii")).decode("utf-8"))
    except Exception: return {}

def _post_form(url: str, data: Dict[str, str], proxies: Any = None) -> Dict[str, Any]:
    resp = requests.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, proxies=proxies, impersonate="chrome", timeout=60)
    if resp.status_code != 200: raise RuntimeError(f"token exchange failed: {resp.status_code}")
    return resp.json()

@dataclass(frozen=True)
class OAuthStart:
    auth_url: str
    state: str
    code_verifier: str
    redirect_uri: str

def generate_oauth_url(redirect_uri: str = DEFAULT_REDIRECT_URI) -> OAuthStart:
    state = _random_state()
    code_verifier = _pkce_verifier()
    params = {"client_id": CLIENT_ID, "response_type": "code", "redirect_uri": redirect_uri, "scope": DEFAULT_SCOPE, "state": state, "code_challenge": _sha256_b64url_no_pad(code_verifier), "code_challenge_method": "S256", "prompt": "login", "id_token_add_organizations": "true", "codex_cli_simplified_flow": "true"}
    return OAuthStart(auth_url=f"{AUTH_URL}?{urllib.parse.urlencode(params)}", state=state, code_verifier=code_verifier, redirect_uri=redirect_uri)

def submit_callback_url(callback_url: str, expected_state: str, code_verifier: str, redirect_uri: str, proxy: Optional[str]) -> str:
    cb = _parse_callback_url(callback_url)
    if cb["error"]: raise RuntimeError(f"oauth error: {cb['error']}")
    token_resp = _post_form(TOKEN_URL, {"grant_type": "authorization_code", "client_id": CLIENT_ID, "code": cb["code"], "redirect_uri": redirect_uri, "code_verifier": code_verifier}, proxies={"http": proxy, "https": proxy} if proxy else None)
    claims = _jwt_claims_no_verify(token_resp.get("id_token", ""))
    now = int(time.time())
    config = {
        "id_token": token_resp.get("id_token"),
        "access_token": token_resp.get("access_token"),
        "refresh_token": token_resp.get("refresh_token"),
        "account_id": (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id"),
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "email": claims.get("email"),
        "password": _temp_password,
        "proxy_url": proxy,
        "type": "codex",
        "expired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + int(token_resp.get("expires_in", 0))))
    }
    return json.dumps(config, ensure_ascii=False)

def run_registration(proxy: Optional[str]) -> Optional[str]:
    s = requests.Session(proxies={"http": proxy, "https": proxy} if proxy else None, impersonate="chrome")
    try:
        email, dev_token, _ = get_email_and_token()
        if not email: return None
        oauth = generate_oauth_url()
        resp = s.get(oauth.auth_url, timeout=100)
        did = s.cookies.get("oai-did")
        if not did: return None
        sen_resp = requests.post("https://sentinel.openai.com/backend-api/sentinel/req", headers={"origin": "https://sentinel.openai.com", "content-type": "text/plain;charset=UTF-8"}, data=f'{{"p":"","id":"{did}","flow":"authorize_continue"}}', proxies={"http": proxy, "https": proxy} if proxy else None, impersonate="chrome")
        sentinel = f'{{"p": "", "t": "", "c": "{sen_resp.json()["token"]}", "id": "{did}", "flow": "authorize_continue"}}'
        s.post("https://auth.openai.com/api/accounts/authorize/continue", headers={"openai-sentinel-token": sentinel, "content-type": "application/json"}, data=f'{{"username":{{"value":"{email}","kind":"email"}},"screen_hint":"signup"}}')
        sen_resp2 = requests.post("https://sentinel.openai.com/backend-api/sentinel/req", headers={"origin": "https://sentinel.openai.com", "content-type": "text/plain;charset=UTF-8"}, data=f'{{"p":"","id":"{did}","flow":"username_password_create"}}', proxies={"http": proxy, "https": proxy} if proxy else None, impersonate="chrome")
        sentinel2 = f'{{"p": "", "t": "", "c": "{sen_resp2.json()["token"]}", "id": "{did}", "flow": "username_password_create"}}'
        global _temp_password
        _temp_password = secrets.token_urlsafe(16) + "!A1"
        s.post("https://auth.openai.com/api/accounts/user/register", headers={"openai-sentinel-token": sentinel2, "content-type": "application/json"}, data=json.dumps({"password": _temp_password, "username": email}))
        code = get_oai_code(dev_token, email)
        if not code: return None
        s.post("https://auth.openai.com/api/accounts/email-otp/validate", headers={"content-type": "application/json"}, data=f'{{"code":"{code}"}}')
        s.post("https://auth.openai.com/api/accounts/create_account", headers={"content-type": "application/json"}, data='{"name":"Neo","birthdate":"2000-02-20"}')
        auth_cookie = s.cookies.get("oai-client-auth-session")
        auth_json = _decode_jwt_segment(auth_cookie.split(".")[0])
        workspace_id = auth_json["workspaces"][0]["id"]
        select_resp = s.post("https://auth.openai.com/api/accounts/workspace/select", headers={"content-type": "application/json"}, data=f'{{"workspace_id":"{workspace_id}"}}')
        current_url = select_resp.json()["continue_url"]
        for _ in range(6):
            final_resp = s.get(current_url, allow_redirects=False, timeout=30)
            location = final_resp.headers.get("Location")
            if not location: break
            next_url = urllib.parse.urljoin(current_url, location)
            if "code=" in next_url: return submit_callback_url(next_url, oauth.state, oauth.code_verifier, oauth.redirect_uri, proxy)
            current_url = next_url
    except Exception: logger.exception("捕获异常")
    return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="只运行一次")
    parser.add_argument("--proxy", default=None, help="代理地址")
    args = parser.parse_args()
    
    output_dir = "codex"
    os.makedirs(output_dir, exist_ok=True)
    
    while True:
        logger.info("开始注册流程...")
        token_json = run_registration(args.proxy)
        if token_json:
            t_data = json.loads(token_json)
            fname = f"token_{t_data['email'].replace('@','_')}_{int(time.time())}.json"
            with open(os.path.join(output_dir, fname), "w", encoding="utf-8") as f:
                f.write(token_json)
            logger.info("注册成功: {}", fname)
        else:
            logger.error("本次注册失败")
        
        if args.once: break
        time.sleep(10)

if __name__ == "__main__":
    main()
