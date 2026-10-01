"""Blogcast コア — 記事(URL/ブログ/テキスト) → 台本の型付き絵コンテ(storyboard) + 画風AI提案。

v2: スライド風テンプレをやめ、**動画バックエンドの画像生成(gen_slides=gpt-image)必須**。
各 scene は slide_prompt(自由な画づくりメモ)を持ち、画像は build_episode 側の gen_slides が生成する。
台本の型 = 動画バックエンド script_styles 7種。画風 = AIが記事から提案(自由文アートディレクション)。
"""
from __future__ import annotations
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
SLIDE_STUDIO = REPO / "slide-studio"
sys.path.insert(0, str(HERE.parents[1] / "core"))
from backend_path import resolve_backend_dir
VIDEO_BACKEND = resolve_backend_dir()
sys.path.insert(0, str(SLIDE_STUDIO / "engine"))
sys.path.insert(0, str(SLIDE_STUDIO / "api"))
sys.path.insert(0, str(VIDEO_BACKEND))

import ingest        # slide-studio: 取込
import audience      # slide-studio: 宛先
import llm           # slide-studio: claude CLI(課金ゼロ)
from formats.seminar import script_styles as _ss   # 動画バックエンド: 台本の型カタログ

STYLE_ORDER = ["owned_explainer_practical", "owned_explainer_story",
               "three_point", "pas", "bab", "curiosity", "listicle"]


def styles() -> list[dict]:
    out = []
    for k in STYLE_ORDER:
        v = _ss.SCRIPT_STYLES.get(k)
        if v:
            out.append({"id": k, "label": v["label"], "summary": v["summary"],
                        "when_to_use": v.get("when_to_use", "")})
    return out


def suggest_style(article: str, title: str = "") -> dict:
    """記事から台本の型をAI提案(理由付き・課金ゼロ)。"""
    catalog = "\n".join(f"- {s['id']} ({s['label']}): {s['summary']} 向く: {s['when_to_use']}"
                        for s in styles())
    prompt = f"""あなたは解説動画の構成ディレクターです。次の「記事」を1本の解説動画にするとき、
下の台本の型から最も向くものを1つ推奨してください(記事の動画化なので、迷えば記事忠実な owned_explainer 系)。
## 記事タイトル
{title}
## 台本の型 一覧
{catalog}
## 出力(JSONのみ)
{{"style":"<上のidを1つ>","label":"<日本語名>","reason":"なぜこの記事にこの型か1〜2文"}}
## 記事(冒頭)
{article[:4000]}
"""
    r = llm.generate_json(prompt, "")
    sid = r.get("style") if r.get("style") in _ss.SCRIPT_STYLES else "owned_explainer_practical"
    st = _ss.SCRIPT_STYLES[sid]
    return {"style": sid, "label": st["label"], "reason": r.get("reason", "記事に合わせて選びました。")}


def suggest_art(article: str, title: str = "") -> str:
    """記事から画風(gpt-image用の自由文アートディレクション)をAI提案(課金ゼロ)。
    動画バックエンドの auto_style_hint(題材に沿った画風のAI導出)と同思想。"""
    prompt = f"""次の記事を解説動画にします。スライド画像を gpt-image で生成するための「画風のアートディレクション」を、
記事の題材・トーンに合わせて2〜3文の日本語で書いてください(配色・質感・図の雰囲気・文字の扱い)。
制約: 実在ブランドの模倣や実在人物は指定しない。読みやすさ最優先(ごちゃごちゃさせない)。
出力は画風の文章のみ(前置き不要)。
## 記事タイトル
{title}
## 記事(冒頭)
{article[:3000]}
"""
    try:
        return (llm.generate(prompt, "") or "").strip() or _DEFAULT_ART
    except Exception:
        return _DEFAULT_ART


_DEFAULT_ART = "端正でモダンなフラットデザイン。落ち着いた配色と大きな余白、シンプルな図とアイコンで要点を1つずつ見せる。"

# ============ 圧縮度プリセット(Slide/Blog と同型の全文採用) ============
# 現行は単発生成=シーン数6〜14固定+16kクリップで長い記事を端折る。faithful は記事を章分割して
# 全論点をシーン化(上限なし)、condensed=20/essence=10 は上限を守って統合する。
_SCENE_SYS = "あなたは解説動画の構成作家です。記事に忠実に、JSONのみを返します。"
_PRESET_SCENES = {"essence": 10, "condensed": 20, "faithful": None}
_SCENE_RULES = (
    "## 厳守(接地)\n"
    "- 記事に実在する内容だけを使う。記事に無い数字・固有名詞・事例・日付を創作しない。\n"
    "- narration は話し言葉で各シーン2〜4文。文末の敬体/常体は宛先トーンに従う(指定が無ければです・ます)。スライドの見た目やページ番号を読み上げない。本文中の数量・割合・点数はアラビア数字で書く(例: 120件、30%、4.5点)。ただし『一組』『一次情報』など語彙の一部は漢字のままにする。\n"
    "- slide_prompt は要点を1枚で見せる画づくりメモ(50〜70字・主役の図/比喩を1つ・箇条書きの羅列や小さい文字の詰め込みはしない・画面内の日本語は見出し1つ+ラベル数語まで)。\n"
)


def _norm_scenes(raw) -> list[dict]:
    scenes = []
    for i, sc in enumerate(raw or []):
        if not isinstance(sc, dict):
            continue
        narr = str(sc.get("narration") or "").strip()
        if not narr:
            continue
        scenes.append({"id": f"scene-{i + 1:03d}",
                       "beat_role": str(sc.get("beat_role") or "").strip(),
                       "summary": str(sc.get("summary") or "").strip(),
                       "headline": str(sc.get("headline") or "").strip(),
                       "slide_prompt": str(sc.get("slide_prompt") or "").strip(),
                       "narration": narr})
    return scenes


def _chunk_article(text: str, max_chars: int = 2500) -> list[str]:
    paras = [p for p in (text or "").replace("\r", "").split("\n\n") if p.strip()]
    chunks, cur, n = [], [], 0
    for p in paras:
        if cur and n + len(p) > max_chars:
            chunks.append("\n\n".join(cur)); cur, n = [], 0
        cur.append(p); n += len(p)
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks or [text]


def _chunk_scenes(chunk: str, aud_block: str) -> tuple[list[dict], str | None]:
    prompt = ("次の「記事の一部」を、解説動画の本編シーン(絵コンテ)に落としてください。"
              "この部分の重要な論点・具体例・数値を漏れなくシーンにする(必要なだけ・上限なし。1シーン1論点で薄くしすぎない)。"
              "フックやまとめは付けない=本編シーンだけ。1シーン=スライド1枚+ナレーション。\n"
              + aud_block + _SCENE_RULES +
              '## 出力(JSONのみ)\n{"scenes":[{"beat_role":"本編","summary":"40字","headline":"8〜15字","slide_prompt":"...","narration":"2〜4文"}]}\n'
              "## 記事の一部\n" + chunk)
    try:
        scenes = _norm_scenes(llm.generate_json(prompt, _SCENE_SYS).get("scenes"))
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"
    if not scenes:
        return [], "有効な scenes を含むJSONが返りませんでした"
    return scenes, None


def _frame_scene(article: str, st: dict, aud_block: str, role: str) -> dict | None:
    what = "記事全体のフックとなる導入シーン" if role == "フック" else "記事全体を締めるまとめシーン"
    prompt = (f"次の記事の、{what}を1つ作ってください(台本の型「{st['label']}」の{role})。記事に無い事実は足さない。\n"
              + aud_block + _SCENE_RULES +
              '## 出力(JSONのみ)\n{"scene":{"beat_role":"' + role +
              '","summary":"40字","headline":"8〜15字","slide_prompt":"...","narration":"2〜4文"}}\n'
              "## 記事(要点)\n" + article[:6000])
    try:
        sc = llm.generate_json(prompt, _SCENE_SYS).get("scene")
    except Exception:
        sc = None
    return sc if (isinstance(sc, dict) and str(sc.get("narration") or "").strip()) else None


def _storyboard_faithful(article: str, st: dict, aud_block: str, title: str) -> dict:
    body = []
    failures = []
    for i, ch in enumerate(_chunk_article(article), 1):
        scenes, error = _chunk_scenes(ch, aud_block)
        if error:
            failures.append(f"chunk {i}: {error}")
        else:
            body += scenes
    if failures:
        raise RuntimeError("必須チャンクの絵コンテ生成に失敗しました: " + "; ".join(failures))
    opening = _frame_scene(article, st, aud_block, "フック")
    closing = _frame_scene(article, st, aud_block, "まとめ")
    scenes = _norm_scenes(([opening] if opening else []) + body + ([closing] if closing else []))
    return {"title": title or "解説動画", "scenes": scenes}


def build_storyboard(article: str, style_key: str, aud: dict | None, title: str,
                     preset: str | None = None) -> tuple[dict, dict]:
    """記事 + 台本の型 → storyboard {title, scenes:[{id,beat_role,summary,slide_prompt,narration}]}。
    preset: faithful=全シーン(章分割) / condensed=20 / essence=10 / None=6〜14。
    画像は作らない(slide_prompt は gen_slides への画づくりメモ)。ナレもここで確定(=固定)。"""
    st = _ss.SCRIPT_STYLES.get(style_key) or _ss.SCRIPT_STYLES["owned_explainer_practical"]
    aud_block = ""
    if aud:
        aud_block = ("\n## この動画の宛先(言葉づかいはこれに合わせる)\n"
                     + audience.describe(aud) + "\n" + audience.wording_rules(aud) + "\n")
    if preset == "faithful":                     # ①全シーン=記事を章分割して全論点をシーン化(上限なし)
        return _storyboard_faithful(article, st, aud_block, title), st
    _target = _PRESET_SCENES.get(preset)         # ③essence=10 / ②condensed=20 / None=6〜14
    if _target:
        scene_line = (f"- シーン数は{_target}前後(最大{_target})。要点を統合してよいが重要な論点・具体例・数値を落とさない。"
                      "骨格の順を守る(フックで始まりまとめで終える)。")
    else:
        scene_line = ("- シーン数は記事の情報量に合わせる(目安6〜14。短い記事を水増ししない・深い記事を無理に圧縮しない)。"
                      "骨格の順を守る(フックで始まりまとめで終える)。")
    prompt = f"""記事を、下の「台本の型」の骨格に沿って、解説動画の絵コンテ(scenes)に落としてください。
1シーン=スライド1枚+ナレーション。画像はあとで画像AIが slide_prompt から自由に描きます。

## 台本の型: {st['label']}
{st['summary']}
### 骨格(この流れで scenes を並べる。beat_role はこの流れの節名)
{st['arc']}
### 各節の作り方
{st['beats_guide']}
{aud_block}
## 厳守(接地)
- 記事に実在する内容だけを使う。**記事に無い数字・固有名詞・事例・日付を創作しない**。
- narration は話し言葉で各シーン2〜4文。文末の敬体/常体は上記「この動画の宛先」のトーン指定に従う(指定が無ければです・ます)。スライドの見た目やページ番号を読み上げない。本文中の数量・割合・点数はアラビア数字で書く(例: 120件、30%、4.5点)。ただし「一組」「一次情報」など語彙の一部は漢字のままにする。
- slide_prompt は「その1シーンの要点を1枚で見せる画づくりメモ」(50〜70字。主役の図・比喩を1つ。
  箇条書きの羅列や小さい文字の詰め込みは書かない。画面内の日本語テキストは見出し1つ+ラベル数語まで)。
{scene_line}

## 出力(JSONのみ・前後に地の文や```を付けない)
{{"title":"<動画タイトル>","scenes":[
  {{"id":"scene-001","beat_role":"フック","summary":"このシーンで何を見せるか(40字)",
    "headline":"画像に入れる視聴者向けの短い見出し(8〜15字。節名『フック』等の内部用語は禁止)",
    "slide_prompt":"画づくりメモ","narration":"ナレーション(2〜4文)"}}
]}}

## 記事
{article[:40000]}
"""
    sb = llm.generate_json(prompt, _SCENE_SYS)
    scenes = _norm_scenes(sb.get("scenes"))
    if _target and len(scenes) > _target:        # 上限超過はまとめ(最終シーン)を残して切る
        scenes = scenes[:_target - 1] + scenes[-1:]
    if not scenes:
        raise SystemExit("絵コンテの生成に失敗しました(scenes が空)")
    return {"title": str(sb.get("title") or title or "解説動画").strip(), "scenes": scenes}, st


def load_source(file: str | None, url: str | None, text: str | None) -> tuple[str, str]:
    if url:
        return ingest.from_url(url), url
    if text:
        return ingest.from_text(text), "記事"
    if file:
        p = pathlib.Path(file)
        if not p.exists():
            raise SystemExit(f"ファイルが見つかりません: {p}")
        if p.suffix.lower() == ".json":
            import json
            # 人が渡すファイル。BOM 付きで保存されていても読めるようにする
            # (utf-8-sig は BOM が無ければ utf-8 と同じ挙動)。
            d = json.loads(p.read_text(encoding="utf-8-sig"))
            body = d.get("markdown") or d.get("source_text") or ""
            if not body and d.get("struct"):
                body = _struct_to_text(d["struct"])
            return ingest.from_text(body or p.read_text(encoding="utf-8-sig")), (d.get("title") or p.stem)
        return ingest.from_file(p), p.stem
    raise SystemExit("記事がありません(--url / --file article.json / --text)")


def _struct_to_text(struct: dict) -> str:
    parts = [struct.get("title", "")]
    parts += struct.get("lead") or []
    for sec in struct.get("sections") or []:
        parts.append(sec.get("heading", ""))
        parts += sec.get("paragraphs") or []
    summ = struct.get("summary") or {}
    parts.append(summ.get("heading", ""))
    parts += summ.get("paragraphs") or []
    return "\n".join(p for p in parts if p)
