#!/usr/bin/env python3
"""studio-agents 環境診断。Claude が SETUP.md と併せて読み、足りないものを潰すための確認スクリプト。
何もインストールしない(確認のみ・課金ゼロ)。有る/無い・どのエージェント用か・入れ方 を JSON で返す。

秘密は出さない: APIキーは「設定あり/なし」だけ返し、値は表示しない。
"""
from __future__ import annotations
import glob
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]   # リポジトリ直下
STUDIO_AGENTS_ROOT = pathlib.Path(__file__).resolve().parent
SLIDE_STUDIO = ROOT / "slide-studio"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "core"))
from backend_path import resolve_backend_dir


def has_cmd(name: str) -> bool:
    return shutil.which(name) is not None


# 診断する Python パッケージ。 import 名で書く (表示名は add() 側)。
_PROBE_MODULES = [
    "playwright", "yaml", "fontTools", "PIL", "fitz", "pptx", "docx",
    "httpx", "openai", "markdown", "jsonschema",
]


def _probe_imports(names: list[str]) -> dict[str, bool]:
    """別プロセスで実際に import して可否を返す。

    find_spec() は「見つかるか」しか見ない。 拡張モジュールを持つパッケージ
    (Pillow の _imaging、 pydantic の _pydantic_core など) は、 見つかっても
    DLL の読込に失敗して import できないことがある。 診断が「有り」と言うのに
    実行が落ちる状態を作らないため、 実 import で確かめる。

    別プロセスにするのは、 import 失敗やクラッシュが診断自体を巻き込まないため。
    1 回のプロセスでまとめて試すので、 パッケージ数が増えても遅くならない。
    """
    # import しただけで stdout に書くパッケージがある (例: fitz の deprecation warning)。
    # stdout 全体を JSON として読むと、そういう1行で診断が丸ごと失敗し、
    # 「全部不足」という実態と正反対の結果になる。目印を付けて該当行だけ拾う。
    marker = "__STUDIO_PROBE__"
    code = (
        "import importlib, json, sys\n"
        "out = {}\n"
        "for n in json.loads(sys.argv[1]):\n"
        "    try:\n"
        "        importlib.import_module(n)\n"
        "        out[n] = True\n"
        "    except BaseException:\n"
        "        out[n] = False\n"
        f"print({marker!r} + json.dumps(out))\n"
    )
    try:
        r = subprocess.run([sys.executable, "-c", code, json.dumps(names)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        for line in (r.stdout or "").splitlines():
            if line.startswith(marker):
                return json.loads(line[len(marker):])
    except Exception:
        pass
    # ここに来たら判定できていない。 有ると誤報するより、 不足として出して人に確認させる。
    return {n: False for n in names}


_IMPORTABLE = _probe_imports(_PROBE_MODULES)


def has_pkg(name: str) -> bool:
    return bool(_IMPORTABLE.get(name, False))


def pip_hint(pkg: str) -> str:
    """この環境で実際に効く導入コマンド。

    uv が管理する Python では `pip install` が externally-managed で弾かれるため、
    uv 経由を先に案内する。
    """
    return f"uv pip install {pkg}   (uv を使っていなければ {sys.executable} -m pip install {pkg})"


def env_key(name: str) -> bool:
    """環境変数または診断対象の .env に非空の KEY があるか。値は返さない。"""
    if os.environ.get(name, "").strip():
        return True
    for envf in (STUDIO_AGENTS_ROOT / ".env", ROOT / ".env"):
        if envf.exists():
            for line in envf.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith(name + "=") and line.split("=", 1)[1].strip():
                    return True
    return False


def chromium_present() -> bool:
    """Playwright の Chromium が使える状態か。

    ディレクトリの有無だけだと、 途中で失敗した導入の残骸でも「有り」になる。
    Playwright 自身に実行ファイルの場所を尋ね (OS ごとの配置を決め打ちしない)、
    その実体が存在するかを確かめる。 実際に起動するかは STUDIO_SETUP_DEEP=1 の
    ときだけ試す (数秒かかるので既定では見ない)。
    """
    code = ("from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as p:\n"
            "    print('__CHROMIUM_PATH__' + p.chromium.executable_path)\n")
    exe = ""
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        for line in (r.stdout or "").splitlines():
            if line.startswith("__CHROMIUM_PATH__"):
                exe = line[len("__CHROMIUM_PATH__"):].strip()
    except Exception:
        return False
    if not exe or not pathlib.Path(exe).exists():
        return False

    if os.environ.get("STUDIO_SETUP_DEEP", "").strip().lower() not in ("1", "true", "yes", "on"):
        return True
    # 深い確認: 実際に起動できるところまで見る
    code = ("from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as p:\n"
            "    b = p.chromium.launch()\n"
            "    b.close()\n")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=180)
        return r.returncode == 0
    except Exception:
        return False


def libreoffice_present() -> bool:
    """LibreOffice を探す。 agents/reslide/_ingest.py の _soffice() と同じ順序を保つこと。

    Windows のインストーラは PATH に soffice を登録しないため、 PATH だけを
    見ていると、 インストール済みでも「無し」と報告してしまう。
    """
    override = os.environ.get("SOFFICE_BIN", "").strip()
    if override:
        return pathlib.Path(override).exists()
    if has_cmd("soffice") or has_cmd("libreoffice"):
        return True
    candidates: list[pathlib.Path] = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(var)
        if base:
            candidates.append(pathlib.Path(base) / "LibreOffice" / "program" / "soffice.exe")
    return any(p.exists() for p in candidates)


checks: list[dict] = []


def add(name, ok, needed_by, install, detail=""):
    checks.append({"name": name, "ok": bool(ok), "needed_by": needed_by,
                   "install": install, "detail": detail})


# --- 全エージェント共通 ---
_claude_cli = has_cmd("claude")
_codex_cli = has_cmd("codex")
add("LLM CLI (Claude または Codex)", _claude_cli or _codex_cli, "全エージェント【必須】",
    "Claude Code または Codex CLI をインストールしてログイン")
add("claude CLI", _claude_cli, "LLM選択肢（任意）",
    "https://claude.ai/download か  npm i -g @anthropic-ai/claude-code")
add("codex CLI", _codex_cli, "LLM選択肢（任意）",
    "Codex CLI をインストールしてログイン")

# --- Slide 描画に必須 ---
add("Playwright (pythonパッケージ)", has_pkg("playwright"), "Slide描画【必須】",
    pip_hint("playwright"))
add("Playwright Chromium", chromium_present(), "Slide描画【必須】",
    f"{sys.executable} -m playwright install chromium",
    "STUDIO_SETUP_DEEP=1 で実際の起動まで確認する")
# Bold も描画で使う (build.py が Noto Sans JP/B・Noto Serif JP/B をサブセット化する)。
# Regular だけ見ていると、Bold 欠けが描画時まで分からない。
_FONT_FILES = ("NotoSerifJP-Regular.ttf", "NotoSerifJP-Bold.ttf",
               "NotoSansJP-Regular.ttf", "NotoSansJP-Bold.ttf")
_fonts_missing = [f for f in _FONT_FILES if not (SLIDE_STUDIO / "assets/fonts" / f).exists()]
add("和文フォント (Noto Serif/Sans JP・Regular+Bold)", not _fonts_missing, "Slide描画【必須】",
    "python slide-studio/assets/fonts/_fetch_fonts.py",
    ("不足: " + ", ".join(_fonts_missing)) if _fonts_missing else "")
add("PyYAML (yaml)", has_pkg("yaml"), "全エージェント【必須】", pip_hint("pyyaml"))
add("fonttools (fontTools)", has_pkg("fontTools"), "Slide描画（テンプレ）【必須】",
    pip_hint("fonttools"))
add("Pillow (PIL)", has_pkg("PIL"), "Slide標準PDF出力【必須】 / Reslide取込【必須】",
    pip_hint("pillow"))

# --- 取込(あると対応形式が増える) ---
add("PyMuPDF (fitz)", has_pkg("fitz"), "PDF取込", pip_hint("pymupdf"))
add("python-pptx", has_pkg("pptx"), "PPTX取込 / PPTX書き出し", pip_hint("python-pptx"))
add("python-docx", has_pkg("docx"), "DOCX取込", pip_hint("python-docx"))
add("httpx", has_pkg("httpx"), "URL取込", pip_hint("httpx"))

# --- 記事・計画の処理 (診断から漏れていた) ---
#   markdown は core/article_io.py が、jsonschema は blogfactory/plan_schema.py が
#   import する。どちらも診断対象になく、不足していても「準備OK」と表示されていた。
add("markdown", has_pkg("markdown"), "Blog記事のHTML化【必須】 / 全エージェント",
    pip_hint("markdown"))
add("jsonschema", has_pkg("jsonschema"), "Blogfactory の計画検証【必須】",
    pip_hint("jsonschema"))

# --- gpt-image(課金・承認制) ---
add("OPENAI_API_KEY", env_key("OPENAI_API_KEY"),
    "Slide AI画像 / Blog図解 / Reslide作り替え / Blogcast画像(gpt-image)【③⑤は必須】",
    "studio-agents/.env に  OPENAI_API_KEY=sk-...  を追記")
add("openai (pythonパッケージ)", has_pkg("openai"), "AI画像", pip_hint("openai"))

# --- 動画(Slidecast / Blogcast) ---
add("ffmpeg", has_cmd("ffmpeg"), "動画(Slidecast / Blogcast)【必須】",
    "OSのパッケージマネージャで ffmpeg をインストール")
add("uv", has_cmd("uv"), "動画ビルド(別途用意する動画バックエンドの起動に必須・Slidecast / Blogcast)",
    "https://docs.astral.sh/uv/getting-started/installation/")
add("別途用意する動画バックエンド", (resolve_backend_dir() / "tools/slidestudio_build.py").exists(),
    "動画ビルドの実行体(Slidecast / Blogcast)", "VIDEO_BACKEND_DIR または video-backend/backend に、別途用意した動画バックエンドが必要")
add("LibreOffice (soffice)", libreoffice_present(), "PPTX→PDF変換(Slidecast pptxモード / Reslide取込)",
    "OSのパッケージマネージャまたは https://www.libreoffice.org/download/ からインストール")
add("ELEVENLABS_API_KEY", env_key("ELEVENLABS_API_KEY"), "動画の音声(Slidecast / Blogcast)",
    "studio-agents/.env に  ELEVENLABS_API_KEY=...  を追記")

# --- 任意 ---
add("ANTHROPIC_API_KEY (任意)", env_key("ANTHROPIC_API_KEY"),
    "Claude CLI が無い時の同一provider内フォールバック(任意)", "studio-agents/.env に追記(任意)")


# --- 手元にあるエージェントだけを診断対象にする ---
#   agents/ 配下の存在で判定(一部のエージェントだけを置いた構成にも対応)。
_AGENTS_DIR = pathlib.Path(__file__).resolve().parent / "agents"
present = {p.name for p in _AGENTS_DIR.iterdir() if (p / "cli.py").exists()} if _AGENTS_DIR.exists() else set()
_KEYWORDS = {  # 各チェックがどのエージェントに関係するか(needed_by 文字列で判定・部分一致衝突に注意)
    "slide": ["Slide描画（テンプレ）", "Slide描画", "Slide標準PDF出力", "Slide AI画像", "全エージェント", "URL取込", "PDF取込", "PPTX取込", "DOCX取込", "AI画像"],
    "blog": ["Blog図解", "Blog記事のHTML化", "全エージェント", "URL取込", "PDF取込", "PPTX取込", "DOCX取込", "AI画像"],
    "reslide": ["Reslide取込", "Reslide", "全エージェント", "PDF取込", "PPTX→PDF変換", "AI画像"],
    "slidecast": ["Slidecast", "全エージェント", "PDF取込"],
    "blogcast": ["Blogcast", "全エージェント", "AI画像"],
    # blogfactory が抜けていた。blogfactory だけを置いた構成では、
    # present に一致するキーワードが無く、ほぼ全チェックが対象外へ落ちていた。
    "blogfactory": ["Blogfactory", "Blog記事のHTML化", "Blog図解", "全エージェント", "URL取込"],
}
def _relevant(c) -> bool:
    nb = c["needed_by"]
    return any(any(k in nb for k in _KEYWORDS.get(a, [])) for a in present) or "任意" in nb
checks = [c for c in checks if _relevant(c)]

essential = [c for c in checks if "【必須】" in c["needed_by"]]
missing = [c for c in checks if not c["ok"]]
print(json.dumps({
    "root": str(ROOT),
    "slide_ready": all(c["ok"] for c in essential),   # Slide が今すぐ動くか
    "checks": checks,
    "missing": [{"name": c["name"], "needed_by": c["needed_by"], "install": c["install"]} for c in missing],
}, ensure_ascii=False, indent=2))
