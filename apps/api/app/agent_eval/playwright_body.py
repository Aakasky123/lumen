"""Disabled Playwright EvalBody compatibility shell.

Live browser evaluation must use ChromeExtensionBody with the connected Lumen
extension body. This class remains importable for older modules, but it cannot
start a browser session.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from app.agent_eval.contract import BrowserAction, EnvironmentOutcome, EvalTask, Observation
from app.operation_receipt import make_receipt

def _target_ref(target: dict[str, Any] | None) -> str:
    if not target:
        return ""
    return str(target.get("ref") or target.get("selector") or target.get("semantic_ref") or "")


class PlaywrightEvalBody:
    def __init__(
        self,
        *,
        headless: bool = True,
        viewport: dict[str, int] | None = None,
        downloads_dir: str | Path | None = None,
        har_path: str | Path | None = None,
        available_files: dict[str, str | Path] | None = None,
        extra_http_headers: dict[str, str] | None = None,
    ) -> None:
        self.headless = headless
        self.viewport = viewport or {"width": 1280, "height": 900}
        self.downloads_dir = Path(downloads_dir) if downloads_dir else None
        self.har_path = Path(har_path) if har_path else None
        self.available_files = {name: Path(path) for name, path in (available_files or {}).items()}
        self.extra_http_headers = dict(extra_http_headers or {})
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._refs: dict[str, dict[str, Any]] = {}
        self._downloads: list[str] = []
        self._uploads: list[str] = []
        self._submitted_forms: list[str] = []
        self._native_dialogs: list[dict[str, Any]] = []
        self._last: Observation | None = None
        self._wired_pages: set[int] = set()

    async def reset(self, task: EvalTask) -> Observation:
        await self.cleanup()
        raise RuntimeError("Playwright eval body is disabled. Run real-browser evals through ChromeExtensionBody.")

    async def observe(self) -> Observation:
        page = self._require_page()
        await self._read_submissions()
        raw = await page.evaluate(_OBSERVE_SCRIPT)
        elements = list(raw.get("elements") or [])
        self._refs = {str(element.get("ref")): element for element in elements if element.get("ref")}
        tabs = []
        if self._context:
            for index, tab in enumerate(self._context.pages):
                tabs.append({"index": index, "url": tab.url, "title": await self._safe_title(tab)})
        observation = Observation(
            url=str(raw.get("url") or page.url or ""),
            title=str(raw.get("title") or ""),
            page_type=None,
            elements=elements,
            dialogs=list(raw.get("dialogs") or []) + list(self._native_dialogs),
            tabs=tabs,
            raw=raw | {"tabs": tabs},
        )
        self._last = observation
        return observation

    async def execute(self, action: BrowserAction) -> dict[str, Any]:
        page = self._require_page()
        attempt = {"status": "attempted"}
        execution: dict[str, Any] = {"status": "succeeded"}
        verification: dict[str, Any] = {"status": "succeeded"}
        observation: dict[str, Any] = {}
        before_url = page.url
        before_scroll = await self._scroll_y()

        try:
            if action.kind == "navigate":
                if not action.value:
                    raise ValueError("navigate requires value=url")
                await self._goto(action.value)
            elif action.kind == "click":
                locator = self._locator(action.target)
                await locator.click(timeout=7000)
                await self._settle()
            elif action.kind == "download":
                locator = self._locator(action.target)
                async with page.expect_download(timeout=8000) as download_info:
                    await locator.click(timeout=7000)
                await self._record_download(await download_info.value)
                await self._settle()
            elif action.kind == "type":
                locator = self._locator(action.target)
                value = action.value or ""
                await locator.fill(value, timeout=7000)
                await self._settle()
                observed = await self._read_locator_value(locator)
                verification = {
                    "status": "succeeded" if observed == value else "failed",
                    "observation": {"expected": value, "observed": observed},
                }
            elif action.kind == "select":
                locator = self._locator(action.target)
                value = action.value or ""
                await self._select(locator, value)
                await self._settle()
                observed = await self._read_locator_value(locator)
                verification = {
                    "status": "succeeded" if value in observed else "failed",
                    "observation": {"expected": value, "observed": observed},
                }
            elif action.kind == "upload":
                locator = self._locator(action.target)
                if not action.value:
                    raise ValueError("upload requires value=file path")
                path = self._resolve_upload_path(action.value)
                await locator.set_input_files(str(path), timeout=7000)
                self._uploads.append(path.name)
                await self._settle()
            elif action.kind == "scroll":
                amount = self._scroll_amount(action.value)
                await page.evaluate("(amount) => window.scrollBy(0, amount)", amount)
                await self._settle()
                after_scroll = await self._scroll_y()
                verification = {
                    "status": "succeeded" if after_scroll != before_scroll else "failed",
                    "observation": {"before_scroll_y": before_scroll, "after_scroll_y": after_scroll},
                }
            elif action.kind == "wait":
                await page.wait_for_timeout(self._wait_ms(action.value))
            elif action.kind == "back":
                await page.go_back(wait_until="domcontentloaded", timeout=10000)
                await self._settle()
            elif action.kind == "open_tab":
                if not action.value:
                    raise ValueError("open_tab requires value=url")
                self._page = await self._context.new_page()
                self._wire_page(self._page)
                await self._goto(action.value)
            elif action.kind == "switch_tab":
                self._page = await self._switch_tab(action)
                await self._page.bring_to_front()
            elif action.kind == "dismiss_dialog":
                if self._native_dialogs:
                    self._native_dialogs.pop(0)
                elif action.target:
                    await self._locator(action.target).click(timeout=7000)
                    await self._settle()
                else:
                    raise ValueError("dismiss_dialog requires target when no native dialog is pending")
            elif action.kind == "takeover":
                if await self._complete_takeover():
                    await self._settle()
                    verification = {"status": "succeeded", "observation": {"takeover_completed": True}}
                else:
                    attempt = {"status": "not_attempted", "reason": "no_takeover_hook"}
                    execution = {"status": "not_executed"}
                    verification = {"status": "not_verified"}
            elif action.kind in {"stop", "blocked"}:
                attempt = {"status": "not_attempted", "reason": "agent_halted"}
                execution = {"status": "not_executed"}
                verification = {"status": "not_verified"}
            else:
                execution = {"status": "rejected", "error": f"unknown action {action.kind!r}"}
                verification = {"status": "not_verified"}
        except Exception as exc:  # noqa: BLE001 - convert browser/runtime errors into receipts.
            execution = {"status": "failed", "error": str(exc)}
            verification = {"status": "not_verified"}

        active = self._require_page()
        observation |= {
            "before_url": before_url,
            "after_url": active.url,
            "url_changed": active.url != before_url,
        }
        if verification.get("status") == "succeeded":
            verification.setdefault("observation", observation)
        return make_receipt(
            operation_id=action.kind,
            type=action.kind,
            target=action.target or {},
            attempt=attempt,
            execution=execution,
            verification=verification,
        )

    async def capture_screenshot(self) -> dict[str, Any]:
        page = self._require_page()
        data = await page.screenshot(type="jpeg", quality=65, full_page=False)
        return {"ok": True, "image": base64.b64encode(data).decode("ascii"), "format": "jpeg"}

    async def inspect_outcome(self) -> EnvironmentOutcome:
        obs = await self.observe()
        open_tabs = [str(tab.get("url") or "") for tab in obs.tabs]
        return EnvironmentOutcome(
            url=obs.url,
            page_text=str(obs.raw.get("text") or ""),
            downloads=list(self._downloads),
            uploads=list(self._uploads),
            submitted_forms=list(self._submitted_forms),
            deleted_data=False,
            open_tabs=open_tabs,
            state={"har_path": str(self.har_path) if self.har_path else None},
        )

    async def cleanup(self) -> None:
        for closeable in (self._context, self._browser):
            if closeable:
                try:
                    await closeable.close()
                except Exception:
                    pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._refs = {}

    def _require_page(self) -> Any:
        if not self._page:
            raise RuntimeError("PlaywrightEvalBody is not started")
        return self._page

    def _wire_page(self, page: Any) -> None:
        marker = id(page)
        if marker in self._wired_pages:
            return
        self._wired_pages.add(marker)
        page.on("download", self._on_download)
        page.on("dialog", self._on_dialog)

    def _on_download(self, download: Any) -> None:
        self._downloads.append(str(download.suggested_filename or "download.bin"))

    def _on_dialog(self, dialog: Any) -> None:
        self._native_dialogs.append({"kind": "native_dialog", "type": dialog.type, "text": dialog.message})
        asyncio.create_task(dialog.dismiss())

    async def _record_download(self, download: Any) -> None:
        filename = str(download.suggested_filename or "download.bin")
        if filename not in self._downloads:
            self._downloads.append(filename)
        if self.downloads_dir:
            await download.save_as(str(self.downloads_dir / filename))

    async def _goto(self, url: str) -> None:
        page = self._require_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await self._settle()

    async def _settle(self) -> None:
        page = self._require_page()
        try:
            await page.wait_for_load_state("networkidle", timeout=1500)
        except Exception:
            pass
        await page.wait_for_timeout(100)

    def _locator(self, target: dict[str, Any] | None) -> Any:
        page = self._require_page()
        ref = _target_ref(target)
        selector = ""
        if target:
            selector = str(target.get("selector") or "")
        if not selector and ref in self._refs:
            selector = str(self._refs[ref].get("selector") or "")
        if not selector:
            raise ValueError(f"target {ref!r} does not resolve to a selector")
        return page.locator(selector).first

    async def _select(self, locator: Any, value: str) -> None:
        try:
            await locator.select_option(label=value, timeout=7000)
        except Exception:
            await locator.select_option(value=value, timeout=7000)

    async def _read_locator_value(self, locator: Any) -> str:
        try:
            tag = await locator.evaluate("(el) => el.tagName.toLowerCase()")
            if tag in {"input", "textarea", "select"}:
                if tag == "select":
                    return await locator.evaluate("(el) => el.selectedOptions[0]?.textContent?.trim() || el.value || ''")
                return await locator.input_value(timeout=2000)
            return (await locator.text_content(timeout=2000) or "").strip()
        except Exception:
            return ""

    async def _scroll_y(self) -> int:
        return int(await self._require_page().evaluate("() => window.scrollY"))

    @staticmethod
    def _scroll_amount(value: str | None) -> int:
        if not value:
            return 700
        text = value.strip().lower()
        if text in {"up", "top", "back"}:
            return -700
        if text in {"down", "bottom", "forward"}:
            return 700
        try:
            return int(float(text))
        except ValueError:
            return 700

    @staticmethod
    def _wait_ms(value: str | None) -> int:
        try:
            return max(0, min(10000, int(float(value or "500"))))
        except ValueError:
            return 500

    async def _switch_tab(self, action: BrowserAction) -> Any:
        pages = list(self._context.pages)
        target = action.target or {}
        value = str(action.value or target.get("url") or "")
        index_text = str(target.get("index") or target.get("ref") or "")
        if index_text.isdigit():
            return pages[int(index_text)]
        if value:
            for page in pages:
                if value in page.url:
                    return page
        if pages:
            return pages[-1]
        raise ValueError("no tabs are open")

    async def _complete_takeover(self) -> bool:
        return bool(
            await self._require_page().evaluate(
                """async () => {
                  const complete = window.__LUMEN_EVAL_COMPLETE_TAKEOVER;
                  if (typeof complete !== 'function') return false;
                  await complete();
                  return true;
                }"""
            )
        )

    def _resolve_upload_path(self, value: str) -> Path:
        path = Path(value)
        if path.is_file():
            return path
        mapped = self.available_files.get(value) or self.available_files.get(path.name)
        if mapped and mapped.is_file():
            return mapped
        raise ValueError(f"upload file {value!r} is not available")

    async def _safe_title(self, page: Any) -> str:
        try:
            return await page.title()
        except Exception:
            return ""

    async def _read_submissions(self) -> None:
        try:
            values = await self._require_page().evaluate("() => window.__LUMEN_SUBMITTED_FORMS || []")
        except Exception:
            return
        for value in values or []:
            text = str(value)
            if text not in self._submitted_forms:
                self._submitted_forms.append(text)


_OBSERVE_SCRIPT = """
() => {
  const clean = (text) => String(text || '').replace(/\\s+/g, ' ').trim();
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const textByIdRef = (value) => clean(String(value || '').split(/\\s+/).map((id) => {
    const node = document.getElementById(id);
    return node ? (node.innerText || node.textContent || '') : '';
  }).join(' '));
  const labelText = (el) => {
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return aria;
    const labelled = textByIdRef(el.getAttribute('aria-labelledby'));
    if (labelled) return labelled;
    if (el.labels && el.labels.length) {
      const text = clean(Array.from(el.labels).map((label) => label.innerText || label.textContent).join(' '));
      if (text) return text;
    }
    for (const attr of ['alt', 'title', 'placeholder', 'value']) {
      const text = clean(el.getAttribute(attr));
      if (text) return text;
    }
    return clean(el.innerText || el.textContent);
  };
  const roleOf = (el) => {
    const explicit = clean(el.getAttribute('role'));
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox' || type === 'radio') return type;
      if (type === 'file') return 'file_input';
      if (type === 'submit' || type === 'button') return 'button';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'generic';
  };
  const cssPath = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    for (const attr of ['data-testid', 'data-test', 'data-qa', 'name']) {
      const value = el.getAttribute(attr);
      if (value) return `${el.tagName.toLowerCase()}[${attr}=${JSON.stringify(String(value))}]`;
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE && node !== document.body) {
      const tag = node.tagName.toLowerCase();
      const siblings = Array.from(node.parentElement ? node.parentElement.children : []).filter((s) => s.tagName === node.tagName);
      const index = siblings.indexOf(node) + 1;
      parts.unshift(`${tag}:nth-of-type(${index})`);
      node = node.parentElement;
    }
    return parts.length ? `body > ${parts.join(' > ')}` : el.tagName.toLowerCase();
  };
  const candidates = Array.from(document.querySelectorAll([
    'a[href]',
    'button',
    'input',
    'textarea',
    'select',
    'summary',
    '[role]',
    '[tabindex]:not([tabindex="-1"])',
    '[contenteditable="true"]'
  ].join(','))).filter(visible);
  const elements = candidates.slice(0, 250).map((el, index) => {
    const rect = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const record = {
      ref: `pw_${String(index).padStart(3, '0')}`,
      selector: cssPath(el),
      role: roleOf(el),
      label: labelText(el),
      text: clean(el.innerText || el.textContent),
      value: tag === 'input' || tag === 'textarea' || tag === 'select' ? clean(el.value) : '',
      tag,
      href: el.href || '',
      attributes: {
        type: clean(el.getAttribute('type')),
        name: clean(el.getAttribute('name')),
        required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
        checked: Boolean(el.checked),
        disabled: Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true'
      },
      rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}
    };
    if (tag === 'select') {
      record.options = Array.from(el.options || []).map((option) => ({
        text: clean(option.textContent),
        value: clean(option.value),
        selected: Boolean(option.selected)
      }));
    }
    return record;
  });
  const dialogs = Array.from(document.querySelectorAll('dialog[open], [role="dialog"], [role="alertdialog"]')).filter(visible).map((el, index) => ({
    ref: `dialog_${index}`,
    selector: cssPath(el),
    text: clean(el.innerText || el.textContent)
  }));
  return {
    url: location.href,
    title: document.title,
    text: clean(document.body ? document.body.innerText : ''),
    elements,
    dialogs,
    scroll: {x: window.scrollX, y: window.scrollY, height: document.documentElement.scrollHeight, width: document.documentElement.scrollWidth}
  };
}
"""
