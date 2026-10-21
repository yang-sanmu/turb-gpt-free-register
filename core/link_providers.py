# -*- coding: utf-8 -*-
"""独立提链服务。

上游 extract_link_service 负责队列、并发与状态；这里只实现具体服务的请求，
结果映射为上游已有字段（long_url / copy_paste / image_url_png ...），账号列表无需改动。
新增服务：实现 run(account_id, access_token) -> dict，并登记到 PROVIDERS。
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Callable

from curl_cffi import requests as curl_requests

from config import link_providers as cfg
from config.proxy import normalize_proxy_url
from core import db


@dataclass(frozen=True)
class LinkProvider:
    name: str
    link_type: str
    run: Callable[..., dict]


def _iter_sse(resp):
    """逐个产出 (event, data)；注释行（: ping）产出 (None, None) 供调用方检查超时。"""
    event, data_lines = "message", []
    for raw in resp.iter_lines():
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")).rstrip("\r")
        if line.startswith(":"):
            yield None, None
        elif line.startswith("event:"):
            event = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "" and data_lines:
            text = "\n".join(data_lines)
            try:
                data = json.loads(text)
            except ValueError:
                data = {"raw": text}
            yield event, data
            event, data_lines = "message", []


def _nerver_result(data: dict) -> dict:
    return {
        **data,
        "long_url": data.get("upi_redirect_url") or data.get("upi_hosted_url"),
        "copy_paste": data.get("upi_qr_data"),
        "image_url_png": data.get("upi_qr_image_url"),
        "payment_method": "upi",
        "payment_link_type": data.get("next_action_type"),
    }


def run_nerver_upi(*, account_id: int, access_token: str) -> dict:
    """调用 Nerver /api/v1/upi/stream。请求直连；代理仅作为参数交给服务端。"""
    pool = [p for p in (normalize_proxy_url(x) for x in cfg.NERVER_UPI_PROXY_POOL) if p]
    if not pool:
        raise ValueError("NERVER_UPI_PROXY_POOL 为空；Nerver UPI 免鉴权模式需要提供代理")
    url = f"{str(cfg.NERVER_UPI_API_BASE).strip().rstrip('/')}/api/v1/upi/stream"
    deadline = time.monotonic() + max(30, int(cfg.NERVER_UPI_TIMEOUT))
    s = curl_requests.Session(trust_env=False)
    try:
        resp = s.post(
            url,
            json={"token": access_token, "proxy": random.choice(pool)},
            headers={"Accept": "text/event-stream"},
            timeout=(15, max(30, int(cfg.NERVER_UPI_TIMEOUT))),
            stream=True,
        )
        if resp.status_code != 200:
            body = b"".join(x if isinstance(x, bytes) else str(x).encode() for x in resp.iter_lines())
            raise RuntimeError(f"Nerver UPI HTTP {resp.status_code}: {body.decode('utf-8', 'replace')[:300]}")
        for event, data in _iter_sse(resp):
            if time.monotonic() > deadline:
                raise TimeoutError(f"Nerver UPI 超过 {cfg.NERVER_UPI_TIMEOUT} 秒未返回结果")
            if event == "progress" and isinstance(data, dict) and data.get("type") == "step":
                db.update_account_extract(account_id, {
                    "ok": False, "status": "running", "link_type": "upi",
                    "message": f"UPI: {data.get('step')}",
                })
            elif event == "done":
                data = data if isinstance(data, dict) else {}
                if not data.get("ok"):
                    detail = "；".join(str(x) for x in (data.get("message"), data.get("error")) if x)
                    raise RuntimeError(f"Nerver UPI 失败 {data.get('reason') or ''} {detail}".strip())
                result = _nerver_result(data)
                if not (result["long_url"] or result["copy_paste"] or result["image_url_png"]):
                    raise RuntimeError(f"Nerver UPI 返回成功但没有链接/二维码 request_id={data.get('request_id')}")
                return {
                    "ok": True, "status": "success", "link_type": "upi",
                    "job_id": data.get("request_id"), "result": result,
                }
        raise RuntimeError("Nerver UPI 事件流结束但未返回 done")
    finally:
        s.close()


PROVIDERS = {
    "nerver_upi": LinkProvider("nerver_upi", "upi", run_nerver_upi),
}


def active() -> LinkProvider | None:
    """返回当前配置的独立提链服务；cdk/空值表示走上游 CDK 提链。"""
    name = str(cfg.LINK_PROVIDER or "").strip().lower()
    if name in ("", "cdk"):
        return None
    if name not in PROVIDERS:
        raise ValueError(f"LINK_PROVIDER 无效：{name}，可选 cdk / {' / '.join(PROVIDERS)}")
    return PROVIDERS[name]
