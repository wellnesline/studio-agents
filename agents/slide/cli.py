#!/usr/bin/env python3
"""Slide Agent — slide-studio の仕組みを丸ごと CLI 化(UIなし・移植)。

slide-studio/api/main.py の各エンドポイントを CLI サブコマンドへ:
  build    取込→宛先→画風×構成の推奨→構成生成→各種チェック→テンプレ描画→書き出し(deck.json)
  render   deck.json を再描画。--engine ppt(課金ゼロ) | ai(gpt-image・課金ゲート付き)、--mode full|hybrid
  recommend ソースから画風×構成をAIが理由付きで提案(描画しない・課金ゼロ)
  options  投入できる変数の選択肢一覧(宛先/構成/画風)
  themes   画風(テーマ)一覧
  improve  deck.json のデザイン自己改善(design_gate.improve)
  relayout deck.json の1枚を別レイアウトで作り直す
  verify   AI画像の文字照合(claude vision・課金ゼロ)
  fix      文字照合で指摘のスライドだけ gpt-image で作り直す(課金ゲート付き)

変数投入(build/recommend): --file/--url/--text(いずれも複数可・混在可)でソース、
  --author/--reader/--purpose/--tone で宛先、--structure で構成、--theme で画風。
  未指定の変数はAIが推定/推奨し、指定した変数は人の指定を優先(候補外の自由記述も可)。
課金(gpt-image)は必ず「概算 → --approved → 実行」の2段。承認なしでは一切叩かない(元 main.py の 402 ゲート踏襲)。
中身は slide-studio の関数をそのまま呼ぶ(ロジックは移植せず import 参照)。動画は別エージェント(Slidecast)。
"""
from __future__ import annotations
import argparse
import json
import pathlib
import sys
import zipfile

# --- slide-studio の api/ engine/ を import path に載せる(=移植の実体) ---
SLIDE_STUDIO = pathlib.Path(__file__).resolve().parents[3] / "slide-studio"
sys.path.insert(0, str(SLIDE_STUDIO / "engine"))
sys.path.insert(0, str(SLIDE_STUDIO / "api"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "core"))

import backend_path  # studio-agents/.env の不足値を補完
import style_store   # 画風の保存・使い回し(4エージェント共通)

import build as engine          # engine/build.py (deck→PNG/PPTX/PDF・課金ゼロ)
import themes as themes_mod      # engine/themes.py
import ingest                   # api/ingest.py (取込)
import audience                 # api/audience.py (宛先推定・選択肢)
import recommend                # api/recommend.py (画風×構成推奨)
import structures               # api/structures.py (起承転結)
import organize                 # api/organize.py (構成生成)
import factcheck                # api/factcheck.py (盛りすぎチェック)
import copy_gate                # api/copy_gate.py (言葉づかい)
import gap_check                # api/gap_check.py (不足)
import structure_gate          # api/structure_gate.py (構成完全性)
import design_gate             # api/design_gate.py (デザイン採点/自己改善)
import render_ai               # api/render_ai.py (gpt-image描画・課金)
import ocr_gate                # api/ocr_gate.py (AI画像の文字照合・課金ゼロ)


# ============================ 共通ヘルパ ============================
def _warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _valid_theme(name: str | None) -> str:
    if name:
        name = style_store.resolve(name, "theme") or name   # 保存名(「同じテイスト」)も受ける
    valid = {t["name"] for t in themes_mod.list_themes()}
    return name if name in valid else themes_mod.DEFAULT_THEME


_MARKDOWN_EXT = (".md", ".markdown")


def _title_from_markdown(text: str) -> str:
    """Markdown 素材の先頭行を題として使う。

    ingest が見出し記号を落とすので、 素材が `# 題名` で始まっていれば
    抽出テキストの 1 行目がそのまま題になる。 これが無いと表紙に
    ファイル名 (例: source) が出てしまう。 長すぎる行は題ではないので使わない。
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        return line if len(line) <= 80 else ""
    return ""


def _load_sources(args) -> tuple[str, str]:
    """--file/--url/--text(各複数可・混在可) と stdin を結合。web UI の複数ソース相当。"""
    parts: list[str] = []
    default_title = ""
    for f in (getattr(args, "file", None) or []):
        p = pathlib.Path(f)
        if not p.exists():
            raise SystemExit(f"ファイルが見つかりません: {p}")
        parts.append(ingest.from_file(p))
        # 素材が自分で題を宣言していれば、 ファイル名より優先する。
        # PDF/PPTX はヘッダやフッタが 1 行目に来ることがあるので Markdown だけ見る。
        if p.suffix.lower() in _MARKDOWN_EXT:
            default_title = default_title or _title_from_markdown(parts[-1])
        default_title = default_title or p.stem
    for u in (getattr(args, "url", None) or []):
        parts.append(ingest.from_url(u))
        default_title = default_title or u
    for t in (getattr(args, "text", None) or []):
        parts.append(ingest.from_text(t))
    if not parts and not sys.stdin.isatty():
        data = sys.stdin.read()
        if data.strip():
            parts.append(ingest.from_text(data))
    parts = [p for p in parts if p and p.strip()]
    if not parts:
        raise SystemExit("ソースがありません(--file / --url / --text か stdin。いずれも複数可)")
    return "\n\n".join(parts), (default_title or "入力テキスト")


def _build_audience(text: str, args) -> dict:
    """宛先を組む。--author/--reader/--purpose/--tone の指定があれば AI推定に上書き(部分可)。
    4項目すべて指定なら推定を省略(claude 呼ばない)。候補外の自由記述も可。"""
    overrides = {k: getattr(args, k) for k in ("author", "reader", "purpose", "tone")
                 if getattr(args, k, None)}
    if len(overrides) == 4:
        aud = dict(audience.DEFAULT)
        aud.update(overrides)
        aud["reason"] = "(すべて手動指定)"
        return aud
    try:
        aud = audience.propose(text)
    except Exception as e:
        _warn(f"宛先の推定に失敗(既定値で続行): {e}")
        aud = dict(audience.DEFAULT)
    if overrides:
        aud.update(overrides)
        aud["reason"] = (aud.get("reason", "") + " ／ 一部を手動指定で上書き").strip(" ／")
    return aud


def _load_deck(path: str) -> tuple[dict, pathlib.Path, pathlib.Path]:
    dp = pathlib.Path(path)
    if not dp.exists():
        raise SystemExit(f"deck.json が見つかりません: {dp}")
    d = json.loads(dp.read_text(encoding="utf-8-sig"))   # 人が用意する deck.json。BOM 付きでも読む
    return d, dp, dp.parent


def _save_deck(d: dict, dp: pathlib.Path) -> None:
    dp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _export(deck: dict, theme_name: str, outdir: pathlib.Path, ai: bool, formats: list[str]) -> tuple[dict, list[dict]]:
    """deck 順で PNG を集めて png/pptx/pdf を書き出す。ai は画像貼りPPTX、ppt は編集可PPTX。"""
    pngs = [outdir / f"{s['id']}.png" for s in deck["slides"]]
    pngs = [q for q in pngs if q.exists()]
    if not pngs:
        raise SystemExit("描画されたページがありません")
    theme = themes_mod.load(theme_name)
    outputs: dict[str, str] = {}
    outputs_failed: list[dict] = []
    for fmt in formats:
        try:
            if fmt == "png":
                zpath = outdir / "deck_png.zip"
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, png in enumerate(pngs, 1):
                        zf.write(png, f"{i:02d}_{png.name}")
                outputs["png"] = str(zpath)
            elif fmt == "pdf":
                outputs["pdf"] = str(engine.export_pdf(pngs, outdir / "deck.pdf"))
            elif fmt == "pptx":
                if ai:
                    outputs["pptx"] = str(render_ai.export_pptx_images(pngs, outdir / "deck.pptx"))
                else:
                    subsets = engine.build_font_assets(deck, theme)
                    engine.render_pptx(deck, theme, outdir / "deck.pptx", subsets)
                    outputs["pptx"] = str(outdir / "deck.pptx")
            else:
                _warn(f"未知の書き出し形式は無視: {fmt}")
        except Exception as e:
            _warn(f"{fmt} 書き出し失敗: {e}")
            outputs_failed.append({"format": fmt, "error": f"{type(e).__name__}: {e}"})
    return outputs, outputs_failed


def _exit_if_outputs_failed(result: dict) -> None:
    failed = result.get("outputs_failed") or []
    if failed:
        raise SystemExit("要求された出力の書き出しに失敗しました: " + ", ".join(x["format"] for x in failed))


def _render_deck(d: dict, deck: dict, theme_name: str, outdir: pathlib.Path,
                 engine_kind: str, mode: str, approved: bool, formats: list[str]) -> dict:
    """描画本体。ai は概算→承認ゲート(承認なしでは gpt-image を一切叩かない)。"""
    if engine_kind == "ai":
        est = render_ai.estimate(len(deck["slides"]))
        if not approved:
            return {"needs_approval": True, "engine": "ai", "mode": mode, "estimate": est,
                    "note": "gpt-image で課金が発生します。--approved を付けて再実行すると描画します。"}
        if not render_ai.key_present():
            raise SystemExit("OPENAI_API_KEY が未設定です(.env を確認)")
        theme = themes_mod.load(theme_name)
        if mode == "hybrid":
            cost = render_ai.render_deck_ai(deck, theme, outdir)
        else:
            # cache=False: build のテンプレPNGが残っていると全枚skipされ「承認したのに0枚生成」
            #   になるため(B2)、承認済みの ai full は必ず全枚生成する。size=16:9直接出力(B10)。
            cost = render_ai.render_deck_ai_full(deck, theme, outdir, cache=False, size="2048x1152")
        render = {"engine": "ai", "mode": mode, "theme": theme_name,
                  "cost": {"images": cost["images_generated"], "usd": cost["actual_usd"], "jpy": cost["actual_jpy"]}}
    else:
        engine.render_deck(deck, themes_mod.load(theme_name), outdir)
        render = {"engine": "ppt", "theme": theme_name}
    outputs, outputs_failed = _export(deck, theme_name, outdir, ai=(engine_kind == "ai"), formats=formats)
    d["render"] = render
    result = {"ok": not outputs_failed, "engine": render["engine"], "theme": theme_name,
            "slides": len(deck["slides"]), "outputs": outputs,
            **({"cost": render["cost"]} if "cost" in render else {})}
    if outputs_failed:
        result["outputs_failed"] = outputs_failed
    return result


def _fmts(s: str) -> list[str]:
    return [f.strip() for f in (s or "pptx,pdf").split(",") if f.strip()]


# ============================ サブコマンド ============================
def cmd_build(args) -> None:
    text, default_title = _load_sources(args)
    title = args.title or default_title

    # 宛先(指定 or AI推定)
    aud = _build_audience(text, args)

    # 画風×構成の推奨(宛先反映)。失敗は致命でない。
    rec = None
    try:
        rec = recommend.recommend(text, aud)
    except Exception as e:
        _warn(f"画風×構成の推奨に失敗(既定で続行): {e}")

    # 構成: --structure 指定を優先、無ければ推奨。
    if args.structure:
        structure = structures.get(args.structure)
        if not structure:
            raise SystemExit(f"不明な構成: {args.structure}(options で候補を確認)")
    else:
        structure = structures.get(rec.get("structure")) if rec else None

    preset = getattr(args, "preset", None)
    _PMAX = {"faithful": None, "condensed": 20, "essence": 10}
    _PLABEL = {"faithful": "原文に忠実(全論点・枚数無制限)", "condensed": "要点にまとめる(20枚以内)",
               "essence": "エッセンス(10枚以内)"}
    label = _PLABEL.get(preset, "標準(単発生成)")
    print(f"… 構成を生成中({label}・claude CLI・課金ゼロ)", file=sys.stderr)
    try:
        if preset in _PMAX:                          # 全文採用パス(端折らない)
            max_slides = args.max_slides or _PMAX[preset]
            deck = organize.build_deck_full(title, text, structure=structure, aud=aud, max_slides=max_slides)
        else:                                        # 従来の単発生成(後方互換)
            deck = organize.build_deck(title, text, structure=structure, aud=aud)
    except Exception as e:
        raise SystemExit(f"構成の生成に失敗しました: {e}")

    def _safe(name, fn):
        try:
            return fn()
        except Exception as e:
            _warn(f"{name} 失敗: {e}"); return {"error": str(e)}

    gates = {
        "factcheck": _safe("盛りすぎチェック", lambda: factcheck.check(text, deck)),
        "copy": _safe("言葉づかい", lambda: copy_gate.check(aud, deck)),
        "gaps": _safe("不足", lambda: gap_check.check(aud, text)),
        "structure": _safe("構成完全性", lambda: structure_gate.check(
            args.structure or (rec.get("structure") if rec else None), deck)),
        "design": _safe("デザイン採点", lambda: design_gate.score(deck)),
    }
    quality_status = "unknown" if any("error" in gate for gate in gates.values()) else "checked"

    # 画風: --theme 指定(保存名も可)を優先、無ければ推奨。使った画風は保存(使い回し用)。
    theme_name = _valid_theme(args.theme or (rec or {}).get("theme"))
    style_store.save("last-slide", "slide", "theme", theme_name)
    if getattr(args, "save_style", ""):
        style_store.save(args.save_style, "slide", "theme", theme_name)
    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    deck = organize.sanitize_deck(deck, title)

    d = {"title": deck.get("title", title), "theme": theme_name, "audience": aud,
         "structure": (structure or {}).get("id"), "recommend": rec,
         "source_text": text, "deck": deck, "gates": gates, "quality_status": quality_status}
    deck_path = outdir / "deck.json"
    _save_deck(d, deck_path)

    if args.no_render:
        _emit({"ok": True, "title": deck.get("title"), "theme": theme_name, "audience": aud,
               "structure": (structure or {}).get("id"), "slides": len(deck["slides"]),
               "quality_status": quality_status,
               "deck_json": str(deck_path), "rendered": False})
        return
    print(f"… 描画中(テーマ={theme_name}・課金ゼロ)", file=sys.stderr)
    res = _render_deck(d, deck, theme_name, outdir, "ppt", "full", True, _fmts(args.format))
    _save_deck(d, deck_path)
    res["deck_json"] = str(deck_path); res["title"] = deck.get("title"); res["audience"] = aud
    res["quality_status"] = quality_status
    _emit(res)
    _exit_if_outputs_failed(res)


def cmd_render(args) -> None:
    d, dp, outdir = _load_deck(args.deck)
    deck = organize.sanitize_deck(d["deck"], d.get("title", "スライド"))
    theme_name = _valid_theme(args.theme or d.get("theme"))
    res = _render_deck(d, deck, theme_name, outdir, args.engine, args.mode, args.approved, _fmts(args.format))
    d["deck"] = deck; d["theme"] = theme_name
    _save_deck(d, dp)
    _emit(res)
    _exit_if_outputs_failed(res)


def cmd_recommend(args) -> None:
    text, _ = _load_sources(args)
    aud = _build_audience(text, args)
    print("… 画風×構成をAIが提案中(claude CLI・課金ゼロ)", file=sys.stderr)
    rec = recommend.recommend(text, aud)
    _emit({"audience": aud, "recommend": rec})


def cmd_options(args) -> None:
    _emit({
        "sources": "--file / --url / --text(いずれも複数可・混在可) か stdin",
        "audience": {
            "author(誰の資料か)": audience.AUTHORS,
            "reader(誰に見せるか)": audience.READERS,
            "purpose(目的)": audience.PURPOSES,
            "tone(トーン)": audience.TONES,
        },
        "structure(構成)": [{"id": s["id"], "name": s["name"], "framework": s["framework"]} for s in structures.STRUCTURES],
        "theme(画風)": [t["name"] for t in themes_mod.list_themes()],
        "note": "未指定の変数はAIが推定/推奨。候補外の自由記述も可(宛先)。build/recommend に --author/--reader/--purpose/--tone/--structure/--theme で投入。",
    })


def cmd_themes(args) -> None:
    favs = set()
    try:
        import store
        favs = set(store.get_favorites())
    except Exception:
        pass
    lst = themes_mod.list_themes()
    _emit({"default": themes_mod.DEFAULT_THEME,
           "themes": [{"name": t["name"], "display": t.get("display_name", t["name"]),
                       "serif": t.get("serif"), "fav": t["name"] in favs} for t in lst]})


def cmd_improve(args) -> None:
    d, dp, outdir = _load_deck(args.deck)
    deck = d["deck"]; text = d.get("source_text", "")
    if not deck.get("slides"):
        raise SystemExit("deck が空です")
    print("… デザイン改善中(claude CLI・課金ゼロ)", file=sys.stderr)
    try:
        improved, before, after = design_gate.improve(deck, text)
    except Exception as e:
        raise SystemExit(f"デザイン改善に失敗しました: {e}")
    d["deck"] = improved
    d.setdefault("gates", {})["design"] = after
    if text.strip():
        d["gates"]["factcheck"] = factcheck.check(text, improved)
    _save_deck(d, dp)
    out = {"ok": True, "before": before, "after": after}
    if args.render:
        out["render"] = _render_deck(d, organize.sanitize_deck(improved, d.get("title", "スライド")),
                                     _valid_theme(d.get("theme")), outdir, "ppt", "full", True, _fmts(args.format))
        if not out["render"].get("ok"):
            out["ok"] = False
            out["outputs_failed"] = out["render"]["outputs_failed"]
        _save_deck(d, dp)
    _emit(out)
    _exit_if_outputs_failed(out)


def cmd_relayout(args) -> None:
    d, dp, outdir = _load_deck(args.deck)
    deck = d["deck"]; text = d.get("source_text", "")
    if args.layout not in organize.LAYOUTS:
        raise SystemExit(f"不明なレイアウト: {args.layout}")
    slides = deck.get("slides", [])
    idx = next((i for i, s in enumerate(slides) if s.get("id") == args.slide), None)
    if idx is None:
        raise SystemExit(f"スライドが見つかりません: {args.slide}")
    topic = next((e.get("text") for e in slides[idx].get("elements", []) if e.get("kind") == "heading"), "")
    print("… 作り直し中(claude CLI・課金ゼロ)", file=sys.stderr)
    newslide = organize.build_slide(text, args.layout, topic)
    if not newslide:
        raise SystemExit("このレイアウトでは作り直せませんでした(元ネタに必要なデータが無い可能性)")
    newslide["id"] = args.slide
    slides[idx] = newslide
    d["deck"] = deck
    d.setdefault("gates", {})["design"] = design_gate.score(deck)
    if text.strip():
        d["gates"]["factcheck"] = factcheck.check(text, deck)
    _save_deck(d, dp)
    out = {"ok": True, "slide": args.slide, "layout": args.layout}
    if args.render:
        out["render"] = _render_deck(d, organize.sanitize_deck(deck, d.get("title", "スライド")),
                                     _valid_theme(d.get("theme")), outdir, "ppt", "full", True, _fmts(args.format))
        if not out["render"].get("ok"):
            out["ok"] = False
            out["outputs_failed"] = out["render"]["outputs_failed"]
        _save_deck(d, dp)
    _emit(out)
    _exit_if_outputs_failed(out)


def cmd_verify(args) -> None:
    d, dp, outdir = _load_deck(args.deck)
    if (d.get("render") or {}).get("engine") != "ai":
        raise SystemExit("AI画像形式で描画したあとに使えます(render --engine ai)")
    print("… 文字照合中(claude vision・課金ゼロ)", file=sys.stderr)
    try:
        res = ocr_gate.check_deck(d.get("deck") or {}, outdir)
    except Exception as e:
        res = {"quality_status": "unknown", "error": f"{type(e).__name__}: {e}"}
    else:
        res["quality_status"] = "checked"
    d["ocr_gate"] = res
    d["quality_status"] = res["quality_status"]
    _save_deck(d, dp)
    _emit(res)


def cmd_fix(args) -> None:
    d, dp, outdir = _load_deck(args.deck)
    if (d.get("render") or {}).get("engine") != "ai":
        raise SystemExit("AI画像形式で描画したあとに使えます(render --engine ai)")
    deck = d.get("deck") or {}
    try:
        before = ocr_gate.check_deck(deck, outdir)
    except Exception as e:
        before = {"passed": None, "flags": [], "quality_status": "unknown", "error": f"{type(e).__name__}: {e}"}
        d["ocr_gate"] = before
        d["quality_status"] = "unknown"
        _save_deck(d, dp)
        _emit({"fixed": 0, "before": before, "after": before,
               "quality_status": "unknown", "note": "文字照合を実行できないため、課金を伴う再描画は行いません"})
        return
    before["quality_status"] = "checked"
    ids = sorted({f["slide"] for f in before.get("flags", [])})
    if not ids:
        d["ocr_gate"] = before
        d["quality_status"] = "checked"
        _save_deck(d, dp)
        _emit({"fixed": 0, "before": before, "after": before, "quality_status": "checked", "note": "指摘なし"}); return
    est = render_ai.estimate(len(ids))
    if not args.approved:
        _emit({"needs_approval": True, "slides": ids, "estimate": est,
               "note": "指摘スライドを gpt-image で作り直します(課金)。--approved を付けて再実行してください。"})
        return
    if not render_ai.key_present():
        raise SystemExit("OPENAI_API_KEY が未設定です")
    theme = themes_mod.load(_valid_theme((d.get("render") or {}).get("theme") or d.get("theme")))
    cost = render_ai.regen_slides(deck, theme, outdir, ids, size="2048x1152")
    try:
        after = ocr_gate.check_deck(deck, outdir)
    except Exception as e:
        after = {"passed": None, "flags": [], "quality_status": "unknown", "error": f"{type(e).__name__}: {e}"}
    else:
        after["quality_status"] = "checked"
    d["ocr_gate"] = after
    d["quality_status"] = after["quality_status"]
    _save_deck(d, dp)
    _emit({"fixed": len(ids), "before": before, "after": after,
           "cost": {"images": cost["images_generated"], "jpy": cost["actual_jpy"]}})


# ============================ 引数 ============================
def _add_source_and_audience(p) -> None:
    """build / recommend 共通: ソース(複数)+ 宛先変数の投入口。"""
    p.add_argument("--file", action="append", help="取込ファイル pdf/pptx/docx/txt/md(複数可)")
    p.add_argument("--url", action="append", help="取込URL(複数可)")
    p.add_argument("--text", action="append", help="直接テキスト(複数可)")
    p.add_argument("--author", help="誰の資料か(未指定はAI推定・候補外も可)")
    p.add_argument("--reader", help="誰に見せるか(同上)")
    p.add_argument("--purpose", help="目的(同上)")
    p.add_argument("--tone", help="トーン(同上)")


def _max_slides(value: str) -> int:
    """0 はプリセット既定、それ以外は表紙・本文・締めを収められる枚数だけ許可する。"""
    try:
        max_slides = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("整数で指定してください") from e
    if max_slides != 0 and max_slides < 3:
        raise argparse.ArgumentTypeError(
            "表紙+本文+締めの構成上 3 枚未満は指定できません（0=プリセット既定、または3以上）"
        )
    return max_slides


def main() -> None:
    ap = argparse.ArgumentParser(prog="slide", description="Slide Agent: slide-studio の仕組みを CLI 化(課金は概算→承認→実行)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="ソース→構成→テンプレ描画→書き出し(課金ゼロ)")
    _add_source_and_audience(b)
    b.add_argument("--preset", choices=["faithful", "condensed", "essence"],
                   help="圧縮度: faithful=原文に忠実(全論点・枚数無制限) / condensed=要点にまとめる(20枚以内) / essence=エッセンス(10枚以内)。全文採用が基本(端折らず統合で枚数調整)。未指定は従来の単発生成")
    b.add_argument("--max-slides", type=_max_slides, default=0, help="プリセットの枚数上限を微調整(0=プリセット既定、または3以上)")
    b.add_argument("--structure", help="構成(consulting/service-intro/case-study/pitch/education/report。未指定はAI推奨)")
    b.add_argument("--theme", default="", help="画風(未指定はAI推奨。themes で一覧)")
    b.add_argument("--title", default="")
    b.add_argument("--out", default="out"); b.add_argument("--format", default="pptx,pdf")
    b.add_argument("--no-render", action="store_true", help="deck.json だけ作り描画しない")
    b.add_argument("--save-style", default="", help="今回の画風をこの名前で保存(--theme に保存名で使い回し)")
    b.set_defaults(func=cmd_build)

    r = sub.add_parser("render", help="deck.json を再描画(ppt=課金ゼロ / ai=gpt-image課金ゲート)")
    r.add_argument("--deck", required=True)
    r.add_argument("--engine", choices=["ppt", "ai"], default="ppt")
    r.add_argument("--mode", choices=["full", "hybrid"], default="full", help="ai時: full=全面委譲 / hybrid=AI背景+正確テキスト")
    r.add_argument("--theme", default=""); r.add_argument("--format", default="pptx,pdf")
    r.add_argument("--approved", action="store_true", help="gpt-image の課金を承認して実行")
    r.set_defaults(func=cmd_render)

    rc = sub.add_parser("recommend", help="ソースから画風×構成をAIが理由付きで提案(描画しない・課金ゼロ)")
    _add_source_and_audience(rc)
    rc.set_defaults(func=cmd_recommend)

    o = sub.add_parser("options", help="投入できる変数の選択肢一覧(宛先/構成/画風)")
    o.set_defaults(func=cmd_options)

    t = sub.add_parser("themes", help="画風(テーマ)一覧"); t.set_defaults(func=cmd_themes)

    im = sub.add_parser("improve", help="deck.json のデザイン自己改善(課金ゼロ)")
    im.add_argument("--deck", required=True)
    im.add_argument("--render", action="store_true", help="改善後に再描画(ppt)")
    im.add_argument("--format", default="pptx,pdf")
    im.set_defaults(func=cmd_improve)

    rl = sub.add_parser("relayout", help="deck.json の1枚を別レイアウトで作り直す(課金ゼロ)")
    rl.add_argument("--deck", required=True); rl.add_argument("--slide", required=True)
    rl.add_argument("--layout", required=True); rl.add_argument("--render", action="store_true")
    rl.add_argument("--format", default="pptx,pdf")
    rl.set_defaults(func=cmd_relayout)

    v = sub.add_parser("verify", help="AI画像の文字照合(vision・課金ゼロ)")
    v.add_argument("--deck", required=True); v.set_defaults(func=cmd_verify)

    fx = sub.add_parser("fix", help="文字照合の指摘スライドを gpt-image で作り直す(課金ゲート)")
    fx.add_argument("--deck", required=True); fx.add_argument("--approved", action="store_true")
    fx.set_defaults(func=cmd_fix)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
