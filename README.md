# ga-swipe

arXiv の **astro-ph.GA** 新着論文を、スマホで Tinder のように左右スワイプして手早く仕分けるための個人用ツール。
タイトルは日本語訳つき。左スワイプで「見送り」、右スワイプで「気になる」に保存できる。

GitHub Actions が毎日 arXiv を取得・翻訳して `latest.json` を更新し、GitHub Pages で配信する。
そのため、ふだんは **スマホで URL を開く（再読み込みする）だけ** で最新の新着を確認できる（PC作業は不要）。

## 使い方（スマホ）

1. 公開URL `https://<ユーザー名>.github.io/ga-swipe/` を Safari 等で開く（共有 → ホーム画面に追加すると、アプリのように使える）。
2. 操作
   - 左スワイプ / ✕ … 見送り（既読にする）
   - 右スワイプ / ♥ … 気になる（既読＋保存）
   - アブストは上下スクロールで読む
   - 中央の「arXiv」ボタンで abs を開く（Safari のページ翻訳で日本語アブストを読める）
   - 「元に戻す」で直前のスワイプを取り消し
   - 右上のリストから保存一覧を表示し、Markdown / BibTeX でコピー
3. 既読・保存はその端末のブラウザに記録される（GitHub には送られない）。データを更新したいときはページを再読み込みするだけ。

## 構成ファイル

| ファイル | 役割 |
|---|---|
| `index.html` | スワイプUI本体（単一ファイル・依存なし）。`latest.json` を読み込んで動作 |
| `ga_fetch.py` | arXiv 取得＋タイトル日本語訳のスクリプト |
| `.github/workflows/update.yml` | 毎日 `ga_fetch.py` を実行して `latest.json` を自動更新 |
| `latest.json` | 直近の新着データ（Actions が自動生成。手で編集しない） |
| `ga_swipe/cache.json` | 翻訳キャッシュ（再翻訳を避けて無料枠を節約） |

## 仕組み（毎日の自動更新）

```
GitHub Actions(毎日) ──→ arXiv API 取得 ──→ DeepL でタイトル翻訳 ──→ latest.json をコミット
                                                                         │
                                              GitHub Pages で配信 ◀──────┘
                                                     │
                                              スマホで開く
```

無料枠を保つ工夫：

- **タイトルのみ翻訳**（`--title-only`）。アブストは原文のままで、必要なときに abs を開いてブラウザのページ翻訳で読む。
- **翻訳キャッシュ（cache.json）をコミット**するので、毎日訳すのは「その日新しく出たタイトル」だけ。DeepL の無料枠（月50万文字）はほぼ消費しない。
- 毎回 **直近7日ぶん** を出力するので、数日アプリを開かなくても取りこぼさない（スワイプ済みは端末側で自動除外）。

## セットアップ（再現用メモ）

1. このリポジトリに `index.html` と `ga_fetch.py`、`.github/workflows/update.yml` を置く。
2. **Settings → Secrets and variables → Actions** で2つの Secret を登録：
   - `DEEPL_API_KEY` … DeepL の API キー（無料版は末尾 `:fx`）
   - `GA_CONTACT` … arXiv へ送る連絡先メール
3. **Actions** タブで `Update arXiv digest` を一度「Run workflow」して `latest.json` を生成。
4. **Settings → Pages** で Source を「Deploy from a branch」、Branch を `main` / `/(root)` にして Save。
5. リポジトリは Public（無料の Pages / Actions のため）。中身は arXiv の公開アブストのみ。

## 既知の限界

- 取得は投稿日（submittedDate）基準のため、**過去に投稿され最近 GA にクロスリストされた論文**は一部拾えない。改訂版（v2 など）は新着として再表示しない。
- カード内でアブストまでワンタップ和訳するには、別途 DeepL を載せた小さなバックエンド（例：Cloudflare Worker）が必要。

## ローカルでの実行（任意）

PC で直接動かして、ダイジェスト（HTML/Markdown/JSON）を生成することもできる。

```bash
pip install feedparser deepl
export DEEPL_API_KEY="....:fx"
python ga_fetch.py --engine deepl --title-only --days 7 --ignore-seen --contact "you@example.com"
open ga_swipe/out/latest.html
```

主なオプション：`--days N`（取得日数）、`--title-only`（タイトルのみ翻訳）、`--ignore-seen`（既出も再出力）、`--usage`（DeepL 消費量の確認）。

---

※ arXiv のメタデータ・アブストは各論文の著者に帰属。本ツールは個人的な閲覧・整理を目的とする。
