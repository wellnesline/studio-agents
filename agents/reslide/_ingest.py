"""PPTX/PDF → ページPNG 取込(自己完結・動画バックエンド非依存)。依存: LibreOffice(PPTX時)+PyMuPDF+Pillow。"""
from __future__ import annotations
import os
import shutil
import subprocess
from pathlib import Path

SLIDE_W, SLIDE_H = 1536, 864


class SlideImportError(Exception):
    pass


def _soffice() -> str | None:
    """LibreOffice の実行ファイルを探す。

    Windows のインストーラは PATH に soffice を登録しないため、 PATH だけを
    見ていると、 インストール済みでも「見つかりません」になる。
    別途用意する動画バックエンドの取込処理と同じく SOFFICE_BIN の明示指定にも対応する。
    """
    override = os.environ.get("SOFFICE_BIN", "").strip()
    if override:
        return override if Path(override).exists() else None

    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found

    candidates: list[Path] = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(var)
        if base:
            candidates.append(Path(base) / "LibreOffice" / "program" / "soffice.exe")
    return next((str(p) for p in candidates if p.exists()), None)


def ensure_pdf(src: Path, work: Path) -> Path:
    ext = src.suffix.lower()
    if ext == ".pdf":
        return src
    if ext not in (".pptx", ".ppt"):
        raise SlideImportError(f"対応形式は PDF / PowerPoint(.pptx) のみです({ext} は不可)。")
    so = _soffice()
    if not so:
        raise SlideImportError("PowerPointの変換に必要なLibreOfficeが見つかりません。PDFに書き出してから取り込んでください。")
    work.mkdir(parents=True, exist_ok=True)
    pdf = work / (src.stem + ".pdf")
    # 前回の出力が残っていると、変換が失敗しても「出力あり」と誤判定して
    # 古い PDF をそのまま取り込んでしまう。先に消してから変換する。
    try:
        pdf.unlink(missing_ok=True)
    except OSError as e:
        raise SlideImportError(f"前回の変換結果を削除できません: {e}") from e

    # 専用プロファイルで起動する。既に LibreOffice が起動していると、
    # 共有プロファイルを掴んで変換が始まらないことがある。
    profile = work / "_lo_profile"
    profile.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([so, f"-env:UserInstallation={profile.resolve().as_uri()}",
                        "--headless", "--norestore", "--convert-to", "pdf",
                        "--outdir", str(work), str(src)],
                       check=True, capture_output=True, timeout=300)
    except subprocess.SubprocessError as e:
        raise SlideImportError(f"PowerPointのPDF変換に失敗しました: {e}") from e
    if not pdf.exists():
        raise SlideImportError("PowerPointのPDF変換に失敗しました(出力が見つかりません)。")
    return pdf


def rasterize(pdf: Path, pages_dir: Path) -> list[tuple[str, Path, str]]:
    """各ページを 1536×864 白地に内接PNG化。戻り [(sid, png, extracted_text)]。"""
    import fitz
    from PIL import Image
    pages_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf))
    try:
        out = []
        for n in range(doc.page_count):
            page = doc[n]
            scale = min(SLIDE_W / page.rect.width, SLIDE_H / page.rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale * 2, scale * 2), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            img.thumbnail((SLIDE_W, SLIDE_H), Image.LANCZOS)
            canvas = Image.new("RGB", (SLIDE_W, SLIDE_H), (255, 255, 255))
            canvas.paste(img, ((SLIDE_W - img.width) // 2, (SLIDE_H - img.height) // 2))
            sid = f"scene-{n + 1:03d}"
            png = pages_dir / f"{sid}.png"
            canvas.save(png)
            out.append((sid, png, " ".join((page.get_text() or "").split())))
        return out
    finally:
        doc.close()
