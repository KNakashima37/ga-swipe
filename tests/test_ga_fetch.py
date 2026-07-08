# -*- coding: utf-8 -*-
"""ga_fetch.py の単体テスト（ネット不要）。

実行方法（リポジトリのルートで）:
  python3 -m unittest discover -s tests -v
"""
import datetime as dt
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


_FEED_HEADER = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<feed xmlns="http://www.w3.org/2005/Atom" '
                'xmlns:arxiv="http://arxiv.org/schemas/atom">')


def _make_feed_xml(ids, published="2025-06-12T17:30:00Z"):
    entries = "".join(f"""
 <entry>
  <id>http://arxiv.org/abs/{i}v1</id>
  <published>{published}</published>
  <updated>{published}</updated>
  <title>Paper {i}</title>
  <summary>Abstract {i}</summary>
  <author><name>A. Researcher</name></author>
  <arxiv:primary_category term="astro-ph.GA"/>
  <category term="astro-ph.GA"/>
 </entry>""" for i in ids)
    return f"{_FEED_HEADER}{entries}\n</feed>".encode()


def _parse_entries(ids, published="2025-06-12T17:30:00Z"):
    import feedparser
    return feedparser.parse(_make_feed_xml(ids, published)).entries


class _FakeResp:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.raw


class TestFetchPage(unittest.TestCase):
    """_fetch_page のリトライ挙動（urlopen と sleep をモック、ネット不要）。"""

    def _run(self, side_effects, **kw):
        from unittest import mock
        with mock.patch.object(ga_fetch.urllib.request, "urlopen",
                               side_effect=side_effects) as m, \
             mock.patch.object(ga_fetch.time, "sleep"):
            result = ga_fetch._fetch_page("http://x", "ua", **kw)
        return result, m.call_count

    def test_retries_after_http_error_then_succeeds(self):
        import urllib.error
        err = urllib.error.HTTPError("http://x", 406, "Not Acceptable", {}, None)
        entries, calls = self._run([err, _FakeResp(_make_feed_xml(["2506.10001"]))])
        self.assertEqual(len(entries), 1)
        self.assertEqual(calls, 2)

    def test_retries_after_empty_response_then_succeeds(self):
        empty = _FakeResp(_make_feed_xml([]))
        ok = _FakeResp(_make_feed_xml(["2506.10001"]))
        entries, calls = self._run([empty, ok])
        self.assertEqual(len(entries), 1)
        self.assertEqual(calls, 2)

    def test_returns_empty_after_all_retries_empty(self):
        empty = [_FakeResp(_make_feed_xml([])) for _ in range(3)]
        entries, calls = self._run(empty, tries=3)
        self.assertEqual(entries, [])
        self.assertEqual(calls, 3)

    def test_raises_after_persistent_errors(self):
        import urllib.error
        err = urllib.error.HTTPError("http://x", 406, "Not Acceptable", {}, None)
        from unittest import mock
        with mock.patch.object(ga_fetch.urllib.request, "urlopen", side_effect=[err] * 3), \
             mock.patch.object(ga_fetch.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                ga_fetch._fetch_page("http://x", "ua", tries=3)


class TestFetchArxiv(unittest.TestCase):
    """fetch_arxiv のページング挙動（_fetch_page をモック、ネット不要）。"""

    def _fetch(self, pages, max_results=10, page_size=2, since_year=2020):
        import datetime as dt
        from unittest import mock
        since = dt.datetime(since_year, 1, 1, tzinfo=ga_fetch.UTC)
        with mock.patch.object(ga_fetch, "_fetch_page", side_effect=pages), \
             mock.patch.object(ga_fetch.time, "sleep"):
            return ga_fetch.fetch_arxiv("astro-ph.GA", since, max_results,
                                        "t@example.com", page_size=page_size)

    def test_short_page_does_not_truncate(self):
        # 途中のページが要求(2件)より短くても、次が空になるまで取得を続けること
        pages = [_parse_entries(["2506.10001"]),   # 短いページ（1件 < 2件）
                 _parse_entries(["2506.10002"]),
                 []]
        papers = self._fetch(pages)
        self.assertEqual([p["id"] for p in papers], ["2506.10001", "2506.10002"])

    def test_stops_at_since_boundary(self):
        # since より古い published が現れたらそのページで打ち切ること
        pages = [_parse_entries(["2506.10001"], published="2019-06-12T00:00:00Z")]
        papers = self._fetch(pages, since_year=2020)
        self.assertEqual(papers, [])

    def test_dedupes_cross_listed_versions(self):
        pages = [_parse_entries(["2506.10001", "2506.10001"]), []]
        papers = self._fetch(pages)
        self.assertEqual(len(papers), 1)


class TestTooFewResults(unittest.TestCase):
    def test_triggers_on_few_results_with_wide_window(self):
        self.assertTrue(ga_fetch.too_few_results(0, days=7, min_results=10))
        self.assertTrue(ga_fetch.too_few_results(9, days=7, min_results=10))

    def test_ok_when_enough_results(self):
        self.assertFalse(ga_fetch.too_few_results(10, days=7, min_results=10))

    def test_disabled_for_narrow_window_or_state_mode(self):
        # --days 未指定（状態ベースの差分取得）や短い窓では0件もあり得るので発動しない
        self.assertFalse(ga_fetch.too_few_results(0, days=None, min_results=10))
        self.assertFalse(ga_fetch.too_few_results(0, days=1, min_results=10))

    def test_disabled_with_zero_threshold(self):
        self.assertFalse(ga_fetch.too_few_results(0, days=7, min_results=0))


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


class TestUsageSnapshot(unittest.TestCase):
    def test_computes_percent_and_formats_timestamp(self):
        when = dt.datetime(2026, 7, 8, 21, 0, 0, tzinfo=ga_fetch.UTC)
        snap = ga_fetch.usage_snapshot(123456, 500000, when)
        self.assertEqual(snap["character_count"], 123456)
        self.assertEqual(snap["character_limit"], 500000)
        self.assertAlmostEqual(snap["percent"], 24.69, places=2)
        self.assertEqual(snap["updated"], "2026-07-08T21:00:00Z")

    def test_zero_limit_does_not_divide_by_zero(self):
        when = dt.datetime(2026, 7, 8, tzinfo=ga_fetch.UTC)
        snap = ga_fetch.usage_snapshot(0, 0, when)
        self.assertEqual(snap["percent"], 0)


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
