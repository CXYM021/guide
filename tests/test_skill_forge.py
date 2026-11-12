# -*- coding: utf-8 -*-
"""自定义技能的联网检索与回退路径。

沙箱里没有外网，所以这里用假的 JSON 取回函数验证「解析/去重/回退」逻辑；
真网请求由 forge_skill_spec 在配置了 AI 时发起，失败一律回退本地模板。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PRODUCT_DIR = Path(__file__).resolve().parents[1]
if str(PRODUCT_DIR) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DIR))

import skill_forge  # noqa: E402


def fake_pages(*titles, text_len=200):
    pages = []
    for i, title in enumerate(titles):
        pages.append({
            "title": title, "index": i + 1,
            "extract": ("关于" + title + "的简介。") * (text_len // 10),
        })
    return {"query": {"pages": pages}}


class TestDiscoverSources(unittest.TestCase):
    def test_collects_from_both_wikis_and_stops_at_limit(self):
        calls = []

        def fake(url):
            calls.append(url)
            return fake_pages("吉他", "Guitar")

        out = skill_forge.discover_sources("吉他", json_get=fake, limit=2)
        self.assertEqual(len(out), 2)
        self.assertEqual([s["title"] for s in out], ["吉他", "Guitar"])
        self.assertTrue(out[0]["url"].startswith("https://zh.wikipedia.org/wiki/"))
        self.assertTrue(all(s["text"] for s in out))

    def test_short_or_empty_extract_is_skipped(self):
        def fake(url):
            return {"query": {"pages": [
                {"title": "空条目", "extract": ""},
                {"title": "短条目", "extract": "太短"},
            ]}}

        self.assertEqual(skill_forge.discover_sources("x", json_get=fake), [])

    def test_network_error_is_swallowed(self):
        def boom(url):
            raise OSError("no network")

        self.assertEqual(skill_forge.discover_sources("吉他", json_get=boom), [])

    def test_duplicate_titles_are_deduped(self):
        def fake(url):
            return fake_pages("吉他")

        out = skill_forge.discover_sources("吉他", json_get=fake, limit=3)
        self.assertEqual(len(out), 1)


class TestForgeWithoutAi(unittest.TestCase):
    """没配 AI 时绝不联网、绝不冒充 AI。"""

    def test_local_template_is_used_and_marked(self):
        spec = skill_forge.forge_skill_spec(
            name="口琴",
            metrics=[{"key": "minutes", "label": "练习时长", "unit": "分钟",
                      "range_low": 0, "range_high": 120}],
            config={"endpoint": "", "key": "", "model": ""},
            now=1_760_000_000)
        self.assertEqual(spec["provenance"]["mode"], "local")
        self.assertFalse(spec["provenance"]["ai_used"])
        self.assertEqual(len(spec["metrics"][0]["level_requirements"]),
                         skill_forge.MAX_LEVEL)
        # 等级要求必须单调不减且在合法范围内
        reqs = spec["metrics"][0]["level_requirements"]
        self.assertEqual(reqs, sorted(reqs))
        self.assertTrue(all(0 <= v <= 120 for v in reqs))

    def test_ai_configured_but_no_sources_falls_back(self):
        """配置了 AI 但检索不到资料：回退本地模板，并写明原因。"""
        original = skill_forge.discover_sources
        skill_forge.discover_sources = lambda *a, **k: []
        try:
            spec = skill_forge.forge_skill_spec(
                name="口琴",
                metrics=[{"key": "minutes", "label": "练习时长", "unit": "分钟",
                          "range_low": 0, "range_high": 120}],
                config={"endpoint": "https://api.example.com/v1/chat",
                        "key": "k", "model": "m"},
                now=1_760_000_000)
        finally:
            skill_forge.discover_sources = original
        self.assertFalse(spec["provenance"]["ai_used"])
        self.assertIn("联网检索", spec["provenance"]["reason"])


if __name__ == "__main__":
    unittest.main()
