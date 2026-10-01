# studio-agents — 5つのコンテンツ制作AIエージェント

スライド・ブログ・動画を、**Claudeに話しかけるだけ**で作れるエージェント群。コマンドや型の名前を覚える必要はありません。ビジュアル版の全体ガイドは **[USER_GUIDE.html](USER_GUIDE.html)**（製造ライン図・オプション一覧・料金目安）。

---

## なにが作れるか（ユーザー向け）

```
 ゼロから作る
  テーマ・資料・URL → ① Slide（スライド）──→ ④ Slidecast ──→ 🎬 解説動画
  テーマ・資料・URL → ② Blog（ブログ記事）──→ ⑤ Blogcast ──→ 🎬 解説動画

 手持ちの資産から
  見づらいパワポ/PDF → ③ Reslide（作り替え）──→ ④ Slidecast ──→ 🎬
  手持ちパワポ/PDF ────────────────────────→ ④ Slidecast ──→ 🎬（そのまま動画化）
  自社の既存ブログURL ─────────────────────────→ ⑤ Blogcast ──→ 🎬
```

| # | エージェント | 一言でいうと | 詳しい使い方 |
|---|---|---|---|
| ① | **Slide** | テーマや資料から高品質スライド（編集可PPTX＋AI画像版） | [agents/slide/README.md](agents/slide/README.md) |
| ② | **Blog** | テーマや資料から図解付きブログ（WordPress貼り付け可HTML） | [agents/blog/README.md](agents/blog/README.md) |
| ③ | **Reslide** | イマイチなスライドを中身そのままデザイン作り替え（詰め詰めページは自動分割） | [agents/reslide/README.md](agents/reslide/README.md) |
| ④ | **Slidecast** | スライド/手持ちパワポ → ナレーション付き解説動画（6声・白フェード・字幕） | [agents/slidecast/README.md](agents/slidecast/README.md) |
| ⑤ | **Blogcast** | 記事/URL → 台本の型付き解説動画（自由なイラスト画） | [agents/blogcast/README.md](agents/blogcast/README.md) |

Claude は各エージェントの README を読んでユーザーを対話誘導する。このほか、記事の量産時に「根拠・偏りの兆候・設定」を人が公開前にレビューできる形で残す補助工程 `agents/blogfactory/`（[README](agents/blogfactory/README.md)）と、その単体テスト `tests/` も含みます。

> **一部のエージェントだけを使う場合**: 上の表・全体図のうち、手元に無い連携先の工程は案内しない。`setup_check.py` は手元にあるエージェントだけを診断します。

## 全エージェント共通のルール

- **会話で完結** — 「この資料でスライド作って」「このURLを動画にして」。Claudeが必要なことだけ聞く
- **おまかせOK** — 型・画風・声など、決めていない項目は**AIが中身を見て提案**。こだわる時だけ指定
- **文量・圧縮度を選べる** — Slide/Blog/Blogcast は最初に「原文に忠実（全量）／要点にまとめる／短く」を選べる（`--preset`）。**全文採用が基本**で、長い元ネタを勝手に端折らない
- **素材の権利** — 取り込む資料・URL・記事は、自分が権利を持つもの、または利用許諾のあるものに限ります。第三者の記事・ニュースは、要約と出典明記の範囲で扱ってください
- **課金は必ず事前承認** — AI画像・音声合成は「◯枚で約◯円。進めますか？」→ OK後に実行。**承認前は1円も使わない**
- **画風の保存・使い回し** — 気に入った画風は `--save-style 名前` で保存 → 「前と同じテイストで」で再現。保存庫は共通の `styles.json`（各agentの `--theme`/`--art` に保存名を渡せる。直近使用分は `last-<agent>` に自動保存）
- **作り直しに強い** — ナレ・絵コンテ・画像は保存され、再ビルドは差分だけ課金（例: 声だけ変える＝数円）

## 料金の目安（概算）

| やること | 目安 |
|---|---|
| スライド(テンプレ版)・ブログ本文・作り替えの抽出・ナレ生成 | **追加料金なし**（手元の Claude/Codex CLI を使用） |
| スライドのAI画像版（6枚） | 約8円 |
| ブログの図解（8枚） | 約10円 |
| スライド→動画（音声のみ） | 約1.5〜3.5円 |
| 記事→動画（画像8枚＋音声） | 約20円（声違い再ビルドは約3.4円） |
| 行政資料風パワポ数枚→十数枚に作り替え | 約20円 |

## セットアップ

初回は「セットアップ確認して」→ Claude が [SETUP.md](SETUP.md) に沿って `setup_check.py` で診断し、足りないものを確認しながら導入する。

- **軽量（①②）**: Claude CLI または Codex CLI ＋ Playwright（手元でログイン済みの CLI で完結。AI画像を使う時だけ OPENAI_API_KEY）。個人のローカル利用を想定しています。他者へサービスとして提供する場合は、各社の規約に沿って API キー方式に切り替えてください
- **動画（④⑤）**: ＋ ffmpeg / uv / 別途用意する動画バックエンド/ ELEVENLABS_API_KEY（⑤は OPENAI_API_KEY も必須）
- **取込（③・④のpptxモード）**: ＋ LibreOffice / PyMuPDF

## 構成（開発者向け）

```
studio-agents/
├ README.md / SETUP.md / setup_check.py / styles.json(画風保存庫)
├ USER_GUIDE.html(ユーザー向け全体ガイド)
├ core/ style_store.py(画風の保存・使い回し) / backend_path.py(動画バックエンドの場所解決) / article_io.py
├ agents/{slide,blog,reslide,slidecast,blogcast}/ … cli.py + README.md(Claude運用ガイド)
├ agents/blogfactory/ … レビュー可能な記事量産工程(policy 解決 → 生成 → 来歴ゲート → 監査 → 人の承認 → 公開)
└ tests/ … blogfactory の単体テスト(generators / policy_resolver / provenance_gate / publisher / runner)
```

- 実装は兄弟リポジトリ `slide-studio`（deck生成・描画・ゲート）と、本リポジトリに含まれない、別途用意する動画バックエンド（動画合成・台本の型・画像生成）の資産を import / サブプロセス呼び出しで流用する。`studio-agents/.env` の `VIDEO_BACKEND_DIR`（非空）は、プロセス環境変数が未設定のとき最優先で使う。未指定なら、`studio-agents` の隣の `video-backend/backend` を探す
- エージェント間の継ぎ手はファイル（`deck.json` / `article.json` / `storyboard.json`）
- 課金は全経路「概算 → `--approved`」の2段（恒久ルール）

## 既知の制約

- 承認済みビルドの途中失敗はリトライで再課金の可能性（生成物の回収は成功時のみ）
- スライドの中身を変えたら Slidecast のナレは `--regen` で作り直す（旧ナレを再利用するため）
- 動画系（④⑤）は依存が重い。動画バックエンドは利用者が別途用意する(本リポジトリには含まれない)

## 閉包: 同梱していない依存（公開版の注意）

本リポジトリは単体では完結しません。`ast` と import 文でリポジトリ内・標準ライブラリ・`requirements.txt` のいずれにも無い import を調べた結果は次のとおりです（合計 19 名 + 外部バックエンド 1 件）。

**他の公開リポジトリに依存（18 名）: `slide-studio`**
`agents/*/cli.py` と `blog_core.py` は、兄弟ディレクトリ `slide-studio` の `engine/` と `api/` を `sys.path` に追加して import します。`studio-agents` と `slide-studio` は同じ親ディレクトリに並べて置いてください。対象は `api`（パッケージ名）, `audience`, `build`, `copy_gate`, `design_gate`, `factcheck`, `gap_check`, `ingest`, `llm`, `ocr_gate`, `organize`, `recommend`, `render_ai`, `store`, `structure_gate`, `structures`, `themes`, `video_el`。

**どの公開リポジトリにも含まれないもの（外部の動画バックエンド・非同梱）**
- `formats`（`formats.seminar.script_styles`: 台本の型カタログ。Blogcast が使用）
- `api.slide_import`（`ensure_pdf` / `rasterize` / `narrate`。Slidecast の PPTX 取り込みモードが使用）
- 動画合成の実行体（`slide-studio` の `video_el` がサブプロセスで呼ぶ）

バックエンドが無い環境では Slidecast / Blogcast の動画ビルドは動きません（Slide / Blog / Reslide の本文生成には影響しません）。場所は `VIDEO_BACKEND_DIR` で指定します。

**合成スタブ**: `render_ai` が使う `openai_image_config` は `slide-studio` 側の合成スタブ（モデル名と quality の定数のみ）です。本リポジトリ自身にスタブはありません。

**外部ツール・サービス（鍵は環境変数名のみ）**
- Python 依存は `requirements.txt` を参照（`ruamel.yaml`, `PyYAML`, `jsonschema`, `playwright`, `Pillow`, `PyMuPDF`, `python-pptx`, `python-docx` など）
- `OPENAI_API_KEY`（AI 画像）、`ELEVENLABS_API_KEY`（音声合成）。いずれも使うときだけ必要です
- LLM は Claude CLI または Codex CLI（`STUDIO_LLM_BACKEND` で選択）。従量課金の Anthropic API は、`ANTHROPIC_API_KEY` に加えて `STUDIO_ALLOW_PAID_ANTHROPIC_API` で明示許可した場合だけ使われます（`slide-studio` 側の実装）
- PPTX / PDF の取り込みには LibreOffice（`SOFFICE_BIN` で場所を指定可能）、動画系には ffmpeg / uv が必要です
- 第三者のドキュメント・URL・音声合成サービスを使う処理は、それぞれの利用規約と権利者の許諾の範囲で利用してください
- VOICEVOX の音声を使った動画を公開する場合は、使った話者の利用規約に沿って「VOICEVOX:<話者名>」などのクレジット表記を説明欄などに入れてください（自動では付きません）

テストは blogfactory 系のみ（`tests/`）です。WordPress 投稿モジュールに依存するテストと、実記事コーパスを読む回帰テストは公開版から除外しています。`styles.json` は空の保存庫（`{"entries": []}`）です。

## ライセンス

All rights reserved. 閲覧目的で公開しています。詳細は [LICENSE](LICENSE) を参照。
