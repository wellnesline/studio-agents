# Slidecast Agent — 使い方（Claude 運用ガイド）

スライド（Slideの`deck.json`）や**手持ちのパワポ/PDF**を、ナレーション付きの解説動画にするエージェント。音声＝ElevenLabs（公開声6種・v3）、映像＝**静止スライド＋フェード切替＋字幕**（動画バックエンドの合成を流用＝音声・字幕は元に忠実）。

**このREADMEは Claude Code が読んで、ユーザーを対話誘導するための運用指示です。** ユーザーは CLI 引数を覚える必要はありません。

---

## 全体フロー（Claude はこう誘導する）

0. **入口ガイド（迷っていたらここから）** — 「スライドやパワポを**解説動画**にできます。**①Slideで作ったスライド（deck.json）** か **②手持ちのパワポ/PDF**、どちらかをください。声はこちらで提案します（おまかせOK）」
1. **入力を受け取る** — deck.json（Slideの出力）か PPTX/PDF。
2. **声を選ぶ** — 6声から。決まっていなければ、環境変数 `DEFAULT_VOICE_PRESET` の設定値で進める（未設定なら声の指定を促す）。
3. **ナレ生成（自動・固定）→ 概算 → 承認** — ナレをclaudeで生成（課金ゼロ）し、`narration.json`に固定。ElevenLabsの合成は課金なので「**約◯円**」と概算を出して承認を取る。
4. **動画ビルド** — 静止スライド＋フェード切替＋字幕で `video.mp4`。
5. **手直し** — ナレを作り直したい/声を変えたい時だけ `--regen`。

## コマンド早見表

`python studio-agents/agents/slidecast/cli.py <cmd> …`（リポジトリ直下から）。

| コマンド | 用途 | 課金 |
|---|---|---|
| `voices` | 音声プリセット一覧 | ゼロ |
| `build --deck deck.json --voice <id> [--approved] [--regen]` | Slideのdeck.jsonから動画 | ナレ=ゼロ / EL音声=課金ゲート |
| `pptx --file 資料.pptx --voice <id> --out <dir> [--approved] [--regen]` | **手持ちパワポ/PDF**から動画 | 同上（PPTX変換にLibreOffice）|

## 音声（6公開声・別環境でも再現可）

`voices` で一覧。全て **EL公開ライブラリの声**なので、別のElevenLabsアカウントでも同じ声が出る（自作声と違い再現できる）。

- 男性：`nao`（明るい説明調）/ `shohei`（落ち着いたナレ）/ `ren`（なめらか）
- 女性：`miki`（明るい）/ `maya`（クリア）/ `kazuha`（温かい）

## ナレーション固定

1動画＝1つの `narration.json`。**一度生成したら再ビルドで再利用**（同じ動画は毎回同じ内容）。作り直したい時だけ `--regen`。
※注意: `slide improve/relayout` 等で**スライドの中身を変えた後**は、旧ナレが残るので `--regen` を付けること。

## 課金（ElevenLabs）

音声合成はEL従量課金。`build`/`pptx` は**まず概算を出し、`--approved` で実行**（承認前は合成しない）。実額はEL側実測（目安：ナレ数百字で1〜2円）。

## 映像（静止＋白フェード＋字幕）

**blob段階表示なし**。静止スライド＋**白フェード切替（スライド部分のみ・帯と字幕はフェードしない）**＋字幕帯（黒帯・白文字・音声同期）。**音声・字幕は 動画バックエンドの合成をそのまま流用＝元に忠実**（キャリア句・幻音トリム・difflib文字整列・無音snap）。
- 実装：bridge が `reveal_mode="static"` / `scene_transition="fade"` を指定（本体はadditive・既存動作は無影響）。

## 出力物

`--out`（deck.json / PPTX の隣）に:
- **`video.mp4`** — 完成動画（1920×1080）
- `narration.json` — 固定ナレ（再利用元）
- `el_job.json` — ビルド指示（デバッグ用）

## 依存（このエージェントは重め）

**別途用意する動画バックエンド＋ ffmpeg ＋ ELEVENLABS_API_KEY**（PPTX取込時のみ LibreOffice ＋ PyMuPDF）。`studio-agents/setup_check.py` で確認できる。動画バックエンドは利用者が別途用意する（軽量2本の Slide/Blog と違い、ここは重い）。
