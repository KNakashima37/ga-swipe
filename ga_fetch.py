#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ga_fetch.py  —  astro-ph.GA 新着取得 + 日本語訳ダイジェスト生成（option B / 検証用）

やること:
  1) arXiv API から指定カテゴリ（既定 astro-ph.GA）の新着を取得
  2) 「前回実行以降」を seen-id + 取得窓のオーバーラップで堅く判定
  3) タイトル・アブストを日本語訳（engine: deepl / argos / none を選択）
     - $...$ の数式はマスクして翻訳に巻き込まれないよう保護
     - 訳はキャッシュ（再実行で再翻訳しない＝無料枠の節約）
  4) digest_*.md / digest_*.html / papers_*.json を出力

依存:
  必須 : feedparser            （pip install feedparser）
  任意 : deepl                 （engine=deepl のとき / pip install deepl）
        argostranslate         （engine=argos のとき / pip install argostranslate）

使い方の例:
  python ga_fetch.py --selftest                 # ネット不要の動作確認
  python ga_fetch.py --engine none --days 2     # 取得だけ（翻訳なし・直近2日）
  DEEPL_API_KEY=xxxx:fx python ga_fetch.py --engine deepl --days 1 --limit 10
  python ga_fetch.py --engine argos             # オフライン翻訳（初回はモデルDL）

arXiv API の作法:
  - リクエスト間は 3 秒以上あける（本スクリプトは自動で sleep）
  - User-Agent に連絡先を入れる（--contact か環境変数 GA_CONTACT）
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

UTC = dt.timezone.utc
ARXIV_API = "https://export.arxiv.org/api/query"
DEFAULT_CATEGORY = "astro-ph.GA"


# --------------------------------------------------------------------------
# 状態 / キャッシュ
# --------------------------------------------------------------------------
def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def too_few_results(n_papers, days, min_results):
    """--days 指定の定期取得で件数が閾値未満なら True（arXiv API 不調の疑い）。
    取得窓が3日以上あれば astro-ph.GA の新着が閾値を下回ることは通常ないため、
    この場合は成果物を更新せず異常終了させる（空の latest.json で上書きしない）。"""
    return (days is not None and days >= 3
            and min_results > 0 and n_papers < min_results)


def merge_seen_ids(old_ids, new_ids, cap=3000):
    """既出IDリストに new_ids を追記し、古い方から捨てて cap 件に保つ（順序を保持）。"""
    seen = set(old_ids)
    merged = list(old_ids)
    for i in new_ids:
        if i not in seen:
            seen.add(i)
            merged.append(i)
    return merged[-cap:]


def save_json(path, obj):
    # 一時ファイルに書いてから os.replace で差し替える（中断時に元ファイルを壊さない）
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# arXiv 取得
# --------------------------------------------------------------------------
def _fetch_page(url, ua, tries=3, wait=5.0):
    """arXiv API から1ページ取得。一時的な失敗（HTTPエラー・タイムアウト・空応答）は
    wait 秒あけて最大 tries 回まで再試行する（arXiv の3秒間隔の要請より長い間隔）。
    再試行しても空のままなら [] を返し、エラーのままなら例外を投げる。"""
    import feedparser  # 必須依存。ここで import して未導入時に分かりやすく落とす。

    last_err = None
    for attempt in range(tries):
        if attempt:
            print(f"[warn] arXiv 応答不良のため再試行 {attempt}/{tries - 1} …", file=sys.stderr)
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except Exception as ex:
            last_err = ex
            continue
        entries = feedparser.parse(raw).entries
        if entries:
            return entries
        # 空応答: arXiv API は一時的に空を返すことがある → 再試行
    if last_err is not None:
        raise last_err
    return []


def fetch_arxiv(category, since_dt, max_results, contact, page_size=100):
    """category の論文を submittedDate 降順で取得し、published >= since_dt のものを返す。"""
    ua = f"ga-swipe/0.1 (mailto:{contact})"
    out, start = [], 0
    while len(out) < max_results:
        params = {
            "search_query": f"cat:{category}",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "start": start,
            "max_results": min(page_size, max_results - len(out)),
        }
        url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
        entries = _fetch_page(url, ua)
        if not entries:
            break  # リトライ後も空 → 結果の末尾に達したと判断

        stop = False
        for e in entries:
            p = normalize_entry(e)
            if p is None:
                continue
            if p["published_dt"] < since_dt:
                # 降順なのでこれ以降は全部古い → 打ち切り
                stop = True
                break
            out.append(p)
        if stop:
            break
        # 要求より短いページでも打ち切らない（arXiv API は途中のページを一時的に
        # 短く返すことがある）。末尾かどうかは次の要求が空かどうかで判定する。
        start += len(entries)
        time.sleep(3.0)  # arXiv への礼儀

    # ベースID重複（クロスリスト/版違い）を排除：最初に出たもの＝最新版を残す
    seen, deduped = set(), []
    for p in out:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        deduped.append(p)
    return deduped


def normalize_entry(e):
    """feedparser のエントリを扱いやすい dict に整形。"""
    raw_id = e.get("id", "")
    m = re.search(r"abs/([^v]+)(v\d+)?$", raw_id)
    if not m:
        return None
    base_id = m.group(1)
    version = m.group(2) or ""

    pp = e.get("published_parsed")
    up = e.get("updated_parsed")
    if pp is None:
        return None
    published_dt = dt.datetime(*pp[:6], tzinfo=UTC)
    updated_dt = dt.datetime(*up[:6], tzinfo=UTC) if up else published_dt

    primary = ""
    if e.get("arxiv_primary_category"):
        primary = e["arxiv_primary_category"].get("term", "")
    cats = [t.get("term", "") for t in e.get("tags", []) if t.get("term")]
    cross = [c for c in cats if c != primary]

    return {
        "id": base_id,
        "version": version,
        "title": _clean(e.get("title", "")),
        "abstract": _clean(e.get("summary", "")),
        "authors": [a.get("name", "") for a in e.get("authors", [])],
        "primary": primary or (cats[0] if cats else ""),
        "cross": cross,
        "comment": _clean(e.get("arxiv_comment", "") or ""),
        "published": published_dt.strftime("%Y-%m-%d"),
        "published_dt": published_dt,
        "updated": updated_dt.strftime("%Y-%m-%d"),
        "abs_url": f"https://arxiv.org/abs/{base_id}",
        "pdf_url": f"https://arxiv.org/pdf/{base_id}",
    }


def _clean(s):
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# 数式マスク（$...$ を翻訳から保護）
# --------------------------------------------------------------------------
_MATH_RE = re.compile(r"\$\$.+?\$\$|\$[^$]*\$")


def mask_math(text):
    store = []

    def repl(m):
        store.append(m.group(0))
        return f" ZMATH{len(store) - 1}Z "

    return _MATH_RE.sub(repl, text), store


def unmask_math(text, store):
    for i, seg in enumerate(store):
        # 訳出時に前後の空白や大小文字が揺れても戻せるよう緩めに置換
        text = re.sub(rf"\s*ZMATH{i}Z\s*", " " + seg.replace("\\", "\\\\") + " ", text, flags=re.I)
    return _clean(text)


# --------------------------------------------------------------------------
# 翻訳エンジン
# --------------------------------------------------------------------------
class NoneTranslator:
    name = "none"

    def translate(self, text):
        return ""  # 翻訳なし（原文のみ表示）


class DeepLTranslator:
    name = "deepl"

    def __init__(self, api_key):
        import deepl
        self.client = deepl.Translator(api_key)

    def translate(self, text):
        masked, store = mask_math(text)
        res = self.client.translate_text(masked, source_lang="EN", target_lang="JA")
        return unmask_math(res.text, store)

    def usage(self):
        u = self.client.get_usage()
        return u.character.count, u.character.limit


class ArgosTranslator:
    name = "argos"

    def __init__(self):
        import argostranslate.package as pkg
        import argostranslate.translate as tr
        installed = {(l.code) for l in tr.get_installed_languages()}
        if "ja" not in installed or "en" not in installed:
            print("[argos] en→ja モデルを取得中（初回のみ・ネット必要）…", file=sys.stderr)
            pkg.update_package_index()
            avail = pkg.get_available_packages()
            target = next((p for p in avail if p.from_code == "en" and p.to_code == "ja"), None)
            if target is None:
                raise RuntimeError("argos: en→ja パッケージが見つかりません")
            pkg.install_from_path(target.download())
        self.tr = tr

    def translate(self, text):
        masked, store = mask_math(text)
        out = self.tr.translate(masked, "en", "ja")
        return unmask_math(out, store)


def get_translator(engine):
    if engine == "none":
        return NoneTranslator()
    if engine == "deepl":
        key = os.environ.get("DEEPL_API_KEY")
        if not key:
            sys.exit("エラー: engine=deepl には環境変数 DEEPL_API_KEY が必要です。")
        return DeepLTranslator(key)
    if engine == "argos":
        return ArgosTranslator()
    sys.exit(f"未知の engine: {engine}")


# --------------------------------------------------------------------------
# 翻訳適用（キャッシュつき）
# --------------------------------------------------------------------------
def translate_papers(papers, translator, cache, limit, title_only=False):
    fields = ("title",) if title_only else ("title", "abstract")
    n_calls = 0
    for i, p in enumerate(papers):
        do_translate = translator.name != "none" and (limit is None or i < limit)
        if title_only:
            p.setdefault("abstract_ja", "")  # アブストは原文のまま（スワイプアプリ用）
        for field in fields:
            ck = f"{translator.name}:{p['id']}:{field}"
            if not do_translate:
                p[f"{field}_ja"] = cache.get(ck, "")
                continue
            if ck in cache:
                p[f"{field}_ja"] = cache[ck]
                continue
            try:
                ja = translator.translate(p[field])
            except Exception as ex:
                print(f"[warn] 翻訳失敗 {p['id']} {field}: {ex}", file=sys.stderr)
                ja = ""
            p[f"{field}_ja"] = ja
            if ja:
                cache[ck] = ja
            n_calls += 1
    return n_calls


# --------------------------------------------------------------------------
# 出力（Markdown / HTML）
# --------------------------------------------------------------------------
def render_markdown(papers, meta):
    L = []
    L.append(f"# astro-ph.GA 新着ダイジェスト  ({meta['generated']})\n")
    L.append(f"- カテゴリ: `{meta['category']}` / 取得窓: {meta['since']} 以降 / 翻訳: `{meta['engine']}`")
    L.append(f"- 新着 **{len(papers)}** 件\n\n---\n")
    for n, p in enumerate(papers, 1):
        cross = " ".join(f"`{c}`" for c in p["cross"])
        authors = ", ".join(p["authors"][:3]) + (" ほか" if len(p["authors"]) > 3 else "")
        title = p.get("title_ja") or p["title"]
        L.append(f"## {n}. {title}\n")
        L.append(f"- arXiv: [{p['id']}]({p['abs_url']}) · `{p['primary']}` {cross} · {p['published']}")
        L.append(f"- 著者: {authors}")
        if p.get("title_ja"):
            L.append(f"- 原題: {p['title']}")
        if p["comment"]:
            L.append(f"- comment: {p['comment']}")
        L.append("")
        body = p.get("abstract_ja") or p["abstract"]
        L.append(body + "\n")
        if p.get("abstract_ja"):
            L.append("<details><summary>原文アブスト</summary>\n")
            L.append(p["abstract"] + "\n")
            L.append("</details>\n")
        L.append(f"[abs]({p['abs_url']}) · [PDF]({p['pdf_url']})\n")
        L.append("---\n")
    return "\n".join(L)


def render_html(papers, meta):
    css = """
    body{background:#0b1020;color:#e8ecf6;font-family:'Noto Sans JP',system-ui,sans-serif;
         max-width:760px;margin:0 auto;padding:24px 18px;line-height:1.7}
    h1{font-size:20px} .meta{color:#9aa6c4;font-size:13px;margin-bottom:18px}
    .card{background:#141b30;border:1px solid #2a3656;border-radius:14px;padding:18px;margin:14px 0}
    .badge{font-family:monospace;font-size:12px;background:#f2b24c;color:#0b1020;border-radius:6px;padding:1px 7px}
    .x{font-family:monospace;font-size:12px;color:#9aa6c4;border:1px solid #2a3656;border-radius:6px;padding:1px 6px;margin-left:4px}
    .ttl{font-size:17px;font-weight:700;margin:8px 0 2px} .en{color:#9aa6c4;font-size:13px}
    .auth{color:#9aa6c4;font-size:13px;margin:6px 0} .abs{margin:8px 0}
    a{color:#5aa9ff;text-decoration:none;margin-right:12px;font-family:monospace;font-size:13px}
    details{margin-top:8px;color:#c4cce0} summary{cursor:pointer;color:#9aa6c4;font-size:13px}
    """
    rows = []
    for n, p in enumerate(papers, 1):
        cross = "".join(f'<span class="x">{html.escape(c)}</span>' for c in p["cross"])
        authors = ", ".join(p["authors"][:3]) + (" ほか" if len(p["authors"]) > 3 else "")
        title = html.escape(p.get("title_ja") or p["title"])
        en = f'<div class="en">{html.escape(p["title"])}</div>' if p.get("title_ja") else ""
        body = html.escape(p.get("abstract_ja") or p["abstract"])
        orig = ""
        if p.get("abstract_ja"):
            orig = f'<details><summary>原文アブスト</summary><div class="abs">{html.escape(p["abstract"])}</div></details>'
        cmt = f' · {html.escape(p["comment"])}' if p["comment"] else ""
        rows.append(f"""
        <div class="card">
          <div><span class="badge">{html.escape(p['primary'])}</span>{cross}
               <span class="x">{p['id']}</span></div>
          <div class="ttl">{n}. {title}</div>{en}
          <div class="auth">{html.escape(authors)} · {p['published']}{cmt}</div>
          <div class="abs">{body}</div>{orig}
          <div><a href="{p['abs_url']}" target="_blank">abs</a><a href="{p['pdf_url']}" target="_blank">PDF</a></div>
        </div>""")
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>astro-ph.GA digest</title>
<script>window.MathJax={{tex:{{inlineMath:[['$','$'],['\\\\(','\\\\)']],displayMath:[['$$','$$'],['\\\\[','\\\\]']]}},options:{{skipHtmlTags:['script','noscript','style','textarea','pre']}}}};</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-mml-chtml.js"></script>
<style>{css}</style></head><body>
<h1>astro-ph.GA 新着ダイジェスト</h1>
<div class="meta">{meta['generated']} · カテゴリ {meta['category']} · {meta['since']} 以降 · 翻訳 {meta['engine']} · 新着 {len(papers)} 件</div>
{''.join(rows)}
</body></html>"""


# --------------------------------------------------------------------------
# メイン
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="astro-ph.GA 新着取得 + 日本語訳ダイジェスト")
    ap.add_argument("--category", default=DEFAULT_CATEGORY)
    ap.add_argument("--engine", choices=["none", "deepl", "argos"], default="none")
    ap.add_argument("--days", type=float, default=None,
                    help="状態を無視して直近 N 日を取得（初回や再取得に）")
    ap.add_argument("--overlap-days", type=float, default=1.0,
                    help="前回以降に少し重ねて取得しseen-idで重複排除（取りこぼし防止）")
    ap.add_argument("--max", type=int, default=200, help="取得上限")
    ap.add_argument("--min-results", type=int, default=10,
                    help="--days が3日以上のとき、取得がこの件数未満なら異常終了する"
                         "（API不調時に空データで上書きしない保険。0で無効）")
    ap.add_argument("--limit", type=int, default=None,
                    help="翻訳する件数の上限（無料枠の節約／先頭から N 件のみ訳す）")
    ap.add_argument("--title-only", action="store_true",
                    help="タイトルのみ翻訳（アブストは原文）。スワイプアプリ用・無料枠を節約")
    ap.add_argument("--ignore-seen", action="store_true", help="既出IDも再出力する")
    ap.add_argument("--format", choices=["md", "html", "both"], default="both")
    ap.add_argument("--workdir", default="./ga_swipe")
    ap.add_argument("--contact", default=os.environ.get("GA_CONTACT", "you@example.com"),
                    help="arXiv へ送る連絡先メール（礼儀。環境変数 GA_CONTACT でも可）")
    ap.add_argument("--selftest", action="store_true", help="ネット不要の自己テスト")
    ap.add_argument("--usage", action="store_true",
                    help="DeepL の文字数消費量を表示して終了（engine=deepl 用）")
    args = ap.parse_args()

    if args.selftest:
        return selftest(args)

    if args.usage:
        c, lim = get_translator("deepl").usage()
        pct = (c / lim * 100) if lim else 0
        print(f"DeepL 使用量: {c:,} / {lim:,} 文字 ({pct:.1f}%)")
        return

    os.makedirs(args.workdir, exist_ok=True)
    state_path = os.path.join(args.workdir, "state.json")
    cache_path = os.path.join(args.workdir, "cache.json")
    state = load_json(state_path, {"last_run": None, "seen_ids": []})
    cache = load_json(cache_path, {})
    seen_ids = set(state.get("seen_ids", []))

    now = dt.datetime.now(UTC)
    if args.days is not None:
        since = now - dt.timedelta(days=args.days)
    elif state.get("last_run"):
        since = dt.datetime.fromisoformat(state["last_run"]) - dt.timedelta(days=args.overlap_days)
    else:
        since = now - dt.timedelta(days=1)  # 初回既定

    print(f"取得: cat:{args.category} / {since.strftime('%Y-%m-%d %H:%M')}Z 以降 …", file=sys.stderr)
    papers = fetch_arxiv(args.category, since, args.max, args.contact)

    if too_few_results(len(papers), args.days, args.min_results):
        sys.exit(f"エラー: 取得が {len(papers)} 件しかありません（閾値 {args.min_results} 件）。"
                 "arXiv API の不調とみなして中断します（出力ファイルは更新されません）。")

    if not args.ignore_seen:
        papers = [p for p in papers if p["id"] not in seen_ids]
    print(f"新着 {len(papers)} 件", file=sys.stderr)

    translator = get_translator(args.engine)
    n_calls = translate_papers(papers, translator, cache, args.limit, args.title_only)
    if n_calls:
        print(f"翻訳 API/モデル呼び出し: {n_calls} 回", file=sys.stderr)
    if args.engine == "deepl":
        try:
            c, lim = translator.usage()
            pct = (c / lim * 100) if lim else 0
            print(f"DeepL 使用量: {c:,} / {lim:,} 文字 ({pct:.1f}%)", file=sys.stderr)
        except Exception:
            pass

    meta = {"generated": now.strftime("%Y-%m-%d %H:%M UTC"),
            "category": args.category, "since": since.strftime("%Y-%m-%d"),
            "engine": args.engine}
    stamp = now.strftime("%Y%m%d_%H%M")
    written = write_outputs(args, papers, meta, stamp)

    # 状態更新
    state["last_run"] = now.isoformat()
    state["seen_ids"] = merge_seen_ids(state.get("seen_ids", []), [p["id"] for p in papers])
    save_json(state_path, state)
    save_json(cache_path, cache)

    print("\n出力:", file=sys.stderr)
    for w in written:
        print("  " + w, file=sys.stderr)


def write_outputs(args, papers, meta, stamp):
    out_dir = os.path.join(args.workdir, "out")
    os.makedirs(out_dir, exist_ok=True)
    written = []
    dump = [{k: v for k, v in p.items() if k != "published_dt"} for p in papers]
    md_txt = render_markdown(papers, meta)
    html_txt = render_html(papers, meta)

    # 履歴用（タイムスタンプ付き）と、常に最新を指す latest.* の両方を書く
    save_json(os.path.join(out_dir, f"papers_{stamp}.json"), dump)
    save_json(os.path.join(out_dir, "latest.json"), dump)
    written.append(os.path.join(out_dir, f"papers_{stamp}.json"))
    if args.format in ("md", "both"):
        for name in (f"digest_{stamp}.md", "latest.md"):
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
                f.write(md_txt)
        written.append(os.path.join(out_dir, f"digest_{stamp}.md"))
    if args.format in ("html", "both"):
        for name in (f"digest_{stamp}.html", "latest.html"):
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
                f.write(html_txt)
        written.append(os.path.join(out_dir, f"digest_{stamp}.html"))
    written.append(os.path.join(out_dir, "latest.html") + "  ←『open ga_swipe/out/latest.html』で常に最新を開けます")
    return written


# --------------------------------------------------------------------------
# 自己テスト（ネット不要）：解析→マスク→整形を検証
# --------------------------------------------------------------------------
SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
 <entry>
  <id>http://arxiv.org/abs/2506.10001v1</id>
  <published>2025-06-12T17:30:00Z</published>
  <updated>2025-06-12T17:30:00Z</updated>
  <title>Sub-au Structure in Diffuse Clouds traced by HCO+ Absorption at $T<100$ K</title>
  <summary>We search for variation in $\\tau$ across two epochs. No change above $3\\sigma$ is found.</summary>
  <author><name>A. Researcher</name></author>
  <author><name>B. Collaborator</name></author>
  <arxiv:comment>14 pages, accepted by ApJ</arxiv:comment>
  <arxiv:primary_category term="astro-ph.GA"/>
  <category term="astro-ph.GA"/>
  <category term="astro-ph.IM"/>
 </entry>
</feed>"""


def selftest(args):
    try:
        import feedparser
    except Exception:
        sys.exit("feedparser が必要です: pip install feedparser")
    feed = feedparser.parse(SAMPLE_ATOM)
    papers = [normalize_entry(e) for e in feed.entries]
    papers = [p for p in papers if p]
    assert papers and papers[0]["id"] == "2506.10001", "ID解析に失敗"
    # 数式マスクの往復確認
    masked, store = mask_math(papers[0]["abstract"])
    assert "$" not in masked and "ZMATH" in masked, "数式マスク失敗"
    restored = unmask_math(masked, store)
    assert "\\tau" in restored and "\\sigma" in restored, "数式復元失敗"
    translate_papers(papers, NoneTranslator(), {}, None)
    meta = {"generated": "selftest", "category": args.category,
            "since": "2025-06-11", "engine": "none"}
    written = write_outputs(args, papers, meta, "selftest")
    print("self-test OK ✅  解析・数式マスク・整形・出力すべて通過")
    for w in written:
        print("  " + w)


if __name__ == "__main__":
    main()
