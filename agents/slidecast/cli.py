#!/usr/bin/env python3
"""Slidecast Agent — スライド(deck.json) → ナレーション付き解説動画(mp4)。

Slide エージェントの出力 deck.json(+ 同ディレクトリの sXX.png / deck.pptx)を入力に、
ElevenLabs ナレーション + 動画バックエンドの seminar 一気通貫(構造reveal)で final.mp4 を焼く。
slide-studio の video_el.py(方式B)の部品を流用し、**pid 結合を剥がして deck.json 直受け**にした版。
既存 PNG を使う(再描画しない)ので、承認前に gpt-image 課金が走るバグも構造的に回避。

  voices   使える音声プリセット一覧(課金ゼロ)
  build    deck.json → 動画。ナレ生成(課金ゼロ)→ EL概算 → --approved で合成(EL従量課金)

重依存: 別途用意する動画バックエンド+ uv + LibreOffice(構造reveal) + ffmpeg + ELEVENLABS_API_KEY。
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
SLIDE_STUDIO = HERE.parents[2] / "slide-studio"
sys.path.insert(0, str(HERE.parents[1] / "core"))
sys.path.insert(0, str(SLIDE_STUDIO / "engine"))
sys.path.insert(0, str(SLIDE_STUDIO / "api"))

import backend_path  # studio-agents/.env の不足値を補完
import video_el   # slide-studio/api/video_el.py (方式B: narrate_el / estimate_cost / _run_runner / list_presets)


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _fingerprint(source: bytes, voice: dict, route: str) -> str:
    """Cache key for artifacts whose reuse can change billable runner work."""
    payload = {
        "route": route,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "voice": {k: voice.get(k) for k in ("id", "voice_id", "seed", "voice_params")},
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _load_reusable_job(job_path: pathlib.Path, fingerprint: str, regen: bool) -> dict | None:
    if regen or not job_path.exists():
        return None
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"既存 el_job.json を読めません: {e}。--regen で作り直してください。")
    if job.get("input_fingerprint") != fingerprint:
        raise SystemExit("入力または音声指定が前回の動画ジョブと異なります。キャッシュを再利用せず --regen で作り直してください。")
    if not job.get("cid") or not job.get("vid"):
        raise SystemExit("既存 el_job.json に job ID がありません。--regen で作り直してください。")
    return job


def _discard_stale_video(outdir: pathlib.Path) -> None:
    """A fresh job must not leave an older successful video as its result."""
    video_path = outdir / "video.mp4"
    if video_path.exists():
        video_path.unlink()


def _load_reusable_narration(path: pathlib.Path, fingerprint: str, regen: bool,
                             *, required_mode: str | None = None) -> dict | None:
    if regen or not path.exists():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"既存 narration.json を読めません: {e}。--regen で作り直してください。")
    if saved.get("_input_fingerprint") != fingerprint or not isinstance(saved.get("narration"), dict):
        raise SystemExit("既存 narration.json の入力条件を確認できません。--regen でナレーションを作り直してください。")
    if required_mode and saved.get("_narration_mode") != required_mode:
        raise SystemExit("既存 narration.json は全ページ成功を確認した形式ではありません。--regen で作り直してください。")
    return saved["narration"]


def cmd_voices(args) -> None:
    _emit({"voices": [{"id": p["id"], "label": p.get("label", p["id"]), "gender": p.get("gender", "")}
                      for p in video_el.list_presets()]})


def _resolve_voice(args) -> str:
    """--voice、無ければ環境変数 DEFAULT_VOICE_PRESET。どちらも無ければエラー(コードに既定の声を持たない)。"""
    v = (getattr(args, "voice", None) or os.environ.get("DEFAULT_VOICE_PRESET", "")).strip()
    if not v:
        raise SystemExit("音声プリセットを --voice か環境変数 DEFAULT_VOICE_PRESET で指定してください(slidecast voices で一覧)")
    return v


def cmd_build(args) -> None:
    dp = pathlib.Path(args.deck)
    if not dp.exists():
        raise SystemExit(f"deck.json が見つかりません: {dp}")
    d = json.loads(dp.read_text(encoding="utf-8-sig"))   # 人が用意する deck.json。BOM 付きでも読む
    outdir = dp.parent
    deck = d.get("deck") or {}
    if not deck.get("slides"):
        raise SystemExit("deck が空です(先に Slide で作成してください)")
    if not video_el.el_key_present():
        raise SystemExit("ELEVENLABS_API_KEY が未設定です(.env を確認)")
    aud = d.get("audience") or {}
    args.voice = _resolve_voice(args)
    try:
        preset = video_el._load_preset(args.voice)
    except ValueError as e:
        raise SystemExit(f"{e}(voices で一覧を確認)")
    fingerprint = _fingerprint(dp.read_bytes(), preset, "deck")
    job_path = outdir / "el_job.json"
    reusable_job = _load_reusable_job(job_path, fingerprint, args.regen)

    # Slide が描いた既存スライドPNGを使う(再描画しない=再課金なし)
    png_by_id = {}
    for sl in deck["slides"]:
        p = outdir / f"{sl['id']}.png"
        if p.exists():
            png_by_id[sl["id"]] = p
    if not png_by_id:
        raise SystemExit(f"スライド画像(sXX.png)が {outdir} にありません。先に Slide で描画してください")

    # ナレーション: 1動画で固定(narration.json)。既存あれば再利用、--regen で作り直し。
    narr_path = outdir / "narration.json"
    narr = _load_reusable_narration(narr_path, fingerprint, args.regen)
    if narr is not None:
        print("… ナレーション再利用(narration.json・--regen で作り直し)", file=sys.stderr)
    else:
        print("… ナレーション生成中(claude・課金ゼロ)", file=sys.stderr)
        narr = video_el.narrate_el(deck, aud, preset.get("narration_persona") or {},
                                   preset.get("narration_tone", ""))
        # 空ナレ・欠けナレを保存しない(B6): 保存すると以後ずっと再利用され無音動画になる。
        missing = [sid for sid in png_by_id if not (narr.get(sid) or "").strip()]
        if not narr or missing:
            raise SystemExit(f"ナレーション生成に失敗しました(欠け: {missing or '全部'})。再実行してください。")
        narr_path.write_text(json.dumps({"_input_fingerprint": fingerprint, "narration": narr}, ensure_ascii=False, indent=2), encoding="utf-8")
    cost = video_el.estimate_cost(narr)

    # 課金ゲート: 承認なしなら概算だけ返す(EL を叩かない)
    if not args.approved:
        _emit({"needs_approval": True, "voice": preset.get("label", args.voice), "cost": cost,
               "slides": len(png_by_id),
               "note": "ElevenLabs 音声合成で課金が発生します。--approved を付けて再実行すると動画をビルドします。"})
        return

    # job.json を組み 動画バックエンド runner(bridge)を起動。静止スライド+fade+字幕は
    #   build_episode 側(bridge が reveal_mode="static"/scene_transition="fade" を指定)= 音声・字幕は元に忠実。
    print(f"… 動画をビルド中(EL音声={preset.get('label')}・静止+fade・字幕あり)", file=sys.stderr)
    # 古い動画は「今回の結果」として残さない。 ただし破棄は必ず承認ゲートより後で行う:
    #   承認前に消すと、 見積もりを見ただけのユーザーが代わりを得られないまま完成品を失う。
    _discard_stale_video(outdir)
    cid = reusable_job["cid"] if reusable_job else f"ss_{uuid.uuid4().hex[:8]}"
    vid = reusable_job["vid"] if reusable_job else f"v_{uuid.uuid4().hex[:8]}"
    slides = [{"id": sid, "png": str(p.resolve()), "narration": narr.get(sid, "")}
              for sid, p in png_by_id.items()]
    job = {"cid": cid, "vid": vid, "input_fingerprint": fingerprint, "state": "running",
           "title": deck.get("title", "スライド解説"),
           "preset": {"id": preset["id"], "label": preset.get("label", preset["id"]),
                      "voice_id": preset["voice_id"], "gender": preset.get("gender", ""),
                      "seed": preset.get("seed", 42), "voice_params": preset.get("voice_params") or {}},
           "slides": slides, "pptx": None}
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        final = video_el._run_runner(job_path)
    except Exception:
        job["state"] = "failed"
        job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    if not final:
        job["state"] = "failed"
        job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit("動画ビルドに失敗しました(動画バックエンド runner)。stderr のログを確認してください。")
    job["state"] = "completed"
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    dest = outdir / "video.mp4"
    shutil.copy(final, dest)
    _emit({"ok": True, "video": str(dest), "voice": preset.get("label"), "cost": cost})


def cmd_pptx(args) -> None:
    """手持ちの PPTX/PDF を直接動画化。動画バックエンドの slide_import(取込→ページ画像化→vision ナレ)を流用。
    Slide エージェント(deck.json)を経由しない別ルート。ページ画像をそのままスライドに使う。"""
    src = pathlib.Path(args.file)
    if not src.exists():
        raise SystemExit(f"ファイルが見つかりません: {src}")
    if src.suffix.lower() not in {".pdf", ".pptx", ".ppt"}:
        raise SystemExit("対応形式は PDF / PowerPoint(.pptx) です")
    if not video_el.el_key_present():
        raise SystemExit("ELEVENLABS_API_KEY が未設定です(.env を確認)")
    args.voice = _resolve_voice(args)
    try:
        preset = video_el._load_preset(args.voice)
    except ValueError as e:
        raise SystemExit(f"{e}(voices で一覧)")
    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    fingerprint = _fingerprint(src.read_bytes(), preset, "pptx")
    job_path = outdir / "el_job.json"
    reusable_job = _load_reusable_job(job_path, fingerprint, args.regen)

    # 別途用意する動画バックエンドの取込ロジックを使う(bridge と同じ依存)
    sys.path.insert(0, str(video_el.VIDEO_BACKEND))
    try:
        from api.slide_import import ensure_pdf, rasterize, narrate, SlideImportError
    except Exception as e:
        raise SystemExit(f"取込モジュールの読込に失敗(別途用意する動画バックエンド): {e}")

    title = src.stem
    print("… パワポ/PDFをページ画像化中(LibreOffice/PyMuPDF)", file=sys.stderr)
    try:
        pdf = ensure_pdf(src, outdir / "_work")
        pages = rasterize(pdf, outdir / "slides")   # [(sid, png, extracted_text)]
    except SlideImportError as e:
        raise SystemExit(str(e))
    if not pages:
        raise SystemExit("ページが抽出できませんでした")

    # ナレーション: 1動画で固定(narration.json)。既存あれば再利用、--regen で作り直し。
    narr_path = outdir / "narration.json"
    narrs = _load_reusable_narration(narr_path, fingerprint, args.regen, required_mode="vision-strict-v1")
    if narrs is not None:
        print("… ナレーション再利用(narration.json・--regen で作り直し)", file=sys.stderr)
    else:
        print(f"… 各ページのナレーション生成中({len(pages)}枚・claude vision・課金ゼロ)", file=sys.stderr)
        from concurrent.futures import ThreadPoolExecutor

        def _one(t):
            sid, png, text, idx = t
            return sid, narrate(png, title, idx, len(pages), text, strict=True)

        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                narrs = dict(ex.map(_one, [(sid, png, text, i + 1) for i, (sid, png, text) in enumerate(pages)]))
        except SlideImportError as e:
            raise SystemExit(f"必須ページのナレーション生成に失敗しました: {e}")
        except Exception as e:
            raise SystemExit(f"必須ページのナレーション生成に失敗しました: {type(e).__name__}: {e}")
        if set(narrs) != {sid for sid, _, _ in pages} or any(not (narrs.get(sid) or "").strip() for sid, _, _ in pages):
            raise SystemExit("必須ページのナレーション生成に失敗しました。課金工程には進みません。")
        narr_path.write_text(json.dumps({"_input_fingerprint": fingerprint, "_narration_mode": "vision-strict-v1",
                                         "narration": narrs}, ensure_ascii=False, indent=2), encoding="utf-8")
    if (set(narrs) != {sid for sid, _, _ in pages}
            or any(not isinstance(narrs.get(sid), str) or not narrs[sid].strip() for sid, _, _ in pages)):
        raise SystemExit("必須ページのナレーションが不完全です。--regen で作り直してください。課金工程には進みません。")
    chars = sum(len(v or "") for v in narrs.values())
    cost = {"chars": chars, "yen_approx": round(chars / 1000.0 * 4.5, 1),
            "note": "ElevenLabs 従量課金の概算(実額はEL側実測)"}

    if not args.approved:
        _emit({"needs_approval": True, "voice": preset.get("label", args.voice),
               "pages": len(pages), "cost": cost,
               "note": "EL音声合成で課金が発生します。--approved を付けて再実行すると動画をビルドします。"})
        return

    print(f"… 動画をビルド中(EL音声={preset.get('label')}・静止+fade・字幕あり)", file=sys.stderr)
    # 古い動画は「今回の結果」として残さない。 ただし破棄は必ず承認ゲートより後で行う:
    #   承認前に消すと、 見積もりを見ただけのユーザーが代わりを得られないまま完成品を失う。
    _discard_stale_video(outdir)
    cid = reusable_job["cid"] if reusable_job else f"ss_{uuid.uuid4().hex[:8]}"
    vid = reusable_job["vid"] if reusable_job else f"v_{uuid.uuid4().hex[:8]}"
    slides = [{"id": sid, "png": str(png.resolve()), "narration": narrs.get(sid, "")}
              for sid, png, _ in pages]
    job = {"cid": cid, "vid": vid, "input_fingerprint": fingerprint, "state": "running", "title": title,
           "preset": {"id": preset["id"], "label": preset.get("label", preset["id"]),
                      "voice_id": preset["voice_id"], "gender": preset.get("gender", ""),
                      "seed": preset.get("seed", 42), "voice_params": preset.get("voice_params") or {}},
           "slides": slides, "pptx": None}
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        final = video_el._run_runner(job_path)
    except Exception:
        job["state"] = "failed"
        job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    if not final:
        job["state"] = "failed"
        job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit("動画ビルドに失敗しました(動画バックエンド runner)。stderr のログを確認してください。")
    job["state"] = "completed"
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    dest = outdir / "video.mp4"
    shutil.copy(final, dest)
    _emit({"ok": True, "video": str(dest), "voice": preset.get("label"),
           "pages": len(pages), "cost": cost})


def main() -> None:
    ap = argparse.ArgumentParser(prog="slidecast", description="Slidecast: スライド(deck.json)→解説動画(EL音声)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("voices", help="音声プリセット一覧")
    v.set_defaults(func=cmd_voices)
    b = sub.add_parser("build", help="deck.json → 動画(ナレ生成→概算→--approvedで合成)")
    b.add_argument("--deck", required=True, help="Slide の出力 deck.json のパス")
    b.add_argument("--voice", default=None, help="音声プリセット(voices で一覧。省略時は環境変数 DEFAULT_VOICE_PRESET)")
    b.add_argument("--approved", action="store_true", help="EL 課金を承認して動画をビルド")
    b.add_argument("--regen", action="store_true", help="ナレーションを作り直す(既定=narration.json を再利用してナレ固定)")
    b.set_defaults(func=cmd_build)

    pp = sub.add_parser("pptx", help="手持ちのPPTX/PDFを直接動画化(各ページ→vision ナレ→動画)")
    pp.add_argument("--file", required=True, help="PPTX/PDF ファイル")
    pp.add_argument("--voice", default=None, help="音声プリセット(voices で一覧。省略時は環境変数 DEFAULT_VOICE_PRESET)")
    pp.add_argument("--out", default="out", help="出力先ディレクトリ")
    pp.add_argument("--approved", action="store_true", help="EL 課金を承認して動画をビルド")
    pp.add_argument("--regen", action="store_true", help="ナレーションを作り直す(既定=narration.json を再利用してナレ固定)")
    pp.set_defaults(func=cmd_pptx)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
