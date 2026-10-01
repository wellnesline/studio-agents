#!/usr/bin/env python3
"""Reslide Agent — イマイチなデザインの既存スライド(PPTX/PDF)を、OpenAI(gpt-image)で高品質に作り替える。

流れ: 既存スライド → ページ画像化(LibreOffice/PyMuPDF) → 各ページを claude vision で中身抽出
(記載の文言・数字だけ・課金ゼロ) → 1ページ=1スライドの deck 化(layout はAIが内容で選択)
→ gpt-image で描き直し(課金・概算→承認) → 文字照合(vision・課金ゼロ)。

出力の deck.json は Slide と同形式 = そのまま `slide verify/fix`(指摘だけ再描画)や
Slidecast(動画化)に繋がる。元ページは pages/ に保存(before/after 比較用)。

  build --file 資料.pptx|pdf [--theme 画風|保存名] [--save-style NAME] [--approved] [--regen]
"""
from __future__ import annotations
import argparse
import hashlib
import json
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
SLIDE_STUDIO = REPO / "slide-studio"
sys.path.insert(0, str(HERE.parents[1] / "core"))    # style_store
from backend_path import resolve_backend_dir
VIDEO_BACKEND = resolve_backend_dir()
sys.path.insert(0, str(SLIDE_STUDIO / "engine"))
sys.path.insert(0, str(SLIDE_STUDIO / "api"))
sys.path.insert(0, str(VIDEO_BACKEND))

import style_store
import themes as themes_mod
import organize      # LAYOUTS / sanitize_deck / SYSTEM(接地)
import recommend as slide_recommend
import render_ai     # gpt-image 描き直し + estimate + 文字照合は ocr_gate
import ocr_gate
import llm
import build as engine   # export_pdf


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _valid_theme(name: str | None) -> str:
    if name:
        name = style_store.resolve(name, "theme") or name
    valid = {t["name"] for t in themes_mod.list_themes()}
    return name if name in valid else themes_mod.DEFAULT_THEME


def _fingerprint(source: pathlib.Path, theme: str) -> str:
    payload = {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "theme": theme}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _extract_page_slides(png: pathlib.Path, idx: int, total: int, extracted_text: str) -> tuple[list[dict], str | None]:
    """1ページを vision で読み、話題ごとに**複数の構造化スライドへ分割**する(詰め詰めページ対策)。
    記載内容だけ・創作なし・**ページの内容は全ていずれかのスライドに漏れなく割り当てる**。課金ゼロ。"""
    layouts = "\n".join(f"- {k}: {v}" for k, v in organize.LAYOUTS.items())
    ref = f"\n参考(ページから抽出したテキスト): {extracted_text[:1200]}" if extracted_text.strip() else ""
    prompt = f"""この画像はスライド資料の {idx}/{total} ページ目です。このページの内容を、デザインを作り替えるための
構造化スライド(JSON)にしてください。
## 分割の原則(最重要)
- このページに **複数の話題**(例: 目的/対象者/条件/手順フロー/留意事項/スケジュール…)が詰め込まれている場合、
  **1話題=1スライドに分割**する(複数の制度・条件・手順が並ぶ高密度なページは3〜6枚になってよい)。
- **ページに書かれている内容は、全て いずれかのスライドに漏れなく割り当てる**(主役だけ残して他を捨てない)。
  留意事項・注記(※/注)も、内容に意味があるものは箇条書き等で必ず拾う。
- 単純なページ(話題が1つ)なら1枚でよい。
## 厳守
- **ページに実際に書かれている文言・数字だけ**を使う(画像に無い数値・固有名詞・文言を創作しない)。
- 各スライドは内容に最も合う layout を選ぶ(数値が主役→big-number/kpi-row、対比→two-column、手順→steps、
  期日の列び→timeline、比較表→comparison 等。単純な列挙は title-bullets でよい)。
- 装飾・ページ番号のみのノイズは拾わない。各スライドに必ず heading を含める。
## 使える layout
{layouts}
{ref}
## 出力(JSONのみ)
{{"slides":[{{"layout":"<layout名>","elements":[{{"kind":"heading","text":"..."}}, ...]}}, ...]}}"""
    try:
        raw = ocr_gate._vision(png, prompt)
        r = llm.extract_json(raw)
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"
    if isinstance(r, dict) and isinstance(r.get("slides"), list):
        out = []
        for sl in r["slides"]:
            if isinstance(sl, dict) and sl.get("elements"):
                lay = sl.get("layout") if sl.get("layout") in organize.LAYOUTS else "title-bullets"
                out.append({"layout": lay, "elements": sl["elements"], "_page": idx})
        if out:
            return out, None
        return [], "有効な slides を含むJSONが返りませんでした"
    # 旧形式(単一スライド)で返った場合の救済
    if isinstance(r, dict) and r.get("elements"):
        lay = r.get("layout") if r.get("layout") in organize.LAYOUTS else "title-bullets"
        return [{"layout": lay, "elements": r["elements"], "_page": idx}], None
    return [], "slides/elements を含むJSONが返りませんでした"


def _coverage_check(page_text: str, page_slides: list[dict], idx: int) -> list[str]:
    """抽出漏れの検出(課金ゼロ・テキスト照合): ページの文字のうち、どのスライドにも
    割り当てられなかった重要な内容を列挙する(文字照合ゲートの盲点=「そもそも抽出されなかった内容」を塞ぐ)。"""
    if not page_text.strip() or not page_slides:
        return []
    assembled = json.dumps(page_slides, ensure_ascii=False)
    prompt = f"""元スライドのテキストと、作り替え後のスライド群(JSON)を比べ、**作り替え側に入っていない重要な内容**
(数値・条件・期日・留意事項など意味のある情報)を日本語で列挙してください。装飾・ページ番号・重複は無視。
漏れが無ければ空配列。
## 元ページ({idx}ページ目)のテキスト
{page_text[:3000]}
## 作り替え後のスライド群
{assembled[:4000]}
## 出力(JSONのみ)
{{"missing": ["漏れた内容1", ...]}}"""
    try:
        r = llm.generate_json(prompt, "")
        return [str(m) for m in (r.get("missing") or []) if str(m).strip()]
    except Exception:
        return []


def cmd_build(args) -> None:
    src = pathlib.Path(args.file)
    if not src.exists():
        raise SystemExit(f"ファイルが見つかりません: {src}")
    if src.suffix.lower() not in {".pdf", ".pptx", ".ppt"}:
        raise SystemExit("対応形式は PDF / PowerPoint(.pptx) です")
    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    deck_path = outdir / "deck.json"

    # deck 再利用(抽出済みなら vision をやり直さない)。--regen で作り直し。
    if deck_path.exists() and not args.regen:
        try:
            d = json.loads(deck_path.read_text(encoding="utf-8-sig"))   # 手編集される想定。BOM 付きでも読む
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"既存 deck.json を読めません: {e}。--regen で抽出し直してください。")
        deck = d["deck"]
        theme_name = _valid_theme(args.theme or d.get("theme"))
        if d.get("input_fingerprint") != _fingerprint(src, theme_name):
            raise SystemExit("入力ファイルまたはテーマが前回の抽出結果と異なります。再利用せず --regen で抽出し直してください。")
        print("… 抽出済み deck.json を再利用(--regen で作り直し)", file=sys.stderr)
    else:
        # 1. ページ画像化(自己完結 _ingest。単体配布で 動画バックエンド非依存)
        sys.path.insert(0, str(HERE))
        from _ingest import ensure_pdf, rasterize, SlideImportError
        print("… ページ画像化中(LibreOffice/PyMuPDF)", file=sys.stderr)
        try:
            pdf = ensure_pdf(src, outdir / "_work")
            pages = rasterize(pdf, outdir / "pages")   # [(sid, png, text)] 元ページ=before 比較用
        except SlideImportError as e:
            raise SystemExit(str(e))
        if not pages:
            raise SystemExit("ページが抽出できませんでした")

        # 2. 各ページを vision で構造化(並列・課金ゼロ)。詰め詰めページは複数スライドに分割。
        print(f"… 各ページの中身を抽出中({len(pages)}枚・話題ごとに分割・claude vision・課金ゼロ)", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=4) as ex:
            page_results = list(ex.map(
                lambda t: _extract_page_slides(t[1][1], t[0] + 1, len(pages), t[1][2]),
                enumerate(pages)))
        failures = [f"page {i + 1}: {error}" for i, (_, error) in enumerate(page_results) if error]
        if failures:
            raise SystemExit("必須ページの抽出に失敗しました: " + "; ".join(failures))
        per_page = [slides for slides, _ in page_results]
        slides = []
        for page_slides in per_page:
            slides.extend(page_slides)
        if not slides:
            raise SystemExit("中身の抽出に失敗しました(全ページ)")
        for k, s in enumerate(slides, 1):    # 分割後に連番を振り直す
            s["id"] = f"s{k:02d}"
        split_info = {f"page{t[0] + 1}": len(t[1]) for t in enumerate(per_page)}
        deck = organize.sanitize_deck({"title": src.stem, "slides": slides}, src.stem)

        # 2.5 カバレッジ照合(課金ゼロ): 抽出漏れ(どのスライドにも入らなかった重要内容)を検出
        print("… 抽出漏れを照合中(課金ゼロ)", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=4) as ex:
            missing_lists = list(ex.map(
                lambda t: _coverage_check(t[1][2], per_page[t[0]], t[0] + 1),
                enumerate(pages)))
        coverage = {f"page{i + 1}": m for i, m in enumerate(missing_lists) if m}
        if coverage:
            print(f"[warn] 抽出漏れの可能性: {sum(len(v) for v in coverage.values())}件(deck.json の coverage 参照)",
                  file=sys.stderr)

        # 3. 画風(指定/保存名 or AI推奨)
        all_text = "\n".join(t for _, _, t in pages if t)
        theme_name = args.theme
        if not theme_name:
            try:
                theme_name = (slide_recommend.recommend(all_text or src.stem, None) or {}).get("theme")
            except Exception:
                theme_name = None
        theme_name = _valid_theme(theme_name)

        d = {"title": deck.get("title", src.stem), "theme": theme_name,
             "input_fingerprint": _fingerprint(src, theme_name),
             "source_file": str(src), "deck": deck,
             "split": split_info, "coverage": coverage,
             "render": {"engine": "ai", "theme": theme_name}}   # slide verify/fix 互換
        deck_path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    # --regen は古い抽出由来の画像を残さない。 ただし実際の削除は承認ゲートより後で行う:
    #   未承認の見積もりで消すと、 再課金を避けるための生成済み画像を失ったうえに、
    #   「いくらか聞いた」行為そのものが次回の請求額を全枚数ぶんへ引き上げてしまう。
    #   概算は削除後の状態(= 全枚数を生成し直す)で計算するので、 提示額と実額はずれない。
    regen_will_discard_pngs = bool(args.regen)

    style_store.save("last-reslide", "reslide", "theme", theme_name)
    if args.save_style:
        style_store.save(args.save_style, "reslide", "theme", theme_name)

    # 4. 課金ゲート: gpt-image 描き直しの概算(cache 済みはスキップされる)
    n = len(deck["slides"])
    if regen_will_discard_pngs:
        todo = list(deck["slides"])   # 削除後は全枚数が生成対象になる
    else:
        todo = [s for s in deck["slides"] if not (outdir / f"{s['id']}.png").exists()]
    est = render_ai.estimate(len(todo))
    if not args.approved:
        _emit({"needs_approval": True, "theme": theme_name, "slides": n,
               "to_generate": len(todo), "estimate": est,
               "note": "gpt-image で作り替えます(課金)。--approved を付けて再実行してください。"})
        return
    if not render_ai.key_present():
        raise SystemExit("OPENAI_API_KEY が未設定です(.env を確認)")

    # ここまで来た = 承認済み かつ キーもある。 古い画像を捨てるのはこの時点。
    if regen_will_discard_pngs:
        for png in outdir.glob("s*.png"):
            png.unlink()

    # 5. gpt-image で描き直し(cache=True: 生成済み sXX.png は再課金なし)
    print(f"… gpt-image で作り替え中(画風={theme_name}・新規{len(todo)}枚)", file=sys.stderr)
    theme = themes_mod.load(theme_name)
    cost = render_ai.render_deck_ai_full(deck, theme, outdir, size="2048x1152")   # 16:9直接出力(B10)

    # 6. 文字照合(vision・課金ゼロ): 数字・文言が正しく描かれたか
    print("… 文字照合中(claude vision・課金ゼロ)", file=sys.stderr)
    try:
        gate = ocr_gate.check_deck(deck, outdir)
    except Exception as e:
        gate = {"passed": None, "error": f"{type(e).__name__}: {e}"}
    d["ocr_gate"] = gate
    d["quality_status"] = "unknown" if gate.get("error") else "checked"
    d["render"]["cost"] = {"images": cost["images_generated"], "jpy": cost["actual_jpy"]}
    deck_path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    # 7. PDF 書き出し(after)
    pngs = [outdir / f"{s['id']}.png" for s in deck["slides"]]
    pngs = [p for p in pngs if p.exists()]
    outputs = {"deck_json": str(deck_path)}
    outputs_failed = []
    for fmt, export in (("pdf", lambda: engine.export_pdf(pngs, outdir / "deck.pdf")),
                        ("pptx", lambda: render_ai.export_pptx_images(pngs, outdir / "deck.pptx"))):
        try:
            outputs[fmt] = str(export())
        except Exception as e:
            outputs_failed.append({"format": fmt, "error": f"{type(e).__name__}: {e}"})
            print(f"[warn] {fmt} 書き出し失敗: {e}", file=sys.stderr)

    result = {"ok": not outputs_failed, "theme": theme_name, "slides": n,
           "split": d.get("split"), "coverage_missing": sum(len(v) for v in (d.get("coverage") or {}).values()),
           "images_generated": cost["images_generated"],
           "cost": {"jpy": cost["actual_jpy"]},
           "quality_status": d["quality_status"],
           "ocr_gate": {"passed": gate.get("passed"), "flags": len(gate.get("flags") or [])},
           "before": str(outdir / "pages"), "outputs": outputs,
           "note": "文字照合の指摘は `slide fix --deck deck.json --approved` で該当枚だけ再描画できます。"
                   "coverage(抽出漏れ)は自動修正されません — `--regen` で抽出し直すか、該当内容を手動で追記してください"}
    if outputs_failed:
        result["outputs_failed"] = outputs_failed
    _emit(result)
    if outputs_failed:
        raise SystemExit("要求された出力の書き出しに失敗しました: " + ", ".join(x["format"] for x in outputs_failed))


def main() -> None:
    ap = argparse.ArgumentParser(prog="reslide", description="Reslide: 既存スライドを gpt-image で高品質に作り替え")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="PPTX/PDF → 中身抽出(課金ゼロ) → gpt-image 作り替え(概算→承認)")
    b.add_argument("--file", required=True, help="元スライド(PPTX/PDF)")
    b.add_argument("--theme", default="", help="画風(テーマ名 or 保存名。未指定はAI推奨)")
    b.add_argument("--save-style", default="", help="今回の画風をこの名前で保存(使い回し用)")
    b.add_argument("--out", default="out")
    b.add_argument("--approved", action="store_true", help="gpt-image の課金を承認して作り替え")
    b.add_argument("--regen", action="store_true", help="中身の抽出からやり直す")
    b.set_defaults(func=cmd_build)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
