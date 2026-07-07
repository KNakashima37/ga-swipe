# -*- coding: utf-8 -*-
"""ga_fetch.py の単体テスト（ネット不要）。

実行方法（リポジトリのルートで）:
  python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ga_fetch


class TestNormalizeEntry(unittest.TestCase):
    def _sample_papers(self):
        import feedparser
        feed = feedparser.parse(ga_fetch.SAMPLE_ATOM)
        return [p for p in (ga_fetch.normalize_entry(e) for e in feed.entries) if p]

    def test_sample_atom(self):
        papers = self._sample_papers()
        self.assertEqual(len(papers), 1)
        p = papers[0]
        self.assertEqual(p["id"], "2506.10001")
        self.assertEqual(p["version"], "v1")
        self.assertEqual(p["primary"], "astro-ph.GA")
        self.assertEqual(p["cross"], ["astro-ph.IM"])
        self.assertEqual(len(p["authors"]), 2)
        self.assertEqual(p["published"], "2025-06-12")
        self.assertEqual(p["abs_url"], "https://arxiv.org/abs/2506.10001")
        self.assertEqual(p["pdf_url"], "https://arxiv.org/pdf/2506.10001")

    def test_bad_id_returns_none(self):
        e = {"id": "http://example.com/not-arxiv",
             "published_parsed": (2025, 6, 12, 0, 0, 0, 0, 0, 0)}
        self.assertIsNone(ga_fetch.normalize_entry(e))

    def test_missing_published_returns_none(self):
        e = {"id": "http://arxiv.org/abs/2506.10001v1", "published_parsed": None}
        self.assertIsNone(ga_fetch.normalize_entry(e))


class TestMathMask(unittest.TestCase):
    def test_roundtrip_basic(self):
        text = r"We measure $\tau$ and $$x + y$$ at $T<100$ K."
        masked, store = ga_fetch.mask_math(text)
        self.assertNotIn("$", masked)
        self.assertEqual(len(store), 3)
        restored = ga_fetch.unmask_math(masked, store)
        self.assertIn(r"$\tau$", restored)
        self.assertIn("$$x + y$$", restored)
        self.assertIn("$T<100$", restored)

    def test_roundtrip_many_tokens_no_index_collision(self):
        # ZMATH1Z と ZMATH10Z 等が混同されないこと（トークンが10個以上）
        parts = [f"${{c_{i}}}$" for i in range(12)]
        text = " word ".join(parts)
        masked, store = ga_fetch.mask_math(text)
        self.assertEqual(len(store), 12)
        restored = ga_fetch.unmask_math(masked, store)
        for seg in parts:
            self.assertIn(seg, restored)

    def test_unmask_case_insensitive(self):
        # 翻訳エンジンがトークンの大小文字を変えても復元できること
        text = r"flux $\sigma$ level"
        masked, store = ga_fetch.mask_math(text)
        mangled = masked.replace("ZMATH0Z", "zmath0z")
        restored = ga_fetch.unmask_math(mangled, store)
        self.assertIn(r"$\sigma$", restored)


class _FakeTranslator:
    name = "fake"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def translate(self, text):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return "JA:" + text


def _paper(pid="2506.10001"):
    return {"id": pid, "title": "A title", "abstract": "An abstract"}


class TestTranslatePapers(unittest.TestCase):
    def test_translates_and_caches(self):
        tr, cache = _FakeTranslator(), {}
        papers = [_paper()]
        n = ga_fetch.translate_papers(papers, tr, cache, limit=None)
        self.assertEqual(n, 2)  # title + abstract
        self.assertEqual(papers[0]["title_ja"], "JA:A title")
        self.assertEqual(cache["fake:2506.10001:title"], "JA:A title")

    def test_cache_hit_skips_api_call(self):
        tr = _FakeTranslator()
        cache = {"fake:2506.10001:title": "cached-title",
                 "fake:2506.10001:abstract": "cached-abs"}
        papers = [_paper()]
        ga_fetch.translate_papers(papers, tr, cache, limit=None)
        self.assertEqual(tr.calls, 0)
        self.assertEqual(papers[0]["title_ja"], "cached-title")

    def test_failure_yields_empty_and_not_cached(self):
        tr, cache = _FakeTranslator(fail=True), {}
        papers = [_paper()]
        ga_fetch.translate_papers(papers, tr, cache, limit=None)
        self.assertEqual(papers[0]["title_ja"], "")
        self.assertEqual(cache, {})  # 失敗は次回リトライできるようキャッシュしない

    def test_limit_zero_uses_cache_only(self):
        tr = _FakeTranslator()
        cache = {"fake:2506.10001:title": "cached-title"}
        papers = [_paper()]
        ga_fetch.translate_papers(papers, tr, cache, limit=0)
        self.assertEqual(tr.calls, 0)
        self.assertEqual(papers[0]["title_ja"], "cached-title")
        self.assertEqual(papers[0]["abstract_ja"], "")

    def test_title_only_keeps_abstract_untranslated(self):
        tr, cache = _FakeTranslator(), {}
        papers = [_paper()]
        n = ga_fetch.translate_papers(papers, tr, cache, limit=None, title_only=True)
        self.assertEqual(n, 1)
        self.assertEqual(papers[0]["abstract_ja"], "")


class TestMergeSeenIds(unittest.TestCase):
    def test_appends_new_and_dedupes(self):
        self.assertEqual(ga_fetch.merge_seen_ids(["a", "b"], ["b", "c"]),
                         ["a", "b", "c"])

    def test_cap_drops_oldest_first(self):
        # 上限超過時は「古いID」から捨てられること（新しいIDは必ず残る）
        old = [f"id{i}" for i in range(3000)]
        out = ga_fetch.merge_seen_ids(old, ["new1", "new2"])
        self.assertEqual(len(out), 3000)
        self.assertNotIn("id0", out)
        self.assertNotIn("id1", out)
        self.assertEqual(out[-2:], ["new1", "new2"])

    def test_empty_old(self):
        self.assertEqual(ga_fetch.merge_seen_ids([], ["a"]), ["a"])


class TestSaveJson(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "x.json")
            ga_fetch.save_json(path, {"k": "v"})
            self.assertEqual(ga_fetch.load_json(path, None), {"k": "v"})

    def test_interrupted_write_keeps_original(self):
        # 書き込み途中で落ちても既存ファイルが破損しないこと（アトミック性）
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.json")
            ga_fetch.save_json(path, {"k": "old"})
            with mock.patch.object(ga_fetch.json, "dump", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    ga_fetch.save_json(path, {"k": "new"})
            self.assertEqual(ga_fetch.load_json(path, None), {"k": "old"})


class TestLoadJson(unittest.TestCase):
    def test_missing_file_returns_fallback(self):
        self.assertEqual(ga_fetch.load_json("/nonexistent/x.json", {"a": 1}), {"a": 1})

    def test_corrupt_file_returns_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "broken.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"truncated": ')
            self.assertEqual(ga_fetch.load_json(path, []), [])


if __name__ == "__main__":
    unittest.main()
