#!/usr/bin/env python3
"""Blog Agent — ソース(記事/資料/メモ) → 読み物ブログ記事(+差し込み図解)。

中身は 動画バックエンドのブログ生成モジュール(blog.py)のロジックを移植した blog_core を呼ぶ薄いCLI(UIなし)。
ソース取込・宛先・画風・gpt-image描画は slide-studio の部品を共有。Slide Agent と同じ型。

  build     ソース→(型×画風の推奨)→記事→品質ゲート→ article.json + .md + .html(課金ゼロ)
  images    article.json の各セクションに図解を gpt-image 生成→HTMLに差し込み(課金ゲート付き)
  recommend ソースから画風×型をAIが理由付きで提案(描画しない・課金ゼロ)
  options   投入できる変数の選択肢(宛先/型/画風/モード)

宛先(--author/--reader/--purpose/--tone)・型(--structure)・画風(--theme)は未指定ならAIが推定/推奨。
gpt-image(images)は必ず「概算 → --approved → 実行」の2段。出力 article.json は受け渡しの箱(Blogcast が食う)。
"""
from __future__ import annotations
import argparse
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SLIDE_STUDIO = HERE.parents[2] / "slide-studio"
sys.path.insert(0, str(HERE))                        # blog_core / blog_structures
sys.path.insert(0, str(HERE.parents[1] / "core"))    # style_store(画風の保存・使い回し)
sys.path.insert(0, str(SLIDE_STUDIO / "engine"))     # themes / (build)
sys.path.insert(0, str(SLIDE_STUDIO / "api"))        # ingest / audience / llm / render_ai

import backend_path  # studio-agents/.env の不足値を補完
import style_store

import ingest        # slide-studio/api/ingest.py
import audience      # slide-studio/api/audience.py
import themes as themes_mod   # slide-studio/engine/themes.py
import blog_core     # 移植コア(blog.py 由来)
import blog_structures        # ブログの型
import llm
import article_io


def _warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _valid_theme(name: str | None) -> str:
    if name:
        name = style_store.resolve(name, "theme") or name   # 保存名(「同じテイスト」)も受ける
    valid = {t["name"] for t in themes_mod.list_themes()}
    return name if name in valid else themes_mod.DEFAULT_THEME


def _load_sources(args) -> tuple[str, str]:
    parts: list[str] = []
    default_title = ""
    for f in (getattr(args, "file", None) or []):
        p = pathlib.Path(f)
        if not p.exists():
            raise SystemExit(f"ファイルが見つかりません: {p}")
        parts.append(ingest.from_file(p)); default_title = default_title or p.stem
    for u in (getattr(args, "url", None) or []):
        parts.append(ingest.from_url(u)); default_title = default_title or u
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
    overrides = {k: getattr(args, k) for k in ("author", "reader", "purpose", "tone")
                 if getattr(args, k, None)}
    if len(overrides) == 4:
        aud = dict(audience.DEFAULT); aud.update(overrides); aud["reason"] = "(すべて手動指定)"
        return aud
    try:
        aud = audience.propose(text)
    except Exception as e:
        _warn(f"宛先の推定に失敗(既定値で続行): {e}"); aud = dict(audience.DEFAULT)
    if overrides:
        aud.update(overrides)
        aud["reason"] = (aud.get("reason", "") + " ／ 一部を手動指定で上書き").strip(" ／")
    return aud


def _load_article(path: str) -> tuple[dict, pathlib.Path, pathlib.Path]:
    ap = pathlib.Path(path)
    if not ap.exists():
        raise SystemExit(f"article.json が見つかりません: {ap}")
    # 人が渡す article.json。BOM 付きでも読む (utf-8-sig は BOM 無しなら utf-8 と同じ)
    return json.loads(ap.read_text(encoding="utf-8-sig")), ap, ap.parent


def _parse_markdown_table(path: str, label: str) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        raise SystemExit(f"{label} が見つかりません: {p}")
    rows = []
    for line in p.read_text(encoding="utf-8-sig").splitlines():   # 人が渡す表。BOM 付きでも読む
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or all(not c for c in cells) or all(set(c) <= {"-", ":", " "} for c in cells):
            continue
        rows.append(cells)
    if len(rows) < 2:
        raise SystemExit(f"{label} はヘッダーと少なくとも1行のMarkdown表が必要です: {p}")
    headers = rows[0]
    return {"path": str(p), "headers": headers,
            "rows": [dict(zip(headers, row + [""] * (len(headers) - len(row)))) for row in rows[1:]]}


def cmd_build(args) -> None:
    # None は llm.record_calls 側で完全 no-op。既存呼び出しの出力を変えない。
    previous_backend = os.environ.get("STUDIO_LLM_BACKEND")
    if getattr(args, "llm_backend", None):
        os.environ["STUDIO_LLM_BACKEND"] = args.llm_backend
    try:
        with llm.record_calls(getattr(args, "llm_trace", None)):
            _cmd_build(args)
    finally:
        if previous_backend is None:
            os.environ.pop("STUDIO_LLM_BACKEND", None)
        else:
            os.environ["STUDIO_LLM_BACKEND"] = previous_backend


def _cmd_build(args) -> None:
    text, default_title = _load_sources(args)
    title = args.title or default_title
    aud = _build_audience(text, args)
    if not getattr(args, "tone", None):        # 既定の文体は です・ます(敬体)に固定=文末の揺れ防止(--tone で上書き可)
        aud["tone"] = "敬体・標準"
    preset = getattr(args, "preset", None)
    rebuild_policy = getattr(args, "rebuild_policy", "off")
    premise_arg = getattr(args, "premise", None)
    cta_arg = getattr(args, "cta", None)
    premise = False if rebuild_policy == "off" else (
        rebuild_policy != "off" if premise_arg is None else premise_arg)
    cta = "keep" if rebuild_policy == "off" else (cta_arg or "email")
    important_facts = outline = None
    if rebuild_policy == "ledger":
        if not args.important_facts or not args.outline:
            raise SystemExit("--rebuild-policy ledger には --important-facts と --outline が必要です")
        important_facts = _parse_markdown_table(args.important_facts, "重要事実チェックリスト")
        outline = _parse_markdown_table(args.outline, "アウトライン")

    # 画風×型の推奨(未指定を埋める)。faithful は型不要だが画風は要るので推奨は呼ぶ。
    rec = None
    if not (args.structure and args.theme):
        try:
            rec = blog_core.recommend(text, aud)
        except Exception as e:
            _warn(f"画風×型の推奨に失敗(既定で続行): {e}")

    if preset == "faithful":                              # ①原文に忠実=型を使わない(原文の構成を維持)
        structure_id, structure = None, None
    else:
        structure_id = args.structure or (rec or {}).get("structure")
        structure = blog_structures.get(structure_id) if structure_id else None
        if args.structure and not structure:
            raise SystemExit(f"不明な型: {args.structure}(options で候補を確認)")
        if preset == "restructure" and not structure:     # ②は型が必須(未指定なら既定=explainer)
            structure = blog_structures.default(); structure_id = structure["id"]

    theme_name = _valid_theme(args.theme or (rec or {}).get("theme"))
    style_store.save("last-blog", "blog", "theme", theme_name)
    if getattr(args, "save_style", ""):
        style_store.save(args.save_style, "blog", "theme", theme_name)

    if preset in ("faithful", "restructure"):             # 全量保持パス(凝縮しない)
        mode = preset
        label = "原文に忠実(言い回しだけ)" if preset == "faithful" else "忠実なまま構成変更"
        print(f"… 全量保持で生成中({label}・型={structure_id or '—'}・課金ゼロ)", file=sys.stderr)
        res = blog_core.generate_faithful(text, args.title, aud, structure=structure,
                                          restructure=(preset == "restructure"),
                                          run_gates=not args.no_gates, rebuild_policy=rebuild_policy,
                                          important_facts=important_facts, outline=outline,
                                          premise=premise, cta=cta,
                                          cta_email_endpoint=getattr(args, "cta_email_endpoint", "#email-signup"))
    else:                                                 # 圧縮/長文SEO(既存 generate)
        mode = "compress" if preset == "compress" else args.mode
        target = args.target
        print(f"… 記事を生成中(型={structure_id or '自動'}・モード={mode}・課金ゼロ)", file=sys.stderr)
        res = blog_core.generate(text, title, aud, mode=mode, target=target,
                                 structure=structure, run_gates=not args.no_gates)

    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    full = {"title": res["title"], "audience": aud, "mode": mode,
            "structure": structure_id, "theme": theme_name, "recommend": rec,
            "char_count": res["char_count"], "source_text": text,
            "struct": res["struct"], "gates": res["gates"], "quality_status": res.get("quality_status", "checked"),
            "html": res["html"], "markdown": res["markdown"]}
    title_warnings = article_io.validate_article_title(
        full, article_io.extract_legacy_markdown_title(full["markdown"])
    )
    if title_warnings:
        full["title_warnings"] = title_warnings
        for warning in title_warnings:
            _warn(f"タイトル検証: {warning}")
    if "rebuild" in res:
        full["rebuild"] = res["rebuild"]
    if res.get("original_title"):
        full["original_title"] = res["original_title"]
    (outdir / "article.json").write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "article.md").write_text(res["markdown"], encoding="utf-8")
    (outdir / "article.html").write_text(res["html"], encoding="utf-8")
    # article.html は WP 貼付用の断片。 そのままブラウザで開くと CSS も charset も無い。
    # 見せる/共有する用に、 自己完結の完全な HTML を別ファイルで出す。
    (outdir / "article_preview.html").write_text(
        blog_core.standalone_html(res["html"], res.get("title", "")), encoding="utf-8")
    if "rebuild" in res:
        (outdir / "rebuild-log.json").write_text(json.dumps(res["rebuild"], ensure_ascii=False, indent=2), encoding="utf-8")

    g = res.get("gates") or {}
    gate_summary = {
        "捏造除去": len((g.get("fabrication") or {}).get("removed") or []),
        "再述圧縮": len((g.get("redundancy") or {}).get("removed") or []),
        "焦らし注入": len((g.get("heading_answer") or {}).get("injected") or []),
    }
    publish_gates = ((res.get("rebuild") or {}).get("publish_gates") or {})
    if publish_gates:
        gate_summary["公開ゲートBLOCK"] = sum(len(hits or []) for hits in publish_gates.values())
    _emit({"ok": True, "title": res["title"], "mode": mode, "structure": structure_id,
           "theme": theme_name, "audience": aud, "char_count": res["char_count"],
           "sections": len(res["struct"].get("sections", [])), "gates": gate_summary,
           "quality_status": res.get("quality_status", "checked"),
           "images": "未生成(images サブコマンドで差し込み可)",
           "outputs": {"json": str(outdir / "article.json"),
                       "md": str(outdir / "article.md"),
                       "html": str(outdir / "article.html"),
                       "preview_html": str(outdir / "article_preview.html")}})


def cmd_images(args) -> None:
    d, ap, outdir = _load_article(args.deck)
    struct = d.get("struct") or {}
    theme_name = _valid_theme(args.theme or d.get("theme"))
    src = d.get("source_text", "")
    if not args.approved:
        res = blog_core.gen_images(struct, theme_name, outdir, approved=False, source_text=src)
        _emit(res); return
    print(f"… 各セクションを解説スライド図に生成中(gpt-image・画風={theme_name})", file=sys.stderr)
    res = blog_core.gen_images(struct, theme_name, outdir, approved=True, source_text=src)
    if not res.get("ran"):
        _emit(res); return
    image_map = {int(k): v for k, v in res["image_map"].items()}
    assembled = blog_core.assemble(struct, image_map)
    d["theme"] = theme_name
    d["image_map"] = {str(k): v for k, v in image_map.items()}
    d["html"] = assembled["html"]; d["markdown"] = assembled["markdown"]
    title_warnings = article_io.validate_article_title(
        d, article_io.extract_legacy_markdown_title(d["markdown"])
    )
    if title_warnings:
        d["title_warnings"] = title_warnings
        for warning in title_warnings:
            _warn(f"タイトル検証: {warning}")
    else:
        d.pop("title_warnings", None)
    ap.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "article.md").write_text(assembled["markdown"], encoding="utf-8")
    (outdir / "article.html").write_text(assembled["html"], encoding="utf-8")
    (outdir / "article_preview.html").write_text(
        blog_core.standalone_html(assembled["html"], d.get("title", "")), encoding="utf-8")
    _emit({"ok": True, "theme": theme_name, "images_placed": assembled["images_placed"],
           "cost": res.get("cost"),
           "outputs": {"md": str(outdir / "article.md"),
                       "html": str(outdir / "article.html"),
                       "preview_html": str(outdir / "article_preview.html")}})


def cmd_recommend(args) -> None:
    text, _ = _load_sources(args)
    aud = _build_audience(text, args)
    print("… 画風×型をAIが提案中(claude CLI・課金ゼロ)", file=sys.stderr)
    rec = blog_core.recommend(text, aud)
    _emit({"audience": aud, "recommend": rec})


def cmd_options(args) -> None:
    _emit({
        "sources": "--file / --url / --text(いずれも複数可・混在可) か stdin",
        "audience": {
            "author(誰の記事か)": audience.AUTHORS,
            "reader(誰に読ませるか)": audience.READERS,
            "purpose(目的)": audience.PURPOSES,
            "tone(トーン)": audience.TONES,
        },
        "preset(作り方・主動線)": [
            {"id": "faithful", "name": "原文に忠実(言い回しだけ)", "desc": "口調・言い回しだけ整える。構成は原文のまま・全量保持。型は使わない"},
            {"id": "restructure", "name": "忠実なまま構成を変える", "desc": "内容そのまま、7型から選んで並べ替え・全量保持"},
            {"id": "compress", "name": "要点に圧縮", "desc": "要点に凝縮(約4000字・--targetで可変)。7型から選ぶ"},
        ],
        "structure(記事の型)": [{"id": s["id"], "name": s["name"], "framework": s["framework"]}
                                for s in blog_structures.STRUCTURES],
        "theme(画風・差し込み図解に使用)": [t["name"] for t in themes_mod.list_themes()],
        "mode(preset未指定時のみ)": [
            {"id": "seo_long", "desc": "長文SEO(元ネタを骨子に一般知識で肉付け・捏造ゲートで検証)。既定"},
            {"id": "faithful", "desc": "元ネタ準拠(肉付けなし・短め)"},
        ],
        "note": "作り方は preset が主動線(faithful/restructure=全量保持、compress=約4000字に凝縮)。未指定の宛先/型/画風はAIが推定/推奨。図解画像は images サブコマンド(gpt-image・概算→承認→実行)。",
    })


def main() -> None:
    ap = argparse.ArgumentParser(prog="blog", description="Blog Agent: ソース→読み物ブログ記事(+図解)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _add_src_aud(p):
        p.add_argument("--file", action="append", help="取込ファイル pdf/pptx/docx/txt/md(複数可)")
        p.add_argument("--url", action="append", help="取込URL(複数可)")
        p.add_argument("--text", action="append", help="直接テキスト(複数可)")
        p.add_argument("--author"); p.add_argument("--reader")
        p.add_argument("--purpose"); p.add_argument("--tone")

    b = sub.add_parser("build", help="ソース→記事→品質ゲート→ article.json/.md/.html")
    _add_src_aud(b)
    b.add_argument("--preset", choices=["faithful", "restructure", "compress"],
                   help="作り方: faithful=原文に忠実(言い回しだけ・全量保持) / restructure=忠実なまま構成変更(--structure の型に並べ替え・全量保持) / compress=要点に圧縮(--structure の型・約4000字)。未指定は従来の --mode 準拠")
    b.add_argument("--structure", help="記事の型(explainer/how-to/listicle/comparison/case-study/news-commentary/opinion。未指定はAI推奨。preset=faithful では未使用)")
    b.add_argument("--theme", default="", help="画風(差し込み図解に使用。未指定はAI推奨)")
    b.add_argument("--mode", choices=["seo_long", "faithful"], default="seo_long", help="(preset未指定時のみ)記事モード")
    b.add_argument("--target", type=int, default=4000, help="compress/長文SEO の目標本文字数")
    b.add_argument("--title", default=""); b.add_argument("--out", default="out")
    b.add_argument("--no-gates", action="store_true", help="品質ゲートを回さず素の記事だけ")
    b.add_argument("--save-style", default="", help="今回の画風をこの名前で保存(--theme に保存名で使い回し)")
    b.add_argument("--rebuild-policy", choices=["off", "plain", "audited", "ledger"], default="off",
                   help="POC: off=従来どおり / plain=用語翻訳注入 / audited=監査・局所修正 / ledger=台帳・構造診断も実行")
    b.add_argument("--premise", action=argparse.BooleanOptionalAction, default=None,
                   help="検索初見者向けの前提リードと検索向けタイトルを生成（非offでは既定ON、offでは無効）")
    b.add_argument("--cta", choices=["email", "keep"], default=None,
                   help="LINE登録CTAをメール登録CTAへ置換（非offではemail、offではkeep）")
    b.add_argument("--cta-email-endpoint", default="#email-signup",
                   help="メール登録CTAのリンク先（既定: #email-signup）")
    b.add_argument("--important-facts", help="ledger 用の固定重要事実Markdown表")
    b.add_argument("--outline", help="ledger 用の固定アウトラインMarkdown表")
    b.add_argument("--llm-trace", help="LLM呼び出しメタデータのJSONL出力先（本文・認証情報は保存しない）")
    b.add_argument("--llm-backend", choices=["claude", "codex"], default=None,
                   help="文章生成バックエンド（未指定=STUDIO_LLM_BACKEND→利用可能CLIを自動選択。選択側の失敗時は反対側へ1回だけ退避）")
    b.set_defaults(func=cmd_build)

    im = sub.add_parser("images", help="article.json の各セクションに図解を gpt-image 生成(課金ゲート)")
    im.add_argument("--deck", required=True, help="article.json のパス")
    im.add_argument("--theme", default="", help="画風(未指定は記事の theme)")
    im.add_argument("--approved", action="store_true", help="gpt-image の課金を承認して実行")
    im.set_defaults(func=cmd_images)

    rc = sub.add_parser("recommend", help="ソースから画風×型をAIが理由付きで提案(課金ゼロ)")
    _add_src_aud(rc)
    rc.set_defaults(func=cmd_recommend)

    o = sub.add_parser("options", help="投入できる変数の選択肢(宛先/型/画風/モード)")
    o.set_defaults(func=cmd_options)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
