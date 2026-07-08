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
   - アブストは上下スクロールで読む。「本文を翻訳」をタップするとその場で日本語訳を取得できる（詳細は後述）
   - 中央の「arXiv」ボタンで abs を開く（Safari のページ翻訳で日本語アブストを読める）
   - 「元に戻す」で直前のスワイプを取り消し
   - 右上のリストから保存一覧を表示し、Markdown / BibTeX でコピー
   - ヘッダー付近に当月の DeepL 使用量を控えめに表示（80%を超えると警告色）
3. 既読・保存・アブスト翻訳キャッシュはその端末のブラウザに記録される（GitHub には送られない）。データを更新したいときはページを再読み込みするだけ。

## 構成ファイル

| ファイル | 役割 |
|---|---|
| `index.html` | スワイプUI本体（単一ファイル・依存なし）。`latest.json` / `usage.json` を読み込み、`worker/` のエンドポイントを叩いて動作 |
| `ga_fetch.py` | arXiv 取得＋タイトル日本語訳＋DeepL使用量取得のスクリプト |
| `.github/workflows/update.yml` | 毎日 `ga_fetch.py` を実行して `latest.json` / `usage.json` を自動更新 |
| `latest.json` | 直近の新着データ（Actions が自動生成。手で編集しない） |
| `usage.json` | 当月の DeepL 使用文字数・上限（Actions が自動生成） |
| `ga_swipe/cache.json` | タイトル翻訳キャッシュ（再翻訳を避けて無料枠を節約） |
| `worker/` | 本文のワンタップ翻訳を中継する Cloudflare Worker（DeepLキーはここにのみ置く） |

## 仕組み（毎日の自動更新）

```
GitHub Actions(毎日) ──→ arXiv API 取得 ──→ DeepL でタイトル翻訳 ──→ latest.json / usage.json をコミット
                                                                         │
                                              GitHub Pages で配信 ◀──────┘
                                                     │
                                              スマホで開く ──(本文タップ翻訳時のみ)──→ Cloudflare Worker ──→ DeepL
```

無料枠を保つ工夫：

- **タイトルのみ自動翻訳**（`--title-only`）。アブストは原文のままで、読みたいときだけ「本文を翻訳」をタップして取得する（下記）。
- **翻訳キャッシュ（cache.json）をコミット**するので、毎日訳すのは「その日新しく出たタイトル」だけ。DeepL の無料枠（月50万文字）はほぼ消費しない。
- 毎回 **直近7日ぶん** を出力するので、数日アプリを開かなくても取りこぼさない（スワイプ済みは端末側で自動除外）。
- 当月の DeepL 使用量を `usage.json` として配信し、アプリ側で見える化。無料枠に近づいていないか一目で確認できる。

## DeepL使用量の可視化

`.github/workflows/update.yml` は `ga_fetch.py --usage-out usage.json` で当月の使用文字数・上限を取得し、`latest.json` と同じタイミングでリポジトリへコミットする。DeepL APIキーはこのワークフロー内（GitHub Secrets経由）でのみ使用され、`usage.json` 自体にはキーは含まれない。アプリはヘッダー付近に「使用量: 12,345 / 500,000字（今月）」のように表示し、80%を超えると警告色になる。

## 本文のワンタップ翻訳（Cloudflare Worker）

アブスト本文は既定では原文のまま表示され、カード内の「本文を翻訳」をタップした時だけ、その場で日本語訳を取得する。取得した訳文は端末の `localStorage`（`gaswipe_absja`）にキャッシュされ、再タップや再表示のたびに課金・再翻訳はしない。

DeepLキーをスマホ側のコードに置けないため、`worker/` に Cloudflare Worker を新設し、DeepL APIキーは Worker の環境変数（secret）にのみ置く。アプリは Worker のエンドポイントを叩き、Worker が DeepL へ中継する。

- `POST /translate` … `{ text, id }` を受け取り、翻訳結果 `{ id, text_ja, cached }` を返す（本文用）。論文ID単位で Cloudflare KV（`TRANSLATE_CACHE`）に永続キャッシュするため、**同じ論文は他の端末・他のユーザーが読んでも二度翻訳しない**（Worker は git へのコミット権限を持てないため、`cache.json` の代わりに KV を「サーバー側の永続キャッシュ」として使う設計）。
- `GET /usage` … DeepLの当月使用量を返す（Actions経由の取得が難しい場合の代替。通常は使わなくてよい）。
- 簡易レート制限（`RATE_LIMIT_KV`、同一IPにつき1分あたり既定20リクエストまで）と、CORS（`ALLOWED_ORIGIN` に設定した自分の GitHub Pages ドメインのみ許可）を実装。DeepLキーはレスポンス・エラーメッセージに一切含めない。

### Workerのデプロイ手順

1. Cloudflareアカウントを作成し、`npm i -g wrangler`（または `worker/` 内で `npm install`）で Wrangler CLI を用意する。
2. `wrangler login` でログイン。
3. KV Namespace を2つ作成する。
   ```bash
   cd worker
   wrangler kv namespace create TRANSLATE_CACHE
   wrangler kv namespace create RATE_LIMIT_KV
   ```
   出力された `id` を `worker/wrangler.toml` の `kv_namespaces` にそれぞれ貼り付ける。
4. `worker/wrangler.toml` の `ALLOWED_ORIGIN` を自分の GitHub Pages のオリジン（例: `https://<ユーザー名>.github.io`）に書き換える。
5. DeepL APIキーを Worker の secret として登録する（このキーはリポジトリやアプリ側には一切書かない）。
   ```bash
   wrangler secret put DEEPL_API_KEY
   ```
6. デプロイする。
   ```bash
   wrangler deploy
   ```
   完了すると `https://ga-swipe-translate.<自分のサブドメイン>.workers.dev` のようなURLが払い出される。
7. `index.html` 内の `WORKER_URL` 定数を、6で払い出された実際のURLに書き換えてコミットする。

## セットアップ（再現用メモ）

1. このリポジトリに `index.html` と `ga_fetch.py`、`.github/workflows/update.yml`、`worker/` を置く。
2. **Settings → Secrets and variables → Actions** で2つの Secret を登録：
   - `DEEPL_API_KEY` … DeepL の API キー（無料版は末尾 `:fx`）
   - `GA_CONTACT` … arXiv へ送る連絡先メール
3. **Actions** タブで `Update arXiv digest` を一度「Run workflow」して `latest.json` / `usage.json` を生成。
4. **Settings → Pages** で Source を「Deploy from a branch」、Branch を `main` / `/(root)` にして Save。
5. リポジトリは Public（無料の Pages / Actions のため）。中身は arXiv の公開アブストのみ。
6. 本文のワンタップ翻訳を使う場合は、上記「Workerのデプロイ手順」に沿って Cloudflare Worker を別途デプロイする（Pages/Actions とは独立した作業）。

## 既知の限界

- 取得は投稿日（submittedDate）基準のため、**過去に投稿され最近 GA にクロスリストされた論文**は一部拾えない。改訂版（v2 など）は新着として再表示しない。
- 本文のワンタップ翻訳は Cloudflare Worker のデプロイが前提。未デプロイ・`WORKER_URL` 未設定の状態では「本文を翻訳」タップ時にエラーになる（タイトル翻訳・その他の機能には影響しない）。

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

## 注意：アイコン更新・ホーム画面アプリの作り直し

既読・保存のデータは、URLそのものではなく**ホーム画面の各ショートカット（standaloneのWebアプリ）ごとに別々の保存領域**を持つ。アイコン更新などでショートカットを作り直す場合、**古いショートカットをホーム画面に残したまま**新しいものを追加すれば、古い方を開けば以前のデータにアクセスできる。

一方、**古いショートカットを先に削除してしまうと、その保存領域には通常の方法でアクセスできなくなる**（Safariの通常タブで同じURLを開いても別の保存領域になり、データは復元できない）。アイコン更新時は、新しいショートカットの動作を確認できるまで古いショートカットを残しておくか、作り直す前に保存リストをMarkdown/BibTeXでコピーして控えておくこと。

なお、現在の `index.html` はMarkdown/BibTeXの**書き出し（エクスポート）のみ**対応しており、書き出した内容をアプリへ読み込み直す機能は無い。控えはあくまで手元の記録用。

## 注意：ホーム画面アプリと Safari タブはデータが別

iOS では、同じURLでも「ホーム画面に追加したWebアプリ（standalone）」と「Safariの通常タブ」は別々の保存領域（localStorage）として扱われる。そのため、片方で見た既読・保存の件数が、もう片方には反映されない（バグではなく仕様）。

確認や利用は **常にホーム画面のアイコンから開く**ことに統一し、Safariの通常タブで開いて件数を比較しない。検証のために一時的にタブで開いた場合、件数が0に見えても、ホーム画面アプリ側のデータは別途無事に保持されている。
