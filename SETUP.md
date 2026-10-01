# studio-agents セットアップ（Claude 運用ガイド）

各エージェントを動かす前の環境準備。**Claude Code がこれを読んで、環境を診断し、足りないものをユーザーに確認しながら導入します。** ユーザーはコマンドを覚えなくてよい。

---

## まず診断する

```bash
python studio-agents/setup_check.py
```

確認のみ・外部 API への課金なし・秘密は表示しない。各項目の「有る/無い・どのエージェント用か・入れ方」を JSON で返す。`slide_ready: true` なら Slide は今すぐ動く。

## Claude の動き方

1. `setup_check.py` を走らせる。
2. `missing` を見て、足りないものを**1つずつ「入れますか？」と確認 → 承認後にインストール**。パッケージマネージャでの導入や `pip install` は環境を変えるので、必ず事前に確認する。
3. APIキー（OpenAI / ElevenLabs）が無ければ、`studio-agents/.env` への登録を案内する。**値は `.env` にだけ書き、README・ソース・ログには残さない。**
4. 再診断して緑になったら完了。使いたいエージェントに必要な項目だけ揃っていればよい（Slide だけなら動画系は不要）。

## 必要なもの（役割別）

### 全エージェント共通【必須】
- **Claude CLI または Codex CLI** — 構成生成・各種チェックに最低1つ必要。`STUDIO_LLM_BACKEND=claude|codex` で優先側を選べ、失敗時は利用可能な反対側へ1回だけ退避する。

### Slide エージェント【必須】
- **Playwright + Chromium** — スライド PNG 描画。`pip install playwright` → `playwright install chromium`
- **和文フォント（Noto Serif/Sans JP）** — 文字表示（PNG と PPTX の書体一致）。`python slide-studio/assets/fonts/_fetch_fonts.py`

### 取込（あると対応形式が増える）
- **PyMuPDF**（PDF）/ **python-pptx**（PPTX）/ **python-docx**（DOCX）/ **httpx**（URL）

### gpt-image（課金・承認制）
- **OPENAI_API_KEY** ＋ **openai** パッケージ — Slide AI画像 / Blog図解 / **Reslide（必須）** / **Blogcast（必須）**

### 動画（Slidecast / Blogcast）
- **ffmpeg** — 動画合成。OS のパッケージマネージャで導入する
- **uv** — 別途用意する動画バックエンドの起動（動画ビルドの必須依存）。公式のインストール手順に従って導入する
- **別途用意する動画バックエンド** — 動画ビルドの実行体。公開リポジトリには含まれないため、利用者が別途用意する。`slide-studio/` の隣の `video-backend/backend` に置くか、別の場所に置く場合は `studio-agents/.env` に `VIDEO_BACKEND_DIR=/path/to/backend` を設定する
- **LibreOffice（soffice）** — PPTX→PDF変換（Slidecast pptxモード / Reslide の取込）。OS のパッケージマネージャか公式サイトから導入する
- **ELEVENLABS_API_KEY** — 動画の音声

## APIキーの登録

`studio-agents/.env` に記載（各エージェントの起動時に、未設定の環境変数だけを補完する）:

```
OPENAI_API_KEY=sk-...
ELEVENLABS_API_KEY=...
```

- **キーは `.env` だけに置く。** ソース・README・ログ・チャットに値を書かない。
- `setup_check.py` はキーの「設定あり/なし」だけを返し、値は表示しない。

### キーの取得先と、契約で気をつける点

キーは各サービスで自分で発行する。**どちらも「キーがある」だけでは足りず、契約状態によって途中で止まる**。

**OpenAI**（AI画像。Reslide / Blogcast は必須）

- 発行: <https://platform.openai.com/api-keys>
- **前払い式**。残高が 0 だと画像生成の段階で止まる（キーは有効でも失敗する）。
  先に少額でもチャージしておく。
- 累計課金額で 1 分あたりの生成枚数が変わる。始めたては遅く、枚数が多いと待ち時間が伸びる。
  ツール側がペースを調整するので失敗はしないが、初回は時間がかかると思っておく。

**ElevenLabs**（動画の音声。Slidecast / Blogcast で必要）

- 発行: <https://elevenlabs.io/>（サインイン後の Profile / API Keys）
- **無料枠は月あたりの文字数が少なく、動画制作には実質足りない。** 有料プランを前提にする。
- プランによって**同時リクエスト数**の上限が変わる。上限を超えるとビルドが失敗する。
  ツールはプランを自動判定して同時数を合わせるので通常は設定不要。
- 上限値はサービス側で変わり得るので、契約前に料金ページで確認する。

**Claude / Codex CLI**（文章生成）

- こちらは手元でログイン済みの CLI の認証で動き、API キーは使わない。従量課金の API へは自動で切り替えない。個人のローカル利用を想定しており、他者へサービスとして提供する場合は、各社の規約に沿って API キー方式を使う。

## 注意

- インストール（パッケージマネージャでの導入など）は環境を変えるので、**実行前に必ずユーザーに確認**する。
- 使うエージェントに応じて必要分だけ導入すればよい。**Slide だけなら**動画系（ffmpeg / LibreOffice / node / ELキー）は不要。
