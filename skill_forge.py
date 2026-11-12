# -*- coding: utf-8 -*-
"""为用户新增技能生成「升级机制」（指标门 + LV1–LV8 达标要求）。

诚实边界（必须原样传达给使用者）
--------------------------------
* 运行时**没有内置大模型**，也不依赖任何 AI 助手。要启用 AI，需要部署方
  自己配置一个 OpenAI 兼容端点（PROF_AI_ENDPOINT / PROF_AI_KEY / PROF_AI_MODEL）。
* 「从网络获取信息」= 服务端用标准库抓取用户给出的公开资料 URL，把正文
  连同严格的 JSON 结构要求一起交给该端点；模型只在这些资料与用户给定的
  数值范围内决定最低线、优秀线、各级要求和文字描述。
* 模型**不能**改指标的 key / 名称 / 单位 / 合法范围，不能开启医学安全门，
  不能声称官方标准；所有返回值逐项校验，越界或字段不全一律拒绝。
* 未配置端点、抓取失败、模型失败或校验不通过：回退到本地保守模板，
  并在 provenance（来源记录）里写明 ai_used=false 与具体原因。
  绝不把本地模板冒充成「AI 联网判定」。
* 生成的是**静态规则**，落盘后按原有纯函数判定；每次练习不需要联网、
  不需要模型，结果可复现。自报数值仍然不验真（trusted=False）。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

MAX_LEVEL = 8
MAX_METRICS = 5
MAX_URLS = 3
MAX_SOURCE_BYTES = 256 * 1024
MAX_CORPUS_CHARS = 12_000
FETCH_TIMEOUT = 8
LLM_TIMEOUT = 25

# 我在 2026-11-10 通过公开检索实际核对过的资料，固化在这里作为「未配置
# AI 时」的参考线索。它只是分级结构参考，运行时不联网、不冒充官方阈值。
VERIFIED_REFERENCES: dict[str, dict] = {
    "钢琴演奏": {
        "url": "https://www.abrsm.org/en-gb/piano",
        "checked_on": "2026-11-10",
        "summary": ("ABRSM 钢琴 Practical Grades 覆盖 Initial Grade 至 Grade 8；"
                    "考试含三首曲目、音阶与琶音、视奏与听觉测试；"
                    "Grade 6–8 报名需先通过 Grade 5 乐理或实用音乐素养。"),
        "note": "仅参考其分级结构；本系统的指标阈值是自建标准，不是 ABRSM 规定值。",
    },
}


class ForgeError(ValueError):
    """生成规格失败，可直接返回给用户。"""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag) -> None:
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _is_private_host(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
        return bool(addr.is_private or addr.is_loopback or addr.is_link_local
                    or addr.is_reserved)
    except ValueError:
        pass
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            addr = ipaddress.ip_address(info[4][0])
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                return True
    except OSError:
        return True
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ForgeError("资料链接不允许重定向")


def fetch_sources(urls: list[str], *, allow_private: bool = False) -> list[dict]:
    """抓取公开资料正文。限制数量/体积/超时，默认拒绝内网地址防 SSRF。"""
    out: list[dict] = []
    opener = urllib.request.build_opener(_NoRedirect)
    for raw in list(urls or [])[:MAX_URLS]:
        url = str(raw or "").strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ForgeError(f"资料链接必须是 http/https 绝对地址：{url[:80]}")
        if not allow_private and _is_private_host(parsed.hostname):
            raise ForgeError(f"资料链接指向内网地址，已拒绝：{parsed.hostname}")
        req = urllib.request.Request(
            url, headers={"User-Agent": "guide-web/1.0 (+local skill forge)",
                          "Accept": "text/html,text/plain,application/json"})
        try:
            with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
                content_type = str(resp.headers.get("Content-Type", ""))
                raw_bytes = resp.read(MAX_SOURCE_BYTES + 1)
        except (urllib.error.URLError, OSError, ForgeError) as exc:
            raise ForgeError(f"资料抓取失败：{url[:80]}（{type(exc).__name__}）") from exc
        if len(raw_bytes) > MAX_SOURCE_BYTES:
            raise ForgeError(f"资料超过 {MAX_SOURCE_BYTES // 1024}KB：{url[:80]}")
        text = raw_bytes.decode("utf-8", errors="replace")
        if "html" in content_type.lower() or "<" in text[:200]:
            parser = _TextExtractor()
            parser.feed(text)
            text = " ".join(parser.parts)
        text = re.sub(r"\s+", " ", text).strip()
        out.append({
            "url": url,
            "content_type": content_type,
            "chars": len(text),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "excerpt": text[:400],
            "text": text[:MAX_CORPUS_CHARS],
        })
    if not out:
        raise ForgeError("没有抓到任何资料正文")
    return out


# 联网检索用的公开百科接口（不需要 API Key）。只用来找「分级结构」参考，
# 阈值仍由 AI 按用户给的合法范围折算，并由 _validate_ai_output 卡住越界值。
WIKI_ENDPOINTS = (
    ("zh", "https://zh.wikipedia.org/w/api.php"),
    ("en", "https://en.wikipedia.org/w/api.php"),
)
DISCOVER_LIMIT = 3
DISCOVER_MIN_CHARS = 120


def _http_json(url: str, *, allow_private: bool) -> dict:
    """GET 一个 JSON 接口，沿用资料抓取的 SSRF 与重定向限制。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ForgeError("检索地址必须是 http/https 绝对地址")
    if not allow_private and _is_private_host(parsed.hostname):
        raise ForgeError("检索地址指向内网，已拒绝")
    req = urllib.request.Request(
        url, headers={"User-Agent": "guide-web/1.0 (+local skill forge)",
                      "Accept": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read(MAX_SOURCE_BYTES)
    return json.loads(raw.decode("utf-8", "replace"))


def discover_sources(term: str, *, allow_private: bool = False,
                     json_get=None, limit: int = DISCOVER_LIMIT) -> list[dict]:
    """按技能名联网找资料。

    先用中文维基，再补英文维基；只取摘要正文，够长才收。任何一步失败都跳过，
    整体失败返回空列表——调用方据此回退本地模板，绝不假装成功。
    """
    query = str(term or "").strip()
    if not query:
        return []
    get = json_get or (lambda url: _http_json(url, allow_private=allow_private))
    out: list[dict] = []
    seen: set[str] = set()
    for lang, api in WIKI_ENDPOINTS:
        if len(out) >= limit:
            break
        params = urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": "2",
            "generator": "search", "gsrsearch": query, "gsrlimit": "3",
            "prop": "extracts", "exintro": "1", "explaintext": "1",
        })
        try:
            data = get(f"{api}?{params}")
        except (ForgeError, urllib.error.URLError, OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        pages = ((data.get("query") or {}).get("pages") or [])
        if isinstance(pages, dict):           # formatversion=1 的兼容分支
            pages = list(pages.values())
        for page in pages:
            if len(out) >= limit:
                break
            title = str(page.get("title") or "").strip()
            text = re.sub(r"\s+", " ", str(page.get("extract") or "")).strip()
            if not title or len(text) < DISCOVER_MIN_CHARS or title in seen:
                continue
            seen.add(title)
            out.append({
                "url": f"https://{lang}.wikipedia.org/wiki/"
                       + urllib.parse.quote(title.replace(" ", "_")),
                "content_type": "text/plain",
                "chars": len(text),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "excerpt": text[:400],
                "text": text[:MAX_CORPUS_CHARS],
                "title": title,
                "lang": lang,
            })
    return out


def _ai_config(config: dict | None) -> dict:
    cfg = dict(config or {})
    return {
        "endpoint": str(cfg.get("endpoint") or os.environ.get("PROF_AI_ENDPOINT", "")).strip(),
        "key": str(cfg.get("key") or os.environ.get("PROF_AI_KEY", "")).strip(),
        "model": str(cfg.get("model") or os.environ.get("PROF_AI_MODEL", "")).strip(),
        "allow_private": bool(cfg.get("allow_private")
                              or os.environ.get("PROF_AI_ALLOW_PRIVATE_URLS", "")
                              in ("1", "true", "yes")),
    }


def call_llm(endpoint: str, key: str, model: str,
             messages: list[dict]) -> str:
    url = endpoint.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = json.dumps({"model": model, "messages": messages,
                       "temperature": 0}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ForgeError(f"AI 端点调用失败：{type(exc).__name__}") from exc
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ForgeError("AI 返回结构不符合 chat/completions 约定") from exc
    text = str(content).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def _norm_metrics(metrics: object) -> list[dict]:
    if not isinstance(metrics, list) or not (1 <= len(metrics) <= MAX_METRICS):
        raise ForgeError(f"指标需 1–{MAX_METRICS} 项")
    out, keys = [], set()
    for item in metrics:
        if not isinstance(item, dict):
            raise ForgeError("指标必须是对象")
        key = str(item.get("key", "")).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", key):
            raise ForgeError(f"指标 key 不合法（小写字母开头的英文标识符）：{key!r}")
        if key in keys or key in ("pain_flag", "rpe", "stop_sign"):
            raise ForgeError(f"指标 key 重复或占用了医学安全门字段：{key}")
        keys.add(key)
        try:
            low, high = int(item.get("range_low")), int(item.get("range_high"))
        except (TypeError, ValueError) as exc:
            raise ForgeError(f"{key}: 合法范围必须是整数") from exc
        if not (0 <= low < high <= 1_000_000):
            raise ForgeError(f"{key}: 合法范围需满足 0 <= 下界 < 上界 <= 1000000")
        out.append({
            "key": key,
            "label": str(item.get("label") or key).strip()[:24] or key,
            "unit": str(item.get("unit") or "项").strip()[:12] or "项",
            "range_low": low, "range_high": high,
        })
    return out


def _linear_requirements(low: int, high: int) -> list[int]:
    span = high - low
    step = max(1, span // 10)
    return [min(high, low + step + round(span * i / (MAX_LEVEL - 1) * 0.9))
            for i in range(MAX_LEVEL)]


def local_spec(name: str, metrics: list[dict], *, now: int,
               reason: str, sources: list[dict] | None = None) -> dict:
    """离线保守模板：只按用户给的合法范围线性铺开，不查资料、不冒充 AI。"""
    specs = []
    for m in metrics:
        low, high = m["range_low"], m["range_high"]
        span = high - low
        min_valid = low + max(1, span // 10)
        good = min(high, min_valid + max(1, span // 3))
        reqs = _linear_requirements(low, high)
        specs.append({
            **m, "min_valid": min_valid, "good": good,
            "level_requirements": reqs,
            "basis": "draft", "source_ids": [],
            "note": "自建标准·非官方；本地保守模板生成，未经网络资料或 AI 判定",
        })
    observables = [
        f"LV{i + 1}：{specs[0]['label']}近期均值达到 "
        f"{specs[0]['level_requirements'][i]} {specs[0]['unit']}（自建标准·非官方）"
        for i in range(MAX_LEVEL)
    ]
    reference = VERIFIED_REFERENCES.get(name)
    return {
        "skill_id": name,
        "description": (f"自定义技能「{name}」。指标门与 LV1–LV8 要求由本地保守模板"
                        "按用户给定的合法范围生成，未联网、未经 AI 判定；"
                        "所有数值都是自建标准，不代表任何官方分级。"),
        "standard": "无官方对应，待定（自建标准·非官方）",
        "source_ids": [], "evidence_type": "draft",
        "official_max_level": MAX_LEVEL, "medical_gate": False,
        "metrics": specs, "level_observables": observables,
        "provenance": {
            "mode": "local", "ai_used": False, "reason": reason,
            "model": "", "sources": [
                {k: v for k, v in s.items() if k != "text"}
                for s in (sources or [])],
            "verified_reference": reference, "created_at": now,
        },
        "created_at": now,
    }


def _validate_ai_output(raw: str, metrics: list[dict], name: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ForgeError("AI 返回的不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ForgeError("AI 返回根节点必须是对象")
    by_key = {m["key"]: m for m in data.get("metrics", [])} \
        if isinstance(data.get("metrics"), list) else {}
    if set(by_key) != {m["key"] for m in metrics}:
        raise ForgeError("AI 返回了未声明或缺失的指标")
    built = []
    for base in metrics:
        item = by_key[base["key"]]
        if not isinstance(item, dict):
            raise ForgeError(f"{base['key']}: AI 返回的指标不是对象")
        low, high = base["range_low"], base["range_high"]
        min_valid = item.get("min_valid")
        good = item.get("good")
        reqs = item.get("level_requirements")
        for label, value in (("min_valid", min_valid), ("good", good)):
            if not isinstance(value, int) or isinstance(value, bool) \
                    or not (low <= value <= high):
                raise ForgeError(f"{base['key']}: {label} 必须是 {low}–{high} 内整数")
        if not (min_valid <= good):
            raise ForgeError(f"{base['key']}: good 不能小于 min_valid")
        if not isinstance(reqs, list) or len(reqs) != MAX_LEVEL:
            raise ForgeError(f"{base['key']}: 必须给 {MAX_LEVEL} 个等级要求")
        if any(not isinstance(v, int) or isinstance(v, bool)
               or not (low <= v <= high) for v in reqs):
            raise ForgeError(f"{base['key']}: 等级要求必须是范围内整数")
        if any(b < a for a, b in zip(reqs, reqs[1:])):
            raise ForgeError(f"{base['key']}: 等级要求必须单调不减")
        built.append({
            **base, "min_valid": min_valid, "good": good,
            "level_requirements": list(reqs),
            "basis": "draft", "source_ids": [],
            "note": str(item.get("note") or "")[:120],
        })
    observables = data.get("level_observables")
    if not isinstance(observables, list) or len(observables) != MAX_LEVEL \
            or any(not isinstance(x, str) or not x.strip() for x in observables):
        raise ForgeError(f"必须给 {MAX_LEVEL} 条非空等级描述")
    description = str(data.get("description") or "").strip()
    if not description or len(description) > 300:
        raise ForgeError("技能说明需 1–300 字")
    return {
        "built": built,
        "observables": [x.strip()[:120] for x in observables],
        "description": description,
    }


def ai_spec(name: str, metrics: list[dict], sources: list[dict], *,
            now: int, cfg: dict) -> dict:
    corpus = "\n\n".join(
        f"[资料 {i + 1}] {s['url']}\n{s['text']}" for i, s in enumerate(sources))
    schema = {
        "metrics": [
            {"key": m["key"], "min_valid": "整数，>= range_low",
             "good": "整数，>= min_valid",
             "level_requirements": "8 个单调不减的整数，均在合法范围内",
             "note": "简短依据"}
            for m in metrics],
        "level_observables": ["LV1..LV8 各一条可观测描述"],
        "description": "1–300 字技能说明",
    }
    prompt = (
        "你在为一款本地熟练度系统生成【自建标准·非官方】的技能升级规则。"
        "只依据下面提供的资料与用户给定的指标范围作答，不要虚构官方标准，"
        "不要输出资料之外的数字，不要输出解释文字或 Markdown，只输出一个 JSON 对象。\n"
        f"技能名：{name}\n"
        f"用户给定的指标（key / 名称 / 单位 / 合法范围）："
        f"{json.dumps(metrics, ensure_ascii=False)}\n"
        f"必须严格返回这个结构：{json.dumps(schema, ensure_ascii=False)}\n"
        f"资料：\n{corpus[:MAX_CORPUS_CHARS]}"
    )
    content = call_llm(cfg["endpoint"], cfg["key"], cfg["model"], [
        {"role": "system", "content": "只输出 JSON，不输出任何额外文字。"},
        {"role": "user", "content": prompt},
    ])
    parsed = _validate_ai_output(content, metrics, name)
    return {
        "skill_id": name,
        "description": parsed["description"],
        "standard": "自建标准·非官方（AI 依据所提供资料生成，非官方分级）",
        "source_ids": [], "evidence_type": "draft",
        "official_max_level": MAX_LEVEL, "medical_gate": False,
        "metrics": parsed["built"],
        "level_observables": parsed["observables"],
        "provenance": {
            "mode": "ai_web", "ai_used": True, "reason": "AI 依据抓取到的资料生成",
            "model": cfg["model"],
            "sources": [{k: v for k, v in s.items() if k != "text"}
                        for s in sources],
            "verified_reference": VERIFIED_REFERENCES.get(name),
            "created_at": now,
        },
        "created_at": now,
    }


def ai_status() -> dict:
    """给界面看的 AI 配置状态；绝不回传密钥。"""
    cfg = _ai_config(None)
    configured = bool(cfg["endpoint"] and cfg["key"] and cfg["model"])
    host = urlparse(cfg["endpoint"]).hostname if configured else ""
    return {"configured": configured,
            "model": cfg["model"] if configured else "",
            "endpoint_host": host or "",
            "allow_private": cfg["allow_private"],
            "discovery": "wikipedia-zh/en",
            "needs_urls": False}


def forge_skill_spec(*, name: object, metrics: object,
                     source_urls: object = None, now: int | None = None,
                     config: dict | None = None) -> dict:
    """生成一份可直接落盘的自定义技能规格。失败一律回退本地模板并写明原因。"""
    t = int(now if now is not None else time.time())
    skill_id = str(name or "").strip()
    if not (2 <= len(skill_id) <= 24):
        raise ForgeError("技能名需 2–24 字")
    if any(c in skill_id for c in "/\\<>\"'{}[]"):
        raise ForgeError("技能名不能包含路径或标记字符")
    base_metrics = _norm_metrics(metrics)
    cfg = _ai_config(config)
    urls = [str(u) for u in (source_urls or []) if str(u).strip()][:MAX_URLS]

    if not cfg["endpoint"] or not cfg["key"] or not cfg["model"]:
        return local_spec(skill_id, base_metrics, now=t,
                          reason="未配置 AI 端点/密钥/模型，使用本地保守模板（未联网）")
    sources: list[dict] = []
    if urls:
        try:
            sources = fetch_sources(urls, allow_private=cfg["allow_private"])
        except ForgeError as exc:
            return local_spec(skill_id, base_metrics, now=t,
                              reason=f"资料抓取失败，回退本地模板：{exc}")
    else:
        # 没给网址就自己联网找；找不到就回退，不编造参考资料。
        sources = discover_sources(skill_id, allow_private=cfg["allow_private"])
        if not sources:
            return local_spec(skill_id, base_metrics, now=t,
                              reason="联网检索没有找到可用资料，使用本地保守模板")
    try:
        return ai_spec(skill_id, base_metrics, sources, now=t, cfg=cfg)
    except ForgeError as exc:
        return local_spec(skill_id, base_metrics, now=t, sources=sources,
                          reason=f"AI 生成未通过校验，回退本地模板：{exc}")
