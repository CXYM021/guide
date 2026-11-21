# -*- coding: utf-8 -*-
"""标准来源配置：把「配 AI」从命令行搬进设置页。

用户看到的三句话（界面照这个写）：
1. 不配也能加技能：按自己填的指标给一套「通用起步标准」，本地生成、不联网。
2. 配了会更有出处：先查公开资料，再让 AI 折算成 LV1–LV8 门槛。
3. 不管配没配，练习打分都在本地算，练习记录不上传。

工程约束：
* 优先级 env > data/ai_secret.json > 预设默认值。env 优先是为了让「不想把钥匙
  落盘」的人仍旧走得通（README 里那条老路径继续有效）。
* 密钥只进 ai_secret.json，不混进 config.json、不回传界面（只回掩码）、不写日志、
  不进导出包（导出走 data_vault，那里按白名单取文件，本文件不在名单里）。
* 本模块不碰生成逻辑；生成仍然由 skill_forge.forge_skill_spec(config=...) 负责。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

SECRET_FILE = "ai_secret.json"
MAX_ENDPOINT = 300
MAX_MODEL = 64
MAX_KEY = 200
AI_TEST_TIMEOUT = 12

# 预设：端点只存 base，skill_forge.call_llm 会自动补 /chat/completions，
# 所以用户少写后缀也不会失败。
PRESETS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "endpoint": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "hint": "便宜，折算中文资料够用",
    },
    "dashscope": {
        "label": "通义千问",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "hint": "阿里云百炼的 OpenAI 兼容口",
    },
    "moonshot": {
        "label": "Kimi",
        "endpoint": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "hint": "长文档资料折算更稳",
    },
    "openai": {
        "label": "OpenAI",
        "endpoint": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "hint": "需要能直连",
    },
    "custom": {
        "label": "我自己填",
        "endpoint": "",
        "model": "",
        "hint": "任何 OpenAI 兼容的 chat/completions 服务",
    },
}
PRESET_ORDER = ("deepseek", "dashscope", "moonshot", "openai", "custom")


class AiConfigError(ValueError):
    """配置写不进去，消息可直接给用户看。"""


# ---------------------------------------------------------------- 读


def secret_path(data_dir) -> Path:
    return Path(data_dir) / SECRET_FILE


def _read_secret(data_dir) -> dict:
    path = secret_path(data_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _env(name: str) -> str:
    return str(os.environ.get(name, "") or "").strip()


def load_ai_config(data_dir) -> dict:
    """合并成 skill_forge 吃得下的配置；同时标出每项的来源。"""
    stored = _read_secret(data_dir)
    preset = str(stored.get("preset") or "custom").strip()
    if preset not in PRESETS:
        preset = "custom"
    base = PRESETS[preset]
    endpoint = str(stored.get("endpoint") or base["endpoint"] or "").strip()
    model = str(stored.get("model") or base["model"] or "").strip()
    key = str(stored.get("key") or "").strip()
    source = "local" if (endpoint and model and key) else "none"

    # 环境变量逐项覆盖：三项齐全才算「用 env 配好了」。
    env_endpoint, env_key, env_model = (_env("PROF_AI_ENDPOINT"),
                                        _env("PROF_AI_KEY"),
                                        _env("PROF_AI_MODEL"))
    if env_endpoint:
        endpoint = env_endpoint
    if env_key:
        key = env_key
    if env_model:
        model = env_model
    if env_endpoint and env_key and env_model:
        preset, source = "env", "env"
    elif (env_endpoint or env_key or env_model) and endpoint and model and key:
        source = "env"

    return {
        "preset": preset,
        "endpoint": endpoint,
        "model": model,
        "key": key,
        "source": source,
        "allow_private": _env("PROF_AI_ALLOW_PRIVATE_URLS").lower()
        in ("1", "true", "yes"),
    }


# ---------------------------------------------------------------- 写


def _clean_endpoint(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if len(text) > MAX_ENDPOINT:
        raise AiConfigError("服务地址太长，检查是不是粘错了")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise AiConfigError("服务地址要以 http:// 或 https:// 开头，例：https://api.deepseek.com")
    return text


def save_ai_config(data_dir, *, preset: object = None, endpoint: object = None,
                   model: object = None, key: object = None) -> dict:
    """存一份配置。key 传空字符串表示「不换钥匙」，传 None 表示保持原值。"""
    pid = str(preset or "").strip()
    if pid not in PRESETS:
        pid = "custom"
    base = PRESETS[pid]
    clean_endpoint = _clean_endpoint(
        endpoint if str(endpoint or "").strip() else base["endpoint"])
    clean_model = str(model if str(model or "").strip() else base["model"]).strip()
    if len(clean_model) > MAX_MODEL:
        raise AiConfigError("模型名太长（最多 64 字符）")
    clean_key = str(key or "").strip()
    if len(clean_key) > MAX_KEY:
        raise AiConfigError("钥匙太长，检查是不是多贴了东西")
    if clean_key and clean_key != "" and any(c in clean_key for c in " \r\n\t"):
        raise AiConfigError("钥匙里不能有空格或换行，重新复制一次")

    old = _read_secret(data_dir)
    record = {"preset": pid, "endpoint": clean_endpoint,
              "model": clean_model,
              "key": clean_key if clean_key else str(old.get("key") or "")}
    _write_secret(data_dir, record)
    return load_ai_config(data_dir)


def clear_ai_key(data_dir) -> dict:
    old = _read_secret(data_dir)
    _write_secret(data_dir, {**old, "key": ""})
    return load_ai_config(data_dir)


def _write_secret(data_dir, record: dict) -> None:
    path = secret_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        try:                       # 尽力而为：POSIX 上收紧到 0600，Windows 忽略
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)
    except OSError as exc:
        raise AiConfigError(f"配置存不下来（{type(exc).__name__}），换个可写目录再试") from exc


# ---------------------------------------------------------------- 给界面


def mask_key(key: str) -> str:
    text = str(key or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "…" + text[-2:]
    return text[:3] + "…" + text[-4:]


def public_status(cfg: dict) -> dict:
    """回给界面：绝不带明文密钥。"""
    endpoint = str(cfg.get("endpoint") or "")
    configured = bool(endpoint and cfg.get("model") and cfg.get("key"))
    host = urlparse(endpoint).hostname if endpoint else ""
    return {
        "configured": configured,
        "preset": str(cfg.get("preset") or "custom"),
        "preset_label": PRESETS.get(str(cfg.get("preset") or ""), PRESETS["custom"])["label"],
        "model": str(cfg.get("model") or "") if configured else "",
        "endpoint": endpoint if configured else "",
        "endpoint_host": host or "" if configured else "",
        "masked_key": mask_key(cfg.get("key", "")) if configured else "",
        "source": str(cfg.get("source") or "none"),   # env / local / none
        "key_on_disk": str(cfg.get("source")) == "local",
        "allow_private": bool(cfg.get("allow_private")),
        "discovery": "wikipedia-zh/en",
        "needs_urls": False,
        "presets": [
            {"id": pid, "label": PRESETS[pid]["label"],
             "endpoint": PRESETS[pid]["endpoint"],
             "model": PRESETS[pid]["model"],
             "hint": PRESETS[pid].get("hint", "")}
            for pid in PRESET_ORDER
        ],
    }


def config_payload(data_dir) -> dict:
    cfg = load_ai_config(data_dir)
    status = public_status(cfg)
    return {
        "ok": True,
        **status,
        "save_path": str(secret_path(data_dir)),
        "note": ("钥匙只存这台电脑（ai_secret.json），界面只回后 4 位，练习记录不上传。"
                 "不想落盘可以改用环境变量 PROF_AI_ENDPOINT / PROF_AI_KEY / PROF_AI_MODEL。"),
    }


# ---------------------------------------------------------------- 连通性测试


def _chat_url(endpoint: str) -> str:
    url = str(endpoint or "").rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return url


def _human_http_error(code: int) -> str:
    return {
        400: "对方拒绝了请求：多半是模型名写错，或这个地址不是 OpenAI 兼容口",
        401: "钥匙无效，或复制的时候少了几位",
        402: "账户余额不足",
        403: "这把钥匙没有这个模型的权限",
        404: "服务地址不对：检查是不是漏了 /v1，或者多写了 /chat/completions",
        429: "调用太频繁或额度用完了，等几分钟再试",
    }.get(code, f"对方返回 HTTP {code}")


def test_ai_connection(cfg: dict) -> dict:
    """真的发一条最小请求。失败一定翻成人话，不抛堆栈给界面。"""
    endpoint = str(cfg.get("endpoint") or "").strip()
    key = str(cfg.get("key") or "").strip()
    model = str(cfg.get("model") or "").strip()
    missing = [name for name, value in
               (("服务地址", endpoint), ("模型名", model), ("钥匙", key)) if not value]
    if missing:
        return {"ok": False, "error": "还差" + "、".join(missing)}
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _chat_url(endpoint), data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=AI_TEST_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:160]
        except Exception:                     # 读 body 失败不影响结论
            detail = ""
        return {"ok": False, "http_status": int(exc.code),
                "error": _human_http_error(int(exc.code)),
                "detail": detail}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False,
                "error": f"连不上（{type(exc).__name__}）：检查网络，或公司网要挂代理"}
    except ValueError:
        return {"ok": False, "error": "对方回的不是 JSON：这个地址可能是网页，不是接口"}
    ms = int((time.monotonic() - started) * 1000)
    served = str((payload or {}).get("model") or model)
    return {"ok": True, "ms": ms, "model": served,
            "message": f"通了：{served}，{ms} 毫秒"}
