"""Local stdio bridge to GptAccount's owned, ephemeral Chrome process."""
from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from config import gptaccount as cfg
from core.cloakbrowser_driver import CloakElement, CloakOpenResult, CloakSeleniumDriver

logger = logging.getLogger(__name__)


class GptAccountBridge:
    def __init__(self):
        root = Path(cfg.GPTACCOUNT_PROJECT_DIR).expanduser() if cfg.GPTACCOUNT_PROJECT_DIR else Path(__file__).resolve().parents[2] / "GptAccount"
        script = root / "scripts" / "registration-browser.mjs"
        if not script.is_file():
            raise RuntimeError(f"找不到 GptAccount 浏览器桥接脚本：{script}")
        self._messages = queue.Queue()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self.cleanup_result = None
        self.runtime_dir = None
        self.process = subprocess.Popen(
            [cfg.GPTACCOUNT_NODE, str(script.resolve())], cwd=str(root.resolve()),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._threads = [threading.Thread(target=target, daemon=True)
                         for target in (self._read_stdout, self._drain_stderr, self._heartbeat)]
        for thread in self._threads:
            thread.start()

    def _read_stdout(self):
        try:
            with self.process.stdout:
                for line in self.process.stdout:
                    try:
                        value = json.loads(line)
                        if isinstance(value, dict) and value.get("protocol") == 1:
                            self._messages.put(value)
                    except ValueError:
                        pass
        finally:
            self._messages.put({"event": "eof"})

    def _drain_stderr(self):
        # Do not forward arbitrary browser/runtime diagnostics into account logs.
        with self.process.stderr:
            for _line in self.process.stderr:
                pass

    def _send(self, command, **fields):
        with self._write_lock:
            self.process.stdin.write(json.dumps({"protocol": 1, "command": command, **fields}) + "\n")
            self.process.stdin.flush()

    def _heartbeat(self):
        while not self._stop.wait(3):
            try:
                self._send("ping")
            except (OSError, ValueError):
                return

    def open(self, proxy=""):
        from core.registration_service import check_stop_requested
        self._send("open", proxy=proxy or "", headless=bool(cfg.GPTACCOUNT_HEADLESS),
                   locale=cfg.GPTACCOUNT_LOCALE, timezone=cfg.GPTACCOUNT_TIMEZONE)
        deadline = time.monotonic() + max(10, int(cfg.GPTACCOUNT_START_TIMEOUT))
        while time.monotonic() < deadline:
            check_stop_requested()
            try:
                message = self._messages.get(timeout=0.25)
            except queue.Empty:
                continue
            if message["event"] == "ready":
                endpoint = str(message.get("endpoint") or "")
                parsed = urlsplit(endpoint)
                if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.username or parsed.password:
                    raise RuntimeError("GptAccount 返回的 CDP 地址不是有效本机端点")
                self.runtime_dir = message.get("runtime_dir")
                return endpoint
            if message["event"] == "error":
                raise RuntimeError(f"GptAccount 启动失败：{message.get('message')}")
            if message["event"] in {"closed", "eof"}:
                if message["event"] == "closed":
                    self.cleanup_result = message
                raise RuntimeError("GptAccount 在浏览器就绪前退出")
        raise TimeoutError("等待 GptAccount Chrome 启动超时")

    def close(self):
        if self._closed:
            return self.cleanup_result
        self._closed = True
        self._stop.set()
        try:
            try:
                self._send("close")
            except (OSError, ValueError):
                pass
            try:
                with self._write_lock:
                    self.process.stdin.close()
            except (OSError, ValueError):
                pass
            deadline = time.monotonic() + 25
            while self.cleanup_result is None and time.monotonic() < deadline:
                try:
                    message = self._messages.get(timeout=0.25)
                except queue.Empty:
                    continue
                if message.get("event") == "closed":
                    self.cleanup_result = message
                elif message.get("event") == "eof":
                    break
            self.process.wait(timeout=max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        finally:
            for thread in self._threads:
                thread.join(timeout=1)
            if not (self.cleanup_result or {}).get("cleaned"):
                location = (self.cleanup_result or {}).get("runtime_dir") or self.runtime_dir or "未知"
                raise RuntimeError(f"GptAccount 临时浏览器清理未确认，目录保留供检查：{location}")
        return self.cleanup_result


class GptAccountElement(CloakElement):
    """Selenium send_keys appends at the caret.

    CloakElement.send_keys uses fill(), which replaces the value, so the shared
    flow's per-character typing left only the last character in plain Chrome.
    """

    def send_keys(self, *values: str) -> None:
        text = "".join(str(v or "") for v in values)
        keyboard = self.page.keyboard
        if "" in text or "" in text:  # Keys.CONTROL / Keys.COMMAND + "a"
            self._eval("el => el.focus()")
            keyboard.press("Control+A")
        elif "" in text:  # Keys.BACKSPACE; keep any selection from Ctrl+A
            keyboard.press("Backspace")
        else:
            self._eval("el => { el.focus(); const n = (el.value || '').length;"
                       " try { el.setSelectionRange(n, n); } catch (e) {} }")
            keyboard.type(text)


def _as_gptaccount_element(value):
    if isinstance(value, CloakElement) and not isinstance(value, GptAccountElement):
        return GptAccountElement(value.page, value.locator, value.handle)
    return value


class GptAccountDriver(CloakSeleniumDriver):
    def __init__(self, *, bridge, playwright, browser, context, page, proxy_relay):
        super().__init__(browser, context, page, proxy_relay)
        self._bridge = bridge
        self._playwright = playwright
        self._quit_done = False
        self._registration_log_prefix = "[GptAccount注册]"

    def find_elements(self, by, selector):
        return [_as_gptaccount_element(el) for el in super().find_elements(by, selector)]

    @staticmethod
    def _unwrap_js_result(page, handle):
        # json_value() turns DOM nodes inside {ok, button: el} into "ref: <Node>";
        # the shared flow then calls .click() on a string. Keep them as elements.
        nested = {}
        try:
            if handle.as_element() is None:
                for key, prop in handle.get_properties().items():
                    element = prop.as_element()
                    if element is not None:
                        nested[key] = GptAccountElement(page, handle=element)
                    else:
                        prop.dispose()
        except Exception:
            nested = {}
        result = _as_gptaccount_element(CloakSeleniumDriver._unwrap_js_result(page, handle))
        if nested and isinstance(result, dict):
            result.update(nested)
        return result

    def quit(self):
        if self._quit_done:
            return
        self._quit_done = True
        try:
            # The GptAccount broker, not Playwright, owns the process tree.
            self._bridge.close()
        finally:
            try:
                self._playwright.stop()
            finally:
                relay, self._proxy_relay = self._proxy_relay, None
                if relay is not None:
                    relay.close()


def build_gptaccount_driver(proxy=None):
    from config.proxy import pick_proxy, normalize_proxy_url
    from core.proxy_chain import open_proxy_pool_proxy
    from playwright.sync_api import sync_playwright

    target = normalize_proxy_url(pick_proxy() if proxy is None else proxy)
    relay = bridge = playwright = None
    try:
        # This also applies PROXY_POOL_UPSTREAM_PROXY to an explicitly selected proxy.
        transport, relay = open_proxy_pool_proxy(target, measure_transport=True)
        if transport and relay is None:
            raise ValueError("GptAccount 注册需使用可由本地中继转发的 HTTP/SOCKS5 代理")
        bridge = GptAccountBridge()
        endpoint = bridge.open(transport)
        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=20_000)
        if not browser.contexts:
            raise RuntimeError("GptAccount Chrome 没有默认上下文")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        driver = GptAccountDriver(bridge=bridge, playwright=playwright, browser=browser,
                                  context=context, page=page, proxy_relay=relay)
        driver.set_page_load_timeout(90)
        return driver, CloakOpenResult(profile_id="gptaccount-temporary", raw={
            "driver": "gptaccount", "proxy_pool_target": target,
            "locale": cfg.GPTACCOUNT_LOCALE, "timezone": cfg.GPTACCOUNT_TIMEZONE,
        })
    except BaseException:
        try:
            if bridge is not None:
                bridge.close()
        except Exception as cleanup_error:
            # Keep the actual launch/attach failure as the task error.
            logger.warning("[GptAccount] 启动失败后的清理未完成：%s", cleanup_error)
        finally:
            try:
                if playwright is not None:
                    playwright.stop()
            finally:
                if relay is not None:
                    relay.close()
        raise
