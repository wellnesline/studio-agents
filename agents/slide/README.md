# Slide Agent — 使い方（Claude 運用ガイド）

ソース（資料 / URL / メモ）から高品質スライドを作る CLI エージェント。中身は slide-studio の機能を丸ごと移植（画風・OpenAI画像・文字照合・デザイン改善・型切替）。

**このREADMEは Claude Code が読んで、ユーザーを対話誘導するための運用指示です。** ユーザーは CLI の引数を覚える必要はありません。「スライド作って」と言われたら、Claude がこの手順でソースと変数を引き出し、CLI を叩きます。

---

## 基本方針（Claude はこう動く）

- いきなり全項目を質問攻めにしない。**決まっていない変数は AI 推定/推奨に任せてよい**（無理に全部聞かない）。
- 課金（gpt-image）は**必ず概算を伝えて承認を取ってから**実行する。承認前は 1 円も出さない。
- 各コマンドの stdout は JSON（機械可読）、`…` の進捗は stderr。

## 圧縮度プリセット（最初にこれを聞く・主動線）

「**全部そのまま／要点にまとめる／短く、どれにします？**」と一度聞く（`--preset`）。**全文採用が基本**で、端折らず「1枚に複数節を統合」して枚数を絞る。

| preset | スライド数 | カバー | 使いどころ |
|---|---|---|---|
| **faithful**（原文に忠実） | 上限なし（内容量に追従） | 全論点 | 資料を漏れなく |
| **condensed**（要点にまとめる） | 20枚以内 | 要点 | ほどよい長さ |
| **essence**（エッセンス） | 10枚以内 | 核 | 短く |

- `--max-slides N` で上限を微調整できる。preset 未指定は従来の単発生成（最大9枚・長文は大きく端折る）。
- faithful は長文だと数十枚になる（AI画像版で描くなら枚数×約1〜2円）。テンプレPPT版は課金ゼロ。

## 誘導フロー

0. **入口ガイド（ユーザーが迷っていたら、まずここから）** — ユーザーは型やワークフローを知らない前提。Claudeから「**元ネタ（資料/URL/メモ）をくれれば、資料の型（提案/報告/解説/比較…）と画風はこちらで提案します。おまかせでOK、『投資家向けに固く』のような希望があれば合わせます**」と口火を切る。**選択肢を全部並べて選ばせない**。型・画風はAIが提案して「これでいい？」と確認する形で体験させる。
1. **ソースを受け取る** — ファイル / URL / 貼り付けテキスト（複数・混在可）。「元ネタは何ですか？」
2. **宛先を軽く確認（任意）** — 「誰向け・何のため」が明確なら投入、曖昧なら AI 推定に任せる。聞き倒さない。
3. **提案を見せる（任意）** — `recommend` で画風×構成を理由付きで提案 → 一言確認。急ぐなら飛ばして build。
4. **生成（課金ゼロ）** — `build` でテンプレ版を作り、スライド画像を見せる。
5. **調整** — 画風変更（build/render の `--theme`）/ デザイン改善（`improve`）/ 1枚だけ型変更（`relayout`）。
6. **AI画像版まで仕上げる（標準の締め）** — テンプレ版で止めず、**見栄えの良いAI画像版（gpt-image）まで作るのが基本**。課金なので「**◯枚で約◯円**」と概算を出して承認だけ取る（承認前は生成しない）。急ぎ・不要ならテンプレ版（課金ゼロ・編集可PPTX）で止めても可。
7. **仕上げ** — AI画像なら `verify`（文字照合）→ ずれていれば `fix`（指摘だけ描き直し・課金ゲート）。

## コマンド早見表

すべて `python studio-agents/agents/slide/cli.py <cmd> …`（リポジトリ直下から）。

| コマンド | 用途 | 課金 |
|---|---|---|
| `options` | 投入できる変数の選択肢一覧（宛先/構成/画風）| ゼロ |
| `recommend` | ソースから画風×構成をAI提案（描画しない）| ゼロ |
| `build` | ソース→構成→チェック→テンプレ描画→書き出し（`--no-render` で deck.json のみ / `--save-style NAME` で画風保存）| ゼロ |
| `render --deck … --engine ppt\|ai [--mode full\|hybrid]` | deck.json を再描画（ai full=全面委譲・**承認すると全枚を必ず生成し直す**、hybrid=AI背景+正確テキスト）| ppt=ゼロ / ai=課金ゲート |
| `themes` | 画風一覧 | ゼロ |
| `improve --deck …` | デザイン自己改善 | ゼロ |
| `relayout --deck … --slide sXX --layout …` | 1枚を別レイアウトへ | ゼロ |
| `verify --deck …` | AI画像の文字照合（未描画スライドも指摘）| ゼロ |
| `fix --deck … [--approved]` | 指摘スライドだけ gpt-image で描き直し | 課金ゲート |

## 同じテイストで作る（画風の保存・使い回し）

`build --save-style 名前` で今回の画風を保存すると、次回から `--theme 名前` で同じテイストにできる（直近は `last-slide` に自動保存）。保存庫は 5エージェント共通の `studio-agents/styles.json`。「前と同じテイストで」と言われたらここを見る。

## 変数の投入（build / recommend）

`slide options` で選択肢を確認できる。**未指定はAIが推定/推奨、指定すると人の指定を優先**（宛先は候補外の自由記述も可）。

- ソース: `--file` / `--url` / `--text`（いずれも複数可・混在可）/ stdin
- 宛先: `--author`（誰の資料か）/ `--reader`（誰に見せるか）/ `--purpose`（目的）/ `--tone`（トーン）
- 構成: `--structure`（consulting / service-intro / case-study / pitch / education / report）
- 画風: `--theme`（executive-mincho / midnight / coral-editorial / warm-serif / modern-sans / indigo-pro）

## 使用例（Claude が実際に叩くコマンド）

```bash
# おまかせ（宛先も画風も構成もAI推定・課金ゼロ）
python studio-agents/agents/slide/cli.py build --url "https://…" --out ./out

# 投資家向けピッチを指定（宛先・目的・構成を人が投入）
python studio-agents/agents/slide/cli.py build \
  --file 事業計画.pdf --reader 投資家 --purpose 資金調達 --structure pitch \
  --theme midnight --out ./out

# 複数ソースを結合
python studio-agents/agents/slide/cli.py build --file A.pdf --url "https://…" --text "補足メモ" --out ./out

# まず提案だけ見る
python studio-agents/agents/slide/cli.py recommend --file 資料.pdf

# テンプレ版を確認後、AI画像版に（概算を見せて承認を取ってから --approved）
python studio-agents/agents/slide/cli.py render --deck ./out/deck.json --engine ai            # 概算のみ
python studio-agents/agents/slide/cli.py render --deck ./out/deck.json --engine ai --approved  # 承認後に描画
```

## 課金ルール（厳守）

- gpt-image（`--engine ai` / `fix`）は「**概算 → ユーザー承認 → `--approved` で実行**」の2段。
- 承認前は gpt-image を一切叩かない（`needs_approval: true` と概算だけ返す）。
- 実コストは生成後に usage 実測で返る（`cost.jpy`）。目安: low 品質で約 1〜2 円/枚。

## 出力物

`--out` ディレクトリに:
- **`deck.json`** — 受け渡しの箱（構成データ＋宛先＋ソース）。後工程（動画化 = Slidecast エージェント）がこれを食う。
- `sXX.png` — 各スライド画像
- `deck.pptx` — テンプレ版は編集可 / AI画像版は画像貼り
- `deck.pdf` / `deck_png.zip`

動画化はこのエージェントの範囲外（別エージェント Slidecast が `deck.json` を入力に取る）。
