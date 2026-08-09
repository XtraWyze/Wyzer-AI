"""Built-in managed-browser tools for Wyzer on Windows.

The pack controls a dedicated Edge or Chrome profile through Chromium's Chrome
DevTools Protocol (CDP). Each tool call reconnects to the existing browser, so
it remains compatible with Wyzer's spawned worker-process executor.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus, urlparse

import psutil
from pydantic import BaseModel, Field, field_validator

from wyzer.models import ConfirmationMode, RiskLevel, ToolArguments
from wyzer.tools.base import ToolContext
from wyzer.tools.packs import CallableTool, SimpleToolPack

try:
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Page,
        Playwright,
        sync_playwright,
    )
except ImportError:  # pragma: no cover - represented as an unavailable pack at runtime
    Browser = BrowserContext = Page = Playwright = Any  # type: ignore[assignment,misc]
    sync_playwright = None  # type: ignore[assignment]


class NoArguments(ToolArguments):
    pass


class BrowserStartArguments(ToolArguments):
    browser: Literal["auto", "edge", "chrome"] = Field(
        default="chrome",
        description="Which installed Chromium browser to launch. Auto prefers Chrome.",
    )
    initial_url: str = Field(
        default="about:blank",
        max_length=2048,
        description="Optional initial http, https, or about URL.",
    )

    @field_validator("initial_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_navigation_url(value)


class OpenUrlArguments(ToolArguments):
    url: str = Field(max_length=2048, description="Exact http, https, or about URL to open.")
    new_tab: bool = Field(default=False, description="Open the URL in a new tab.")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_navigation_url(value)


class SearchWebArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=500, description="The user's web search query.")
    engine: Literal["bing", "google", "duckduckgo"] = "google"
    new_tab: bool = False


class InspectPageArguments(ToolArguments):
    max_elements: int = Field(default=40, ge=1, le=100)
    max_text_characters: int = Field(default=5000, ge=500, le=20_000)


class ElementRefArguments(ToolArguments):
    ref: str = Field(
        pattern=r"^e[1-9][0-9]*$",
        description="Element reference returned by browser_inspect_page, such as e3.",
    )


class TypeTextArguments(ElementRefArguments):
    text: str = Field(max_length=20_000, description="Exact text to enter into the target field.")
    clear_first: bool = True
    submit: bool = Field(default=False, description="Press Enter after typing.")


class PressKeyArguments(ToolArguments):
    key: str = Field(
        min_length=1,
        max_length=50,
        description="Playwright key name such as Enter, Escape, Tab, ArrowDown, or Control+L.",
    )


class ScrollArguments(ToolArguments):
    direction: Literal["up", "down", "top", "bottom"] = "down"
    amount: int = Field(
        default=700, ge=100, le=5000, description="Pixels for up or down scrolling."
    )


class HistoryArguments(ToolArguments):
    action: Literal["back", "forward", "reload"]


class SwitchTabArguments(ToolArguments):
    index: int = Field(ge=1, description="One-based tab index from browser_list_tabs.")


class CloseTabArguments(ToolArguments):
    index: int | None = Field(
        default=None,
        ge=1,
        description="One-based tab index. Omit to close the active tab.",
    )


class BrowserStatusResult(BaseModel):
    running: bool
    endpoint: str
    browser_name: str | None = None
    tabs: int = 0
    active_url: str | None = None
    message: str


class BrowserActionResult(BaseModel):
    success: bool
    title: str | None = None
    url: str | None = None
    message: str


class BrowserElement(BaseModel):
    ref: str
    role: str
    name: str
    tag: str
    value: str | None = None
    disabled: bool = False


class PageInspectionResult(BaseModel):
    title: str
    url: str
    text: str
    elements: list[BrowserElement]
    truncated: bool
    message: str


class BrowserTab(BaseModel):
    index: int
    active: bool
    title: str
    url: str


class TabListResult(BaseModel):
    tabs: list[BrowserTab]
    message: str


_DEFAULT_PORT = 9222
_REF_ATTRIBUTE = "data-wyzer-ref"


def _port() -> int:
    raw = os.getenv("WYZER_BROWSER_CDP_PORT", str(_DEFAULT_PORT)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("WYZER_BROWSER_CDP_PORT must be an integer.") from exc
    if not 1024 <= value <= 65535:
        raise RuntimeError("WYZER_BROWSER_CDP_PORT must be between 1024 and 65535.")
    return value


def _endpoint() -> str:
    return f"http://127.0.0.1:{_port()}"


def _json_version(timeout: float = 0.35) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{_endpoint()}/json/version", timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
            return decoded if isinstance(decoded, dict) else None
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _require_playwright() -> None:
    if sync_playwright is None:
        raise RuntimeError(
            "Built-in browser tools require Playwright. Reinstall Wyzer with its normal dependencies."
        )


def _validate_navigation_url(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https", "about"}:
        raise ValueError("Only http, https, and about URLs are supported.")
    if parsed.scheme in {"http", "https"} and not parsed.netloc:
        raise ValueError("The URL must include a hostname.")
    return value


def _browser_candidates(requested: str) -> list[tuple[str, Path]]:
    local = Path(os.getenv("LOCALAPPDATA", ""))
    program_files = Path(os.getenv("PROGRAMFILES", r"C:\Program Files"))
    program_files_x86 = Path(os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    candidates = {
        "edge": [
            program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
            program_files / "Microsoft/Edge/Application/msedge.exe",
            local / "Microsoft/Edge/Application/msedge.exe",
        ],
        "chrome": [
            program_files / "Google/Chrome/Application/chrome.exe",
            program_files_x86 / "Google/Chrome/Application/chrome.exe",
            local / "Google/Chrome/Application/chrome.exe",
        ],
    }
    order = ["chrome", "edge"] if requested == "auto" else [requested]
    found: list[tuple[str, Path]] = []
    for name in order:
        for path in candidates[name]:
            if path.is_file():
                found.append((name, path))
                break
        command = shutil.which("msedge" if name == "edge" else "chrome")
        if command and not any(item[0] == name for item in found):
            found.append((name, Path(command)))
    return found


def _profile_directory() -> Path:
    base = Path(os.getenv("LOCALAPPDATA", str(Path.home())))
    return Path(os.getenv("WYZER_BROWSER_PROFILE", str(base / "Wyzer/browser-profile")))


def _active_target_path() -> Path:
    return _profile_directory() / ".wyzer-active-target"


def _page_target_id(page: Page) -> str | None:
    session = None
    try:
        session = page.context.new_cdp_session(page)
        info = session.send("Target.getTargetInfo")
        target_id = info.get("targetInfo", {}).get("targetId")
        return str(target_id) if target_id else None
    except Exception:
        return None
    finally:
        if session is not None:
            with suppress(Exception):
                session.detach()


def _remember_active_page(page: Page) -> None:
    target_id = _page_target_id(page)
    if target_id is None:
        return
    path = _active_target_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(target_id, encoding="ascii")


def _start_browser_process(browser: str, initial_url: str) -> tuple[str, Path]:
    if os.name != "nt":
        raise RuntimeError("The managed browser launcher currently supports Windows only.")
    candidates = _browser_candidates(browser)
    if not candidates:
        raise RuntimeError("Microsoft Edge or Google Chrome was not found.")
    browser_name, executable = candidates[0]
    profile = _profile_directory()
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        str(executable),
        f"--remote-debugging-port={_port()}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        initial_url,
    ]
    subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _json_version(timeout=0.5):
            return browser_name, executable
        time.sleep(0.15)
    raise RuntimeError("The browser started but its control endpoint did not become ready.")


@contextmanager
def _connection(*, auto_start: bool = True) -> Any:
    _require_playwright()
    if not _json_version():
        if not auto_start:
            raise RuntimeError("The managed browser is not running.")
        _start_browser_process("chrome", "about:blank")
    playwright = sync_playwright().start()
    browser: Browser | None = None
    try:
        browser = playwright.chromium.connect_over_cdp(_endpoint(), timeout=5000)
        yield playwright, browser
    finally:
        # Stopping Playwright disconnects this worker from CDP. Do not call
        # browser.close() here: that would shut down the user's managed browser.
        playwright.stop()


def _context_and_pages(browser: Browser) -> tuple[BrowserContext, list[Page]]:
    contexts = browser.contexts
    if not contexts:
        raise RuntimeError("The browser has no accessible context.")
    context = contexts[0]
    pages = context.pages
    if not pages:
        pages = [context.new_page()]
    return context, pages


def _active_page(browser: Browser) -> Page:
    _context, pages = _context_and_pages(browser)
    try:
        remembered = _active_target_path().read_text(encoding="ascii").strip()
    except OSError:
        remembered = ""
    if remembered:
        for page in pages:
            if _page_target_id(page) == remembered:
                return page
    for page in reversed(pages):
        try:
            if page.evaluate("document.hasFocus()"):
                _remember_active_page(page)
                return page
        except Exception:
            continue
    page = pages[-1]
    _remember_active_page(page)
    return page


def _page_result(page: Page, message: str) -> BrowserActionResult:
    return BrowserActionResult(success=True, title=page.title(), url=page.url, message=message)


def _start(arguments: BrowserStartArguments, context: ToolContext) -> BrowserStatusResult:
    del context
    version = _json_version()
    browser_name: str | None = None
    if version is None:
        browser_name, _executable = _start_browser_process(arguments.browser, arguments.initial_url)
        version = _json_version(timeout=1.0)
    with _connection(auto_start=False) as (_playwright, browser):
        _context, pages = _context_and_pages(browser)
        page = _active_page(browser)
        _remember_active_page(page)
        return BrowserStatusResult(
            running=True,
            endpoint=_endpoint(),
            browser_name=browser_name or str((version or {}).get("Browser", "Chromium")),
            tabs=len(pages),
            active_url=page.url,
            message="The managed browser is ready.",
        )


def _managed_browser_processes() -> list[psutil.Process]:
    """Return only Chrome/Edge processes launched with Wyzer's profile and CDP port."""

    profile = str(_profile_directory().resolve()).casefold()
    port_argument = f"--remote-debugging-port={_port()}".casefold()
    matches: list[psutil.Process] = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            command = " ".join(str(part) for part in (process.info.get("cmdline") or []))
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        normalized = command.casefold()
        if port_argument in normalized and profile in normalized:
            matches.append(process)
    return matches


def _terminate_managed_browser_processes() -> None:
    roots = _managed_browser_processes()
    targets: dict[int, psutil.Process] = {}
    for root in roots:
        targets[root.pid] = root
        try:
            for child in root.children(recursive=True):
                targets[child.pid] = child
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            pass

    ordered = list(targets.values())
    for process in ordered:
        try:
            process.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    _gone, alive = psutil.wait_procs(ordered, timeout=2.5) if ordered else ([], [])
    for process in alive:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    if alive:
        psutil.wait_procs(alive, timeout=1.5)


def _stop(arguments: NoArguments, context: ToolContext) -> BrowserStatusResult:
    del arguments, context
    version = _json_version()
    if version is None:
        return BrowserStatusResult(
            running=False,
            endpoint=_endpoint(),
            message="The managed browser is already closed.",
        )

    # Ask Chromium to shut down cleanly first so its persistent profile is flushed.
    try:
        with _connection(auto_start=False) as (_playwright, browser):
            browser.close()
    except Exception:
        # CDP commonly disconnects while Chromium is closing.
        pass

    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if _json_version(timeout=0.2) is None:
            return BrowserStatusResult(
                running=False,
                endpoint=_endpoint(),
                browser_name=str(version.get("Browser", "Chromium")),
                message="The managed browser was closed.",
            )
        time.sleep(0.1)

    # Some Chromium builds only disconnect a CDP client. Fall back to terminating
    # only the process tree that has Wyzer's profile path and debugging port.
    _terminate_managed_browser_processes()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if _json_version(timeout=0.2) is None:
            return BrowserStatusResult(
                running=False,
                endpoint=_endpoint(),
                browser_name=str(version.get("Browser", "Chromium")),
                message="The managed browser was closed.",
            )
        time.sleep(0.1)
    raise RuntimeError("Wyzer could not confirm that the managed browser stopped.")


def _status(arguments: NoArguments, context: ToolContext) -> BrowserStatusResult:
    del arguments, context
    version = _json_version()
    if version is None:
        return BrowserStatusResult(
            running=False,
            endpoint=_endpoint(),
            message="The managed browser is not running.",
        )
    try:
        with _connection(auto_start=False) as (_playwright, browser):
            _context, pages = _context_and_pages(browser)
            page = _active_page(browser)
            return BrowserStatusResult(
                running=True,
                endpoint=_endpoint(),
                browser_name=str(version.get("Browser", "Chromium")),
                tabs=len(pages),
                active_url=page.url,
                message="The managed browser is running.",
            )
    except Exception as exc:
        return BrowserStatusResult(
            running=True,
            endpoint=_endpoint(),
            browser_name=str(version.get("Browser", "Chromium")),
            message=f"The endpoint is running, but Wyzer could not connect: {exc}",
        )


def _open_url(arguments: OpenUrlArguments, context: ToolContext) -> BrowserActionResult:
    del context
    with _connection() as (_playwright, browser):
        browser_context, _pages = _context_and_pages(browser)
        page = browser_context.new_page() if arguments.new_tab else _active_page(browser)
        page.goto(arguments.url, wait_until="domcontentloaded", timeout=20_000)
        page.bring_to_front()
        _remember_active_page(page)
        return _page_result(page, "The page was opened.")


def _search_web(arguments: SearchWebArguments, context: ToolContext) -> BrowserActionResult:
    del context
    encoded = quote_plus(arguments.query)
    urls = {
        "bing": f"https://www.bing.com/search?q={encoded}",
        "google": f"https://www.google.com/search?q={encoded}",
        "duckduckgo": f"https://duckduckgo.com/?q={encoded}",
    }
    with _connection() as (_playwright, browser):
        browser_context, _pages = _context_and_pages(browser)
        page = browser_context.new_page() if arguments.new_tab else _active_page(browser)
        page.goto(urls[arguments.engine], wait_until="domcontentloaded", timeout=20_000)
        page.bring_to_front()
        _remember_active_page(page)
        return _page_result(page, f"Searched {arguments.engine} for the requested query.")


_INSPECT_SCRIPT = r"""
({maxElements, maxText, attr}) => {
  document.querySelectorAll(`[${attr}]`).forEach((node) => node.removeAttribute(attr));
  const selectors = [
    'a[href]', 'button', 'input:not([type="hidden"])', 'textarea', 'select',
    '[role="button"]', '[role="link"]', '[role="textbox"]', '[contenteditable="true"]'
  ];
  const all = Array.from(document.querySelectorAll(selectors.join(',')));
  const visible = all.filter((el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  }).slice(0, maxElements);
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const roleFor = (el) => el.getAttribute('role') || ({
    A: 'link', BUTTON: 'button', INPUT: 'textbox', TEXTAREA: 'textbox', SELECT: 'combobox'
  }[el.tagName] || el.tagName.toLowerCase());
  const nameFor = (el) => clean(
    el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') ||
    (el.labels && el.labels.length ? Array.from(el.labels).map((x) => x.innerText).join(' ') : '') ||
    el.innerText || el.value || el.getAttribute('alt')
  ).slice(0, 240);
  const elements = visible.map((el, index) => {
    const ref = `e${index + 1}`;
    el.setAttribute(attr, ref);
    return {
      ref,
      role: roleFor(el),
      name: nameFor(el),
      tag: el.tagName.toLowerCase(),
      value: ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) ? clean(el.value).slice(0, 300) : null,
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true')
    };
  });
  const rawText = clean(document.body ? document.body.innerText : '');
  return {
    text: rawText.slice(0, maxText),
    truncated: rawText.length > maxText || all.length > maxElements,
    elements
  };
}
"""


def _inspect(arguments: InspectPageArguments, context: ToolContext) -> PageInspectionResult:
    del context
    with _connection() as (_playwright, browser):
        page = _active_page(browser)
        data = page.evaluate(
            _INSPECT_SCRIPT,
            {
                "maxElements": arguments.max_elements,
                "maxText": arguments.max_text_characters,
                "attr": _REF_ATTRIBUTE,
            },
        )
        elements = [BrowserElement.model_validate(item) for item in data["elements"]]
        return PageInspectionResult(
            title=page.title(),
            url=page.url,
            text=data["text"],
            elements=elements,
            truncated=bool(data["truncated"]),
            message=(
                "Use the returned element refs with browser_click or browser_type_text. "
                "Inspect again after navigation or a major page update because refs may change."
            ),
        )


def _ref_locator(page: Page, ref: str) -> Any:
    locator = page.locator(f'[{_REF_ATTRIBUTE}="{ref}"]')
    if locator.count() != 1:
        raise RuntimeError(
            f"Element {ref} is no longer available. Inspect the page again to get current refs."
        )
    return locator


def _click(arguments: ElementRefArguments, context: ToolContext) -> BrowserActionResult:
    del context
    with _connection() as (_playwright, browser):
        page = _active_page(browser)
        _ref_locator(page, arguments.ref).click(timeout=5000)
        page.wait_for_timeout(250)
        return _page_result(page, f"Clicked {arguments.ref}.")


def _type_text(arguments: TypeTextArguments, context: ToolContext) -> BrowserActionResult:
    del context
    with _connection() as (_playwright, browser):
        page = _active_page(browser)
        locator = _ref_locator(page, arguments.ref)
        if arguments.clear_first:
            locator.fill(arguments.text, timeout=5000)
        else:
            locator.press_sequentially(arguments.text, delay=10, timeout=10_000)
        if arguments.submit:
            locator.press("Enter")
        return _page_result(page, f"Entered text into {arguments.ref}.")


def _press_key(arguments: PressKeyArguments, context: ToolContext) -> BrowserActionResult:
    del context
    with _connection() as (_playwright, browser):
        page = _active_page(browser)
        page.keyboard.press(arguments.key)
        return _page_result(page, f"Pressed {arguments.key}.")


def _scroll(arguments: ScrollArguments, context: ToolContext) -> BrowserActionResult:
    del context
    with _connection() as (_playwright, browser):
        page = _active_page(browser)
        if arguments.direction == "top":
            page.evaluate("window.scrollTo(0, 0)")
        elif arguments.direction == "bottom":
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        else:
            delta = arguments.amount if arguments.direction == "down" else -arguments.amount
            page.mouse.wheel(0, delta)
        page.wait_for_timeout(150)
        return _page_result(page, f"Scrolled {arguments.direction}.")


def _history(arguments: HistoryArguments, context: ToolContext) -> BrowserActionResult:
    del context
    with _connection() as (_playwright, browser):
        page = _active_page(browser)
        if arguments.action in {"back", "forward"}:
            session = page.context.new_cdp_session(page)
            try:
                history = session.send("Page.getNavigationHistory")
                entries = history.get("entries", [])
                current = int(history.get("currentIndex", 0))
                target_index = current - 1 if arguments.action == "back" else current + 1
                if target_index < 0 or target_index >= len(entries):
                    raise RuntimeError(f"There is no page to navigate {arguments.action} to.")
                target = entries[target_index]
                session.send("Page.navigateToHistoryEntry", {"entryId": target["id"]})
                target_url = str(target.get("url") or "")
            finally:
                with suppress(Exception):
                    session.detach()
            _remember_active_page(page)
            return BrowserActionResult(
                success=True,
                title=None,
                url=target_url,
                message=f"Browser action completed: {arguments.action}.",
            )
        page.reload(wait_until="domcontentloaded", timeout=15_000)
        _remember_active_page(page)
        return _page_result(page, f"Browser action completed: {arguments.action}.")


def _list_tabs(arguments: NoArguments, context: ToolContext) -> TabListResult:
    del arguments, context
    with _connection() as (_playwright, browser):
        _browser_context, pages = _context_and_pages(browser)
        active = _active_page(browser)
        tabs = [
            BrowserTab(index=index, active=page == active, title=page.title(), url=page.url)
            for index, page in enumerate(pages, start=1)
        ]
        return TabListResult(tabs=tabs, message="Tabs are numbered from 1.")


def _switch_tab(arguments: SwitchTabArguments, context: ToolContext) -> BrowserActionResult:
    del context
    with _connection() as (_playwright, browser):
        _browser_context, pages = _context_and_pages(browser)
        if arguments.index > len(pages):
            raise RuntimeError(
                f"Tab {arguments.index} does not exist. There are {len(pages)} tabs."
            )
        page = pages[arguments.index - 1]
        page.bring_to_front()
        _remember_active_page(page)
        return _page_result(page, f"Switched to tab {arguments.index}.")


def _close_tab(arguments: CloseTabArguments, context: ToolContext) -> BrowserActionResult:
    del context
    with _connection() as (_playwright, browser):
        browser_context, pages = _context_and_pages(browser)
        target = _active_page(browser) if arguments.index is None else None
        if arguments.index is not None:
            if arguments.index > len(pages):
                raise RuntimeError(
                    f"Tab {arguments.index} does not exist. There are {len(pages)} tabs."
                )
            target = pages[arguments.index - 1]
        assert target is not None
        target.close()
        remaining = browser_context.pages
        if not remaining:
            page = browser_context.new_page()
        else:
            page = remaining[-1]
            page.bring_to_front()
        _remember_active_page(page)
        return _page_result(page, "The tab was closed.")


def _tool(
    *,
    name: str,
    description: str,
    arguments_type: type[ToolArguments],
    result_type: type[BaseModel],
    handler: Callable[..., BaseModel],
    risk_level: RiskLevel,
    read_only: bool,
    timeout: float = 25.0,
    confirmation: ConfirmationMode = ConfirmationMode.NEVER,
    llm_visible: bool = True,
) -> CallableTool[Any, Any]:
    available = sync_playwright is not None and os.name == "nt"
    reason = None if available else "Requires Windows and the Playwright Python package."
    return CallableTool(
        name=name,
        description=description,
        arguments_type=arguments_type,
        result_type=result_type,
        handler=handler,
        risk_level=risk_level,
        read_only=read_only,
        confirmation=confirmation,
        default_timeout_seconds=timeout,
        available=available,
        unavailable_reason=reason,
        llm_visible=llm_visible,
    )


def create_browser_pack() -> SimpleToolPack:
    return SimpleToolPack(
        name="browser",
        tool_factories=(
            lambda: _tool(
                name="browser_start",
                description=(
                    "Start or connect to Wyzer's managed Google Chrome browser by default. "
                    "Use Edge only when the user explicitly asks for Edge. Other browser tools "
                    "start managed Chrome automatically, so do not call this before every action."
                ),
                arguments_type=BrowserStartArguments,
                result_type=BrowserStatusResult,
                handler=_start,
                risk_level=RiskLevel.LOW,
                read_only=False,
                llm_visible=False,
            ),
            lambda: _tool(
                name="browser_stop",
                description=(
                    "Close Wyzer's managed automation browser and all tabs. Use for close Chrome, "
                    "close browser, or stop browser automation. Do not use browser_close_tab for this."
                ),
                arguments_type=NoArguments,
                result_type=BrowserStatusResult,
                handler=_stop,
                risk_level=RiskLevel.MEDIUM,
                read_only=False,
            ),
            lambda: _tool(
                name="browser_status",
                description="Check whether Wyzer's managed browser is running and report its active URL.",
                arguments_type=NoArguments,
                result_type=BrowserStatusResult,
                handler=_status,
                risk_level=RiskLevel.LOW,
                read_only=True,
                llm_visible=False,
            ),
            lambda: _tool(
                name="browser_open_url",
                description=(
                    "Open an exact web URL in managed Chrome, starting it automatically if needed, "
                    "optionally in a new tab."
                ),
                arguments_type=OpenUrlArguments,
                result_type=BrowserActionResult,
                handler=_open_url,
                risk_level=RiskLevel.MEDIUM,
                read_only=False,
            ),
            lambda: _tool(
                name="browser_search_web",
                description=(
                    "Search the web directly in managed Chrome, starting it automatically if needed. "
                    "Use this instead of opening Chrome as a desktop application or typing into its address bar."
                ),
                arguments_type=SearchWebArguments,
                result_type=BrowserActionResult,
                handler=_search_web,
                risk_level=RiskLevel.LOW,
                read_only=False,
            ),
            lambda: _tool(
                name="browser_inspect_page",
                description=(
                    "Read the active page's visible text and interactive elements. Returns refs such as e1 "
                    "for browser_click and browser_type_text. Always inspect before interacting with a page."
                ),
                arguments_type=InspectPageArguments,
                result_type=PageInspectionResult,
                handler=_inspect,
                risk_level=RiskLevel.LOW,
                read_only=True,
            ),
            lambda: _tool(
                name="browser_click",
                description=(
                    "Click one element ref from the latest browser_inspect_page result. Inspect again after "
                    "navigation or major page changes."
                ),
                arguments_type=ElementRefArguments,
                result_type=BrowserActionResult,
                handler=_click,
                risk_level=RiskLevel.MEDIUM,
                read_only=False,
                confirmation=ConfirmationMode.CONDITIONAL,
            ),
            lambda: _tool(
                name="browser_type_text",
                description=(
                    "Enter exact text into a textbox ref from browser_inspect_page, optionally clearing it "
                    "and pressing Enter. Do not use for passwords unless the user explicitly provided one."
                ),
                arguments_type=TypeTextArguments,
                result_type=BrowserActionResult,
                handler=_type_text,
                risk_level=RiskLevel.MEDIUM,
                read_only=False,
                confirmation=ConfirmationMode.CONDITIONAL,
            ),
            lambda: _tool(
                name="browser_press_key",
                description="Press a browser key or shortcut such as Enter, Escape, Tab, or Control+L.",
                arguments_type=PressKeyArguments,
                result_type=BrowserActionResult,
                handler=_press_key,
                risk_level=RiskLevel.MEDIUM,
                read_only=False,
            ),
            lambda: _tool(
                name="browser_scroll",
                description="Scroll the active page up, down, to the top, or to the bottom.",
                arguments_type=ScrollArguments,
                result_type=BrowserActionResult,
                handler=_scroll,
                risk_level=RiskLevel.LOW,
                read_only=False,
            ),
            lambda: _tool(
                name="browser_history",
                description="Go back, go forward, or reload the active browser tab.",
                arguments_type=HistoryArguments,
                result_type=BrowserActionResult,
                handler=_history,
                risk_level=RiskLevel.LOW,
                read_only=False,
            ),
            lambda: _tool(
                name="browser_list_tabs",
                description="List managed-browser tabs with one-based indexes, titles, URLs, and active state.",
                arguments_type=NoArguments,
                result_type=TabListResult,
                handler=_list_tabs,
                risk_level=RiskLevel.LOW,
                read_only=True,
            ),
            lambda: _tool(
                name="browser_switch_tab",
                description="Switch to a tab index returned by browser_list_tabs.",
                arguments_type=SwitchTabArguments,
                result_type=BrowserActionResult,
                handler=_switch_tab,
                risk_level=RiskLevel.LOW,
                read_only=False,
            ),
            lambda: _tool(
                name="browser_close_tab",
                description=(
                    "Close one tab only: the active tab or a tab index returned by browser_list_tabs. "
                    "Never use this to close Chrome or the whole managed browser; use browser_stop instead."
                ),
                arguments_type=CloseTabArguments,
                result_type=BrowserActionResult,
                handler=_close_tab,
                risk_level=RiskLevel.MEDIUM,
                read_only=False,
            ),
        ),
    )


# Backward-friendly module-level alias for pack-oriented tests/extensions.
create_pack = create_browser_pack
