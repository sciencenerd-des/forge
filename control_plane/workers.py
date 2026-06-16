from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BrowserActionRecord, BrowserSessionRecord, ExternalActionRecord
from .service import ConflictError, append_event, now_utc

MAX_RESULT_BYTES = 64_000


def execute_external_action(db: Session, action_id: str) -> ExternalActionRecord:
    action = db.get(ExternalActionRecord, action_id)
    if not action:
        raise ValueError("external action not found")
    if action.status == "completed":
        return action
    if action.status not in {"queued", "failed"}:
        raise ConflictError(f"external action cannot execute from status {action.status}")
    action.status = "running"
    db.commit()
    try:
        if action.action_type != "https_webhook":
            raise ValueError(f"unsupported external action type: {action.action_type}")
        result = _send_https_webhook(action.request_payload)
        action.status = "completed"
        action.result_payload = result
        action.completed_at = now_utc()
        append_event(db, action.run_id, "external_action.completed", "external-worker", {"action_id": action.id, "status": result["status"]})
    except Exception as error:
        action.status = "failed"
        action.result_payload = {"error": str(error)[:2_000]}
        append_event(db, action.run_id, "external_action.failed", "external-worker", {"action_id": action.id, "error": str(error)[:500]})
    db.commit()
    return action


def _send_https_webhook(payload: dict) -> dict:
    url = str(payload.get("url", ""))
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("webhook URL must be credential-free HTTPS")
    body = payload.get("body", {})
    encoded = json.dumps(body).encode()
    if len(encoded) > 1_000_000:
        raise ValueError("webhook body exceeds 1 MB")
    request = urllib.request.Request(url, data=encoded, method="POST", headers={"Content-Type": "application/json", "User-Agent": "ForgeHarness/0.1"})
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=30) as response:
            content = response.read(MAX_RESULT_BYTES + 1)
            return {"status": response.status, "body": content[:MAX_RESULT_BYTES].decode(errors="replace"), "truncated": len(content) > MAX_RESULT_BYTES}
    except urllib.error.HTTPError as error:
        raise ValueError(f"webhook returned HTTP {error.code}") from error


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are disabled", headers, fp)


async def execute_next_browser_action(db: Session, session_id: str) -> BrowserActionRecord | None:
    session = db.get(BrowserSessionRecord, session_id)
    if not session:
        raise ValueError("browser session not found")
    action = db.scalar(select(BrowserActionRecord).where(BrowserActionRecord.session_id == session_id, BrowserActionRecord.status == "queued").order_by(BrowserActionRecord.sequence).limit(1))
    if not action:
        return None
    action.status = "running"
    db.commit()
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:
        action.status = "failed"
        action.result = {"error": "Playwright is not installed; run playwright install chromium"}
        db.commit()
        raise RuntimeError(action.result["error"]) from error

    storage = Path(session.storage_path) if session.storage_path else None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(storage) if storage and storage.exists() else None)
            page = await context.new_page()
            if session.current_url:
                await _navigate(page, session.current_url, session.allowed_hosts)
            result = await _perform_browser_action(page, action, session.allowed_hosts)
            session.current_url = page.url
            action.status = "completed"
            action.result = result
            await browser.close()
        append_event(db, session.run_id, "browser.action_completed", "browser-worker", {"action_id": action.id, "action": action.action})
    except Exception as error:
        action.status = "failed"
        action.result = {"error": str(error)[:2_000]}
        append_event(db, session.run_id, "browser.action_failed", "browser-worker", {"action_id": action.id, "error": str(error)[:500]})
    db.commit()
    return action


async def _perform_browser_action(page, action: BrowserActionRecord, allowed_hosts: list[str]) -> dict:
    arguments = action.arguments
    if action.action == "navigate":
        await _navigate(page, str(arguments.get("url", "")), allowed_hosts)
        return {"url": page.url, "title": await page.title()}
    if action.action == "snapshot":
        return {"url": page.url, "title": await page.title(), "content": (await page.locator("body").inner_text())[:MAX_RESULT_BYTES]}
    if action.action == "screenshot":
        data = await page.screenshot(full_page=bool(arguments.get("full_page", False)))
        return {"bytes": len(data), "note": "binary screenshot storage adapter not configured"}
    selector = str(arguments.get("selector", ""))
    if not selector:
        raise ValueError("selector is required")
    locator = page.locator(selector)
    if await locator.count() != 1:
        raise ValueError("selector must resolve to exactly one element")
    if action.action == "click":
        await locator.click()
    elif action.action == "type":
        await locator.fill(str(arguments.get("text", "")))
    elif action.action == "press":
        await locator.press(str(arguments.get("key", "")))
    elif action.action == "scroll":
        await page.mouse.wheel(int(arguments.get("x", 0)), int(arguments.get("y", 500)))
    else:
        raise ValueError(f"unsupported browser action: {action.action}")
    return {"url": page.url, "action": action.action}


async def _navigate(page, url: str, allowed_hosts: list[str]) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in allowed_hosts:
        raise ValueError("navigation host is not allowlisted")
    response = await page.goto(url, wait_until="domcontentloaded")
    final_host = (urlparse(page.url).hostname or "").lower()
    if final_host not in allowed_hosts:
        raise ValueError("navigation redirected to a non-allowlisted host")
    if response and response.status >= 400:
        raise ValueError(f"navigation returned HTTP {response.status}")


def run_browser_worker_once(db: Session, session_id: str) -> BrowserActionRecord | None:
    return asyncio.run(execute_next_browser_action(db, session_id))

