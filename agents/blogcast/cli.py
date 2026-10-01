#!/usr/bin/env python3
"""Blogcast Agent — 記事(URL / ブログ / テキスト) → 台本の型付き解説動画(gpt-image・自由な画作り)。

v2: スライド風テンプレをやめ、動画バックエンドの画像生成(gen_slides=gpt-image-2.5-sunburst)必須。
記事 →(台本の型)→ 絵コンテ(slide_prompt+narration) →(gpt-image で各シーンの画を生成+EL音声+静止fade字幕)→ 動画。
画風 = AIが記事から提案(自由文)。保存・使い回し可(--art に保存名 / --save-style NAME)。

  styles   台本の型 一覧
  arts     保存済みの画風 一覧
  build    記事 → 型 → 絵コンテ → 概算(gpt-image+EL) → --approved で動画

課金: 絵コンテまで課金ゼロ。動画化は gpt-image(枚数×約2円) + EL音声 → 必ず概算→承認。
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import sys
import uuid

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
SLIDE_STUDIO = REPO / "slide-studio"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "studio-agents" / "core"))
sys.path.insert(0, str(SLIDE_STUDIO / "engine"))
sys.path.insert(0, str(SLIDE_STUDIO / "api"))

import blogcast_core
import style_store
import audience
import video_el      # slide-studio: presets / _run_runner(bridge起動)
import render_ai

_EL_YEN_PER_1000 = 4.5
_IMG_YEN = 1.7       # gpt-image-2.5-sunburst low 1枚の概算(USD 0.011×155円、render_ai.COST_PER_IMAGE_USD と同値。実請求はAPI usage優先)


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _fingerprint(article: str, args, voice: dict) -> str:
    """Only identical source and generation-affecting options may share artifacts."""
    payload = {
        "article_sha256": hashlib.sha256(article.encode("utf-8")).hexdigest(),
        "preset": args.preset,
        "style": args.style,
        "art": style_store.resolve(args.art, "art_style") if args.art else None,
        "voice": {k: voice.get(k) for k in ("id", "voice_id", "seed", "voice_params")},
        "title": args.title,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _load_reusable_job(path: pathlib.Path, fingerprint: str, regen: bool) -> dict | None:
    if regen or not path.exists():
        return None
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"既存 el_job.json を読めません: {e}。--regen で作り直してください。")
    if job.get("input_fingerprint") != fingerprint:
        raise SystemExit("入力または動画指定が前回のジョブと異なります。キャッシュを再利用せず --regen で作り直してください。")
    if not job.get("cid") or not job.get("vid"):
        raise SystemExit("既存 el_job.json に job ID がありません。--regen で作り直してください。")
    return job


def _discard_stale_video(outdir: pathlib.Path) -> None:
    """A fresh job must not leave an older successful video as its result."""
    video_path = outdir / "video.mp4"
    if video_path.exists():
        video_path.unlink()


def _backend_projects_dir() -> pathlib.Path:
    """backend の状態 root を、 backend 自身と同じ規約で解決する。

    保存先は環境変数 VIDEO_BACKEND_PROJECTS_DIR で差し替えられる(別途用意する動画バックエンドの仕様)。
    こちらが VIDEO_BACKEND/projects を直接見ていると、 保存先を切り替えたときに
    キャッシュの回収先と削除先だけがずれる。 回収に失敗すると gpt-image が
    再生成され、 黙って再課金になる。
    """
    override = os.environ.get("VIDEO_BACKEND_PROJECTS_DIR", "").strip()
    if override:
        return pathlib.Path(override).expanduser().resolve()
    return video_el.VIDEO_BACKEND / "projects"


def _recover_slides(cache_dir: pathlib.Path, cid: str, vid: str) -> None:
    """Keep completed runner assets even when a later runner stage fails."""
    try:
        slides_src = _backend_projects_dir() / cid / "videos" / vid / "slides"
        if slides_src.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            for p in slides_src.glob("scene-*.png"):
                shutil.copy(p, cache_dir / p.name)
    except Exception as e:
        print(f"[warn] スライドキャッシュ回収失敗(次回は再生成): {e}", file=sys.stderr)


def _discard_backend_slide_pngs(cid: str, vid: str) -> None:
    """Force gpt-image regeneration while retaining the job's audio artifacts."""
    slides_dir = _backend_projects_dir() / cid / "videos" / vid / "slides"
    try:
        for png in slides_dir.glob("*.png"):
            png.unlink()
    except OSError as e:
        raise SystemExit(f"既存スライド画像を削除できません: {e}")


def cmd_styles(args) -> None:
    _emit({"styles": blogcast_core.styles(),
           "note": "記事の動画化は owned_explainer(実用/物語)が本命。未指定なら記事からAIが提案。"})


def cmd_arts(args) -> None:
    _emit({"saved_styles": style_store.list_all(),
           "note": "--art に保存名を渡すと同じテイストで作れる。build 時 --save-style NAME で保存。"})


def _resolve_voice(args) -> str:
    """--voice、無ければ環境変数 DEFAULT_VOICE_PRESET。どちらも無ければエラー(コードに既定の声を持たない)。"""
    v = (getattr(args, "voice", None) or os.environ.get("DEFAULT_VOICE_PRESET", "")).strip()
    if not v:
        raise SystemExit("音声プリセットを --voice か環境変数 DEFAULT_VOICE_PRESET で指定してください(slidecast voices で一覧)")
    return v


def cmd_build(args) -> None:
    print("… 記事を取得中", file=sys.stderr)
    article, default_title = blogcast_core.load_source(args.file, args.url, args.text)
    if not article.strip():
        raise SystemExit("記事が空です")
    title = args.title or default_title
    args.voice = _resolve_voice(args)
    try:
        preset = video_el._load_preset(args.voice)
    except ValueError as e:
        raise SystemExit(f"{e}(slidecast voices で一覧)")
    fingerprint = _fingerprint(article, args, preset)

    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    sb_path = outdir / "storyboard.json"

    # 絵コンテ再利用(--regen なし): 保存済みの 型・画風・ナレをそのまま使う(再提案しない=固定)。
    if sb_path.exists() and not args.regen:
        try:
            sb = json.loads(sb_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"既存 storyboard.json を読めません: {e}。--regen で作り直してください。")
        if sb.get("input_fingerprint") != fingerprint:
            raise SystemExit("記事または絵コンテ指定が前回と異なります。storyboard を再利用せず --regen で作り直してください。")
        if args.preset is not None:
            if "preset" not in sb:
                print("[warn] 保存済み storyboard に preset がありません。旧形式として再利用します。", file=sys.stderr)
            elif sb["preset"] != args.preset:
                raise SystemExit("--preset が保存済み storyboard と異なります。--regen で絵コンテから作り直してください。")
        style = sb.get("style") or "owned_explainer_practical"
        art = (style_store.resolve(args.art, "art_style") if args.art else None) or sb.get("art_style") or blogcast_core._DEFAULT_ART
        print("… 絵コンテ再利用(storyboard.json・型/画風/ナレ固定・--regen で作り直し)", file=sys.stderr)
    else:
        try:
            aud = audience.propose(article)
        except Exception:
            aud = dict(audience.DEFAULT)
        # 台本の型(指定 or AI提案)
        valid_styles = {s["id"] for s in blogcast_core.styles()}
        if args.style:
            if args.style not in valid_styles:
                raise SystemExit(f"不明な型: {args.style}(styles で一覧)")
            style = args.style
        else:
            sug = blogcast_core.suggest_style(article, title)
            style = sug["style"]
            print(f"… 台本の型をAI提案: {style}（{sug['reason']}）", file=sys.stderr)
        # 画風(保存名 or 自由文 or AI提案)
        art = style_store.resolve(args.art, "art_style") if args.art else None
        if not art:
            print("… 画風をAI提案中(claude・課金ゼロ)", file=sys.stderr)
            art = blogcast_core.suggest_art(article, title)
            print(f"… 画風: {art[:80]}…", file=sys.stderr)
        print(f"… 記事を型「{style}」で絵コンテに(claude・課金ゼロ)", file=sys.stderr)
        sb, _st = blogcast_core.build_storyboard(article, style, aud, title, preset=getattr(args, "preset", None))
        sb["style"] = style
        sb["art_style"] = art
        sb["preset"] = args.preset
        sb["input_fingerprint"] = fingerprint
        sb_path.write_text(json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8")

    # 使った画風を保存(使い回し用): last-blogcast は常に、--save-style は命名保存。
    style_store.save("last-blogcast", "blogcast", "art_style", art)
    if args.save_style:
        style_store.save(args.save_style, "blogcast", "art_style", art)
    scenes = sb["scenes"]
    job_path = outdir / "el_job.json"
    reusable_job = _load_reusable_job(job_path, fingerprint, args.regen)

    # スライドキャッシュ: 前回ビルドの生成済み画像(outdir/slides_cache/)があれば png として渡し、
    #   gpt-image の再生成(再課金)を回避。--regen-images で捨てて作り直す。
    cache_dir = outdir / "slides_cache"
    # --regen(絵コンテ作り直し)でも破棄: slide_prompt が変わるのに旧画像を使い回すと
    #   「新ナレ×旧絵」の動画になるため(B3)。
    #   ただし実際の rmtree は承認ゲートより後で行う。 未承認の見積もりでキャッシュを消すと、
    #   「いくらかかるか聞いた」行為そのものが次回の請求額を上げてしまう。
    #   概算は破棄後の状態(= 全シーンを生成し直す)で計算するので、提示額と実額はずれない。
    cache_will_be_discarded = (args.regen or args.regen_images) and cache_dir.exists()
    cached: dict[str, pathlib.Path] = {}
    if cache_dir.exists() and not cache_will_be_discarded:
        for s in scenes:
            p = cache_dir / f"{s['id']}.png"
            if p.exists():
                cached[s["id"]] = p
    n_gen = len(scenes) - len(cached)

    # 概算(gpt-image=キャッシュ外のみ + EL) → 承認ゲート
    chars = sum(len(s.get("narration") or "") for s in scenes)
    est = {"images": n_gen, "images_cached": len(cached),
           "images_yen_approx": round(n_gen * _IMG_YEN, 1),
           "el_chars": chars, "el_yen_approx": round(chars / 1000.0 * _EL_YEN_PER_1000, 1)}
    est["total_yen_approx"] = round(est["images_yen_approx"] + est["el_yen_approx"], 1)
    if not args.approved:
        _emit({"needs_approval": True, "style": style, "art_style": art,
               "scenes": len(scenes), "estimate": est,
               "note": "gpt-image(画像生成)+ElevenLabs(音声)で課金が発生します。--approved で動画をビルドします。"})
        return

    missing_keys = []
    if not video_el.el_key_present():
        missing_keys.append("ELEVENLABS_API_KEY")
    if n_gen > 0 and not render_ai.key_present():
        missing_keys.append("OPENAI_API_KEY")
    if missing_keys:
        raise SystemExit(f"{' と '.join(missing_keys)} が未設定です(.env を確認)")

    # ここまで来た = 承認済み かつ キーも揃っている。 破棄はこの時点で初めて行う
    # (キー不足で上の SystemExit に落ちる場合も、成果物は失われない)。
    _discard_stale_video(outdir)
    if cache_will_be_discarded:
        shutil.rmtree(cache_dir)

    # job v2: キャッシュ済シーンは png(生成skip)、未生成は slide_prompt(gen_slides が gpt-image 生成)
    print(f"… 動画をビルド中(gpt-image 新規{n_gen}枚/キャッシュ{len(cached)}枚 + EL={preset.get('label')}・静止+fade・字幕あり)",
          file=sys.stderr)
    cid = reusable_job["cid"] if reusable_job else f"bc_{uuid.uuid4().hex[:8]}"
    vid = reusable_job["vid"] if reusable_job else f"v_{uuid.uuid4().hex[:8]}"
    if args.regen_images and reusable_job:
        _discard_backend_slide_pngs(cid, vid)
    job_slides = []
    for s in scenes:
        e = {"id": s["id"], "beat_role": s.get("beat_role", ""),
             "headline": s.get("headline", ""),
             "slide_prompt": s.get("slide_prompt", ""),
             "narration": s.get("narration", "")}
        if s["id"] in cached:
            e["png"] = str(cached[s["id"]].resolve())
        job_slides.append(e)
    job = {"cid": cid, "vid": vid, "input_fingerprint": fingerprint, "state": "running",
           "title": sb.get("title", title),
           "preset": {"id": preset["id"], "label": preset.get("label", preset["id"]),
                      "voice_id": preset["voice_id"], "gender": preset.get("gender", ""),
                      "seed": preset.get("seed", 42), "voice_params": preset.get("voice_params") or {}},
           "slide_style": art,
           "slides": job_slides, "pptx": None}
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        final = video_el._run_runner(job_path)
    except Exception:
        job["state"] = "failed"
        job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        _recover_slides(cache_dir, cid, vid)
    if not final:
        job["state"] = "failed"
        job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit("動画ビルドに失敗しました(動画バックエンド runner)。stderr のログを確認してください。")
    job["state"] = "completed"
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    dest = outdir / "video.mp4"
    shutil.copy(final, dest)

    _emit({"ok": True, "video": str(dest), "style": style, "art_style": art,
           "voice": preset.get("label"), "scenes": len(scenes),
           "images_generated": n_gen, "images_cached": len(cached), "estimate": est})


def main() -> None:
    ap = argparse.ArgumentParser(prog="blogcast", description="Blogcast: 記事→台本の型付き解説動画(gpt-image)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("styles", help="台本の型 一覧")
    s.set_defaults(func=cmd_styles)

    a = sub.add_parser("arts", help="保存済みの画風 一覧")
    a.set_defaults(func=cmd_arts)

    b = sub.add_parser("build", help="記事→型→絵コンテ→概算→動画(gpt-image+EL)")
    g = b.add_mutually_exclusive_group()
    g.add_argument("--url", help="記事URL")
    g.add_argument("--file", help="記事ファイル(Blogのarticle.json / pdf / txt 等)")
    g.add_argument("--text", help="記事テキスト直接")
    b.add_argument("--preset", choices=["faithful", "condensed", "essence"],
                   help="尺/圧縮度: faithful=全シーン(記事を章分割・全論点・上限なし) / condensed=20シーン以内 / essence=10シーン以内。未指定は従来(6〜14)")
    b.add_argument("--style", help="台本の型(未指定はAI提案。styles で一覧)")
    b.add_argument("--art", default="", help="画風(保存名 or 自由文。未指定はAI提案)")
    b.add_argument("--save-style", default="", help="今回の画風をこの名前で保存(使い回し用)")
    b.add_argument("--voice", default=None, help="音声プリセット(slidecast voices で一覧。省略時は環境変数 DEFAULT_VOICE_PRESET)")
    b.add_argument("--title", default="")
    b.add_argument("--out", default="out")
    b.add_argument("--approved", action="store_true", help="gpt-image+EL の課金を承認して動画をビルド")
    b.add_argument("--regen", action="store_true", help="絵コンテ(ナレ・画づくり)を作り直す")
    b.add_argument("--regen-images", action="store_true", help="スライド画像キャッシュを捨てて gpt-image で作り直す")
    b.set_defaults(func=cmd_build)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
