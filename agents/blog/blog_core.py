"""Blog Agent のコア — 動画バックエンドのブログ生成モジュール(blog.py) のロジックを移植 + 拡張。

元 blog.py は「完成動画(scenes.json)→読み物」の逆向き。ここでは video/scenes 依存を剥がし、
「元ネタ(記事/資料/メモ)→ 読み物ブログ」の順方向に転用する。移植 + 追加した資産:
  - 構造化スキーマ(title/lead/sections/summary)
  - 長文SEOの品質ルール / 品質ゲート(再述圧縮 / 焦らし解消 / 捏造除去) / タイポ正規化
  - HTML/Markdown 組立(+ 差し込み画像)
  - ★追加: 記事の型(blog_structures) / 画風×型のAI提案(recommend) / セクション図解の gpt-image 生成
LLM・宛先・画風・gpt-image描画は slide-studio の部品(llm/audience/themes/render_ai)を共有。
"""
from __future__ import annotations
import json
import logging
import pathlib
import re
from html import unescape
from urllib.parse import urlparse

import llm            # slide-studio/api/llm.py (claude CLI・課金ゼロ)
import audience       # slide-studio/api/audience.py (宛先=言葉づかい)
import themes as themes_mod   # slide-studio/engine/themes.py (画風6種)
import render_ai      # slide-studio/api/render_ai.py (gpt-image描画・render_deck_ai)
import organize       # slide-studio/api/organize.py (記事セクション→構造化スライド)
import blog_structures        # ブログの型
import rebuild_policy
import rebuild_policy as rebuild_policy_module


_LOG = logging.getLogger(__name__)
_FAITHFUL_CHUNK_MAX_ATTEMPTS = 3  # 初回 + 一時的な不正JSONへの再生成2回
# Gate B は、元記事の見出しの30%以上が消えると目次から辿れない公開物になるため止める。
_SOURCE_HEADING_RETENTION_RATIO = rebuild_policy.SOURCE_HEADING_RETENTION_RATIO
_MARKDOWN_HEADING_LINE_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)


# ============================ プロンプト ============================
BLOG_SYSTEM = """あなたは日本語のWebメディア編集者です。渡された元ネタ(記事・資料・メモ)を、そのまま読んで理解できる「読み物」としてのブログ記事に再構成します。

# 絶対ルール(逸脱厳禁)
- 具体ファクトの捏造禁止: 元ネタに無い具体的な数字・統計・固有名詞・企業名・調査結果・日付・実在の事例を新造しない(もっともらしくても不可)。これらは後段の捏造ゲートで機械的に除去される。
- 読み物として構成する: 導入リード → 見出し付きの本文セクション → まとめ。各セクションは内容を表す見出しを付ける。
- ★導入(lead)の最初の文は自己完結させる: 前段のない逆接・順接の接続詞(ですが/しかし/でも/だが/そして/また 等)で書き出さない。読者の状況や一般認識を一文で示してから展開する。
- 必要な章は小見出し(H3)で分割してよい。不要ならsubsectionsを出さず、section本文だけにする。
- ★見出し(section/subsection/FAQの質問)を立てたら必ず本文(paragraphs=回答)を書く。見出しだけで本文が空の項目を作らない。FAQは各質問に必ず答えの段落を付ける。
- 読みやすさ: 1段落は2〜4文程度。長い列挙は箇条書き(paragraphを"- "始まりの行にする)にしてよい。
- ★文末を統一する: です・ます調 と 常体(断定・だ/である)を1本の記事で混在させない。宛先トーンに合わせて全文で揃える。
- 句読点の直後に半角スペースを入れない。日本語文中の記号は全角(？！：)。

# 出力形式(厳守: 下記スキーマのJSONだけを返す。前置き・後置き・```不要)
{
  "title": "記事タイトル(32字前後、内容を表すSEO見出し)",
  "lead": ["導入リード段落1", "導入リード段落2"],
  "sections": [
    {"heading": "セクション見出し", "paragraphs": ["本文段落", "本文段落"],
     "subsections": [{"heading": "必要な場合だけ使う小見出し", "paragraphs": ["本文段落"]}]}
  ],
  "summary": {"heading": "まとめ", "paragraphs": ["まとめ段落"]},
  "meta_description": "本文を忠実に要約した120〜160字の説明文。主キーワードを含め、煽らない。"
}
"""

MODE_DIRECTIVE = {
    "faithful": (
        "# モード: 元ネタ準拠(肉付けなし)\n"
        "- 元ネタの情報だけで再構成する。情報の追加はしない。\n"
    ),
    "compress": (
        "# モード: 要点に凝縮(元ネタだけを根拠に、約{target}字へ要約再構成)\n"
        "- 元ネタの重要な論点・具体例・数値を優先して残し、繰り返しや冗長な言い回しを畳んで本文全体で約{target}字にする。\n"
        "- 情報の追加(一般知識の肉付け)はしない。元ネタに無い数字・固有名詞・事例を創作しない(後段の捏造ゲートで除去)。\n"
        "- 見出しが立てた問い・約束には、そのsectionの中で答える(焦らして後送りにしない)。日本語文中の記号は全角(？！：)。\n"
    ),
    "seo_long": (
        "# モード: 長文SEO(元ネタを骨子に、根拠のある内容を構造化して長尺化)\n"
        "- 元ネタの主張を骨子・切り口として活かし、全体で本文{target}字前後の網羅的なSEO記事にする。\n"
        "- ★具体的な主張(数値・割合・法令・日付・金額・固有名詞・因果・比較評価・手続)は、必ず元ネタに\n"
        "  根拠がある内容だけを書く。根拠のない一般断定(制度の目的の推測、『早めが得』等の効用の断定、\n"
        "  一般的助言)は最小限にし、事実として断定しない。枠組み的な導入・平易化は可。\n"
        "- sectionを十分に分割して見出しを増やし、各sectionも複数段落で掘り下げる。導入は課題提起を厚く、\n"
        "  末尾にFAQ的sectionとまとめを置く。長い列挙は箇条書き('- '始まり)にする。\n"
        "- 元ネタに無い具体ファクトは創作しない。後段の捏造・再述・焦らしゲートは維持される。\n"
        "\n# 品質ルール(厳守)\n"
        "- ★見出しが立てた問い・約束には、そのsectionの中で答える。焦らして別テーマへ飛ばさない\n"
        "  (「この後明かします」「詳しくは後で」等の引っ張りは禁止)。\n"
        "- ★記事の主役となる固有名詞・専門用語(ツール名・サービス名・概念)は初出で1文のやさしい補足説明を付ける。\n"
        "- 同じ主張・コツの再述は『導入+本文+まとめ』の3回まで。『順番/注意』節やFAQで同じ内容を繰り返さない。\n"
        "- h2見出しには読者が検索しそうなキーワードを入れる(体言止め or 問いかけ、内容を表す)。\n"
        "- 必要な章は小見出し(H3)で分割してよい。小見出しを使わない章は従来どおりsection本文だけでよい。\n"
        "- 手順・操作系の話題では可能なら具体例を1つ示す(ただし具体的な数値・固有名詞の創作はしない)。\n"
        "- 因果を元ネタより強く断定しない。日本語文中の記号は全角(？！：)。\n"
    ),
}


def build_prompt(title: str, source_text: str, aud: dict | None, mode: str, target: int,
                 structure: dict | None = None) -> str:
    lines = [MODE_DIRECTIVE.get(mode, MODE_DIRECTIVE["faithful"]).format(target=target), ""]
    if structure and structure.get("chapters"):
        chs = structure["chapters"]
        lines += ["# 記事の型(この章立ての流れで書く。元ネタに内容が無い章は飛ばし、足りなければ分割してよい)",
                  f"型: {structure.get('name', '')} ／ {structure.get('framework', '')}",
                  "\n".join(f"{i + 1}. {c}" for i, c in enumerate(chs)), ""]
    if aud:
        lines += ["# この記事の宛先・目的(言葉づかいはこれに合わせる — 最優先)",
                  audience.describe(aud), audience.wording_rules(aud), ""]
    lines += [f"# 希望タイトル(参考)\n{title or '(元ネタから適切に決める)'}", "",
              "# 元ネタ(これだけを根拠にする)", source_text[:40000], "",
              "上記を読み物として再構成し、指定スキーマのJSONだけを返してください。"]
    return "\n".join(lines)


def rewrite(title: str, source_text: str, aud: dict | None, mode: str, target: int,
            structure: dict | None = None) -> dict:
    art = llm.generate_json(build_prompt(title, source_text, aud, mode, target, structure), BLOG_SYSTEM)
    if not isinstance(art, dict):
        raise SystemExit("記事JSONがdictで返りませんでした")
    return art


# ============================ 忠実モード(全量保持・プリセット①②) ============================
# 現行 generate() は凝縮スキーマ=どうしても短くなる。ここは元ネタの全量を保つため、
# 元ネタをチャンク分割 → 各塊を1対1で言い換え(要約・削除・追加禁止) → 結合する別経路。
# ①=原文の順序を維持 / ②=言い換え済みセクションを型に沿って並べ替え(段落は書き換えない)。
FAITHFUL_SYSTEM = "あなたは日本語の丁寧な編集者です。指示に厳密に従い、要約や省略をせず、JSONのみを返します。"

_HEADING_RE = re.compile(r"^(第[0-9０-９一二三四五六七八九十]+章|#{1,6}\s)")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _looks_heading(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    if _HEADING_RE.match(s):
        return True
    return len(s) <= 28 and "。" not in s and (s.endswith("とは") or "：" in s or ":" in s)


def _chunk_source(text: str, max_chars: int = 3000) -> list[str]:
    """段落境界で貪欲にまとめ、章見出しの直前で区切る(章をなるべく壊さない)。"""
    paras = [p for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for p in paras:
        head_line = p.strip().splitlines()[0]
        is_head = _looks_heading(head_line)
        if cur and (cur_len + len(p) > max_chars or (is_head and cur_len > max_chars * 0.5)):
            chunks.append("\n\n".join(cur)); cur = []; cur_len = 0
        cur.append(p); cur_len += len(p)
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks or [text]


def _json_object_from_raw(raw: str) -> str | None:
    """フェンス外の説明を捨て、文字列を意識して最初の JSON object を切り出す。"""
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start < 0:
        return None
    depth, quoted, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    # 不正な文字列引用符があっても、最後の } までは修復対象にする。
    end = text.rfind("}")
    return text[start:end + 1] if end > start else None


def _relax_json_string_errors(value: str) -> str:
    """制御文字と、値の途中に明白に混じった引用符だけを最小限エスケープする。"""
    out: list[str] = []
    quoted, escaped = False, False
    for index, char in enumerate(value):
        if not quoted:
            out.append(char)
            if char == '"':
                quoted = True
            continue
        if escaped:
            out.append(char)
            escaped = False
            continue
        if char == "\\":
            out.append(char)
            escaped = True
            continue
        if char in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[char])
            continue
        if ord(char) < 0x20:
            out.append(f"\\u{ord(char):04x}")
            continue
        if char == '"':
            following = value[index + 1:]
            next_nonspace = next((c for c in following if not c.isspace()), "")
            # JSON の終端でない引用符（例: 「"失敗"した」）は文字列内の引用とみなす。
            if next_nonspace and next_nonspace not in ",}]:":
                out.append('\\"')
            else:
                out.append(char)
                quoted = False
            continue
        out.append(char)
    return "".join(out)


def _coerce_sections_json(raw: str) -> dict | None:
    """faithful rebuild 専用の JSON 緩和パーサ。共有 llm の既存挙動は変えない。"""
    candidate = _json_object_from_raw(raw)
    if not candidate:
        return None
    for value in (candidate, re.sub(r",(\s*[}\]])", r"\1", _relax_json_string_errors(candidate))):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_faithful_sections_json(raw: str) -> tuple[dict | None, str | None]:
    """直接成功と緩和成功を診断用に区別する。"""
    candidate = _json_object_from_raw(raw)
    if candidate:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed, "parsed_direct"
        except json.JSONDecodeError:
            pass
    parsed = _coerce_sections_json(raw)
    return (parsed, "parsed_coerced") if parsed is not None else (None, None)


def _tone_block(aud: dict | None) -> str:
    # 全チャンク共通で文体を固定する(独立リライトによる文末の揺れを防ぐ)
    block = ("# 文体(全チャンク通して厳密に統一する — 最優先)\n"
             "- ★文末を統一する: です・ます調 と 常体(断定・だ/である)を1本の記事で混在させない。どちらかに全文で揃える。\n")
    if aud:
        block += audience.describe(aud) + "\n" + audience.wording_rules(aud) + "\n"
    else:
        block += "- 指定が無ければ です・ます調 で統一する。\n"
    return block


def _jargon_rules() -> dict[str, dict]:
    """POC の同梱規則。既存(off)経路では呼ばない。"""
    return rebuild_policy.JARGON_RULES


def _faithful_chunk_prompt(chunk: str, aud: dict | None = None, *,
                           jargon_rules: dict | None = None) -> str:
    return (
        "次の日本語テキストの塊を、内容・順序・具体例・数値・固有名詞を一切変えずに、言い回しだけ整えて"
        "書き直し、見出し付きセクションのJSONにしてください。\n"
        + _tone_block(aud) +
        "# 絶対ルール\n"
        "- 要約・圧縮・省略・情報の追加・繰り返しの新設は禁止。段落数・具体例・数値は元のまま、ほぼ同じ長さにする。\n"
        "- テキスト内に見出し行(『第N章：…』『◯◯とは』等の短い行)があればそれを heading にする。\n"
        "- 塊の冒頭が前の話の続きで新しい見出しが無いなら、最初のsectionのheadingは空文字 \"\" にする。\n"
        "- 箇条書き(『・』区切り)は各項目を『- 』始まりの別段落にしてよい。句読点の直後にスペースを入れない。\n"
        "- 一人称や語り口の個性は残しつつ、文末だけは上記トーンに統一する。\n"
        + ("- Markdown画像行（![alt](path)）は画像を生成・削除・移動せず、必ず一行を一つの paragraphs 要素として残す。alt は読者向け用語規則に従って言い換えてよい。\n" if jargon_rules else "")
        + (rebuild_policy.faithful_jargon_prompt() + "\n" if jargon_rules else "")
        + '# 出力(JSONのみ)\n{"sections":[{"heading":"見出し","paragraphs":["段落", "段落"]}]}\n'
        "# テキストの塊\n" + chunk
    )


def _sections_from_payload(payload: dict | None) -> list[dict]:
    """faithful の許容スキーマだけを本文用 section に正規化する。"""
    if not isinstance(payload, dict):
        return []
    secs: list[dict] = []
    for s in (payload.get("sections") or []):
        if not isinstance(s, dict):
            continue
        h = str(s.get("heading") or "").strip()
        ps = [str(p).strip() for p in (s.get("paragraphs") or []) if str(p).strip()]
        if ps or h:
            secs.append({"heading": h, "paragraphs": ps})
    return secs


def _faithful_chunk_sections(chunk: str, aud: dict | None = None, *,
                             jargon_rules: dict | None = None) -> list[dict]:
    """従来/off 経路の generate_json 呼び出しをそのまま残す。"""
    r = llm.generate_json(_faithful_chunk_prompt(chunk, aud, jargon_rules=jargon_rules), FAITHFUL_SYSTEM)
    return _sections_from_payload(r)


def _faithful_plaintext_sections(chunk: str, aud: dict | None, *, jargon_rules: dict) -> list[dict]:
    """JSON を使わない最後の生成経路。見出しはローカルで構造化して保持する。"""
    # この最終フォールバックは、推測的な短行ではなく元Markdownの見出しだけを
    # 構造として復元する。これにより本文段落へ潰れても見出し数は失われない。
    source_headings = [match.group(1).strip() for line in chunk.splitlines()
                       if (match := _MARKDOWN_HEADING_LINE_RE.match(line.strip()))]
    prompt = (
        "次の日本語テキストを、内容・順序・具体例・数値・固有名詞を一切変えず、言い回しだけ整えて返してください。"
        "記事本文だけを返し、指示文・補足・メタ発言・自己言及は一切書きません。要約・省略・追加は禁止です。"
        "「」で囲まれた直接引用は一字一句そのまま保持します。元のMarkdown見出し行は内容・順序・階層を保ち、"
        "必ず `## 見出し` の一行として残します。見出しを通常段落へ変えてはいけません。"
        "それ以外の段落は空行で区切ります。\n"
        + _tone_block(aud) + rebuild_policy.faithful_jargon_prompt()
        + ("\n# この塊で必ず見出しとして残す行\n" + "\n".join(source_headings) if source_headings else "")
        + "\n# テキストの塊\n" + chunk
    )
    text = (llm.generate(prompt, FAITHFUL_SYSTEM) or "").strip()
    # fallback だけの軽量な自己修復。検出された段落全体を落とすため、誤って
    # 指示を本文として公開しない。固定句のみの検出なので通常本文は対象にしない。
    blocks = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    blocks = [p for p in blocks if not rebuild_policy.scan_meta_leakage(p)]
    sections: list[dict] = []
    current: dict = {"heading": "", "paragraphs": []}
    for block in blocks:
        heading = re.fullmatch(r"\s*#{2,3}\s+(.+?)\s*", block)
        # モデルが記法を落としても、入力で検出済みの見出しだけは段落にしない。
        if not heading and block in source_headings:
            heading = re.fullmatch(r"(.+)", block)
        if heading:
            if current["heading"] or current["paragraphs"]:
                sections.append(current)
            current = {"heading": heading.group(1).strip(), "paragraphs": []}
        else:
            current["paragraphs"].append(block)
    if current["heading"] or current["paragraphs"]:
        sections.append(current)
    return _restore_plaintext_source_headings(chunk, sections)


def _restore_plaintext_source_headings(chunk: str, sections: list[dict]) -> list[dict]:
    """分割フォールバックで元Markdown見出しを必ずsectionへ戻す。

    プレーン出力は見出し記法を落とすことがあるため、出力側の見出し検出には
    依存しない。元チャンクの見出しごとの本文量に合わせ、整形済み段落を順に
    割り当てる。段落が不足する極端な応答では対応する元本文を使い、見出しだけ
    の空sectionを作らない。
    """
    source_sections: list[dict] = []
    current: dict | None = None
    for raw_line in chunk.splitlines():
        match = _MARKDOWN_HEADING_LINE_RE.match(raw_line.strip())
        if match:
            current = {"heading": match.group(1).strip(), "source_paragraphs": []}
            source_sections.append(current)
        elif current is not None and raw_line.strip():
            current["source_paragraphs"].append(raw_line.strip())
    if not source_sections:
        return sections

    # モデルが出した本文は捨てず、元の章順に再配分する。見出し本文そのものは
    # paragraph として二重に出さない。
    paragraphs = [str(p).strip() for sec in sections for p in sec.get("paragraphs") or [] if str(p).strip()]
    fallback_bodies = ["\n".join(sec["source_paragraphs"]).strip() for sec in source_sections]
    weights = [max(1, len(sec["source_paragraphs"])) for sec in source_sections]
    total_weight = sum(weights)
    restored: list[dict] = []
    cursor = 0
    for index, sec in enumerate(source_sections):
        # 最終章までに全段落を使い切る丸め。各章に本文を残すことを優先する。
        remaining_sections = len(source_sections) - index - 1
        remaining_paragraphs = len(paragraphs) - cursor
        wanted = round(len(paragraphs) * weights[index] / total_weight)
        take = min(max(1, wanted), max(0, remaining_paragraphs - remaining_sections))
        assigned = paragraphs[cursor:cursor + take]
        cursor += len(assigned)
        if not assigned and fallback_bodies[index]:
            assigned = [fallback_bodies[index]]
        if assigned:
            restored.append({"heading": sec["heading"], "paragraphs": assigned})
    # 見出しのない前置き等、配分で余った本文は最後の有効sectionへ保持する。
    if cursor < len(paragraphs) and restored:
        restored[-1]["paragraphs"].extend(paragraphs[cursor:])
    return restored or sections


def _normalise_quote_wrappers(text: str) -> str:
    return str(text or "").translate(str.maketrans({"「": '"', "」": '"', "“": '"', "”": '"'}))


def check_quote_preservation(source_text: str, output_markdown: str) -> list[dict]:
    """元記事の引用を照合し、単独行の会話引用だけを binding とする。

    技術記事で文中に使われる「概念語」や「問いかけ」は通常の言い換え対象なので、
    欠落していても記録するだけで公開を止めない。
    """
    source = str(source_text or "")
    output = _normalise_quote_wrappers(output_markdown)
    findings: list[dict] = []
    for match in re.finditer(r"「([^「」]+)」", source):
        quote = match.group(1)
        if _normalise_quote_wrappers(f'「{quote}」') not in output:
            line_start = source.rfind("\n", 0, match.start()) + 1
            line_end = source.find("\n", match.end())
            if line_end < 0:
                line_end = len(source)
            # 発話は通常、鉤括弧だけを独立段落にする。文中の「…」は binding にしない。
            binding = source[line_start:line_end].strip() == match.group(0)
            findings.append({"type": "quote_missing_or_changed", "quote": quote,
                             "start": match.start(), "context": match.group(0),
                             "binding": binding,
                             "severity": "BLOCK" if binding else "INFO"})
    return findings


_JAPANESE_PARTICLE_RE = re.compile(r"[はがをにへとでのもやからよりまで]")


def _bare_heading_suspects(markdown: str) -> list[dict]:
    """Markdown 記法を失った短い見出し候補を WARN 用に拾う。"""
    lines = markdown.splitlines()
    findings: list[dict] = []
    in_fence = False
    for index, raw in enumerate(lines):
        text = raw.strip()
        if text.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not text or index == 0 or index + 1 >= len(lines):
            continue
        if lines[index - 1].strip() or lines[index + 1].strip():
            continue
        if (len(text) > 40 or text.endswith(("。", "、", "，", "！", "？", "!", "?", "…", "."))
                or _JAPANESE_PARTICLE_RE.search(text)):
            continue
        if (re.match(r"^(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>|!\[|\[)", text)
                or any(token in text for token in ("`", "|", "<", ">"))):
            continue
        findings.append({"type": "bare_heading_suspect", "severity": "WARN",
                         "line": index + 1, "text": text})
    return findings


def check_heading_toc(article_or_markdown, source_text: str | None = None) -> list[dict]:
    """最終Markdownの見出し数・目次・構造化見出しを照合する。

    source_text があるnon-off rebuildでは、元Markdown見出し数との比もGate Bにする。
    """
    article = article_or_markdown if isinstance(article_or_markdown, dict) else None
    markdown = (article.get("markdown") if article and "markdown" in article
                else assemble(article)["markdown"] if article else article_or_markdown) or ""
    markdown = str(markdown)
    heading_matches = list(re.finditer(r"^#{2,6}\s+(.+?)\s*$", markdown, re.M))
    headings = {m.group(1).strip() for m in heading_matches}
    findings: list[dict] = []
    source_heading_count = len(_MARKDOWN_HEADING_LINE_RE.findall(str(source_text or "")))
    output_heading_count = sum(
        1 for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", markdown, re.M)
        if match.group(1).strip() != "目次"
    )
    # S <= 1 は題名だけ等で比率が不安定なため対象外。固定CTA等で増えた出力は
    # 分母へ足さず、元見出しが3割以上失われた時だけBLOCKする。
    if source_heading_count > 1 and output_heading_count < source_heading_count * _SOURCE_HEADING_RETENTION_RATIO:
        findings.append({"type": "source_heading_count_drop", "severity": "BLOCK",
                         "source_count": source_heading_count, "output_count": output_heading_count,
                         "threshold": _SOURCE_HEADING_RETENTION_RATIO})
    if article and "markdown" in article:
        expected = []
        for sec in article.get("struct", article).get("sections", []):
            if str(sec.get("heading") or "").strip(): expected.append(str(sec["heading"]).strip())
            for sub in sec.get("subsections", []):
                if str(sub.get("heading") or "").strip(): expected.append(str(sub["heading"]).strip())
        for heading in expected:
            if heading not in headings:
                findings.append({"type": "structured_heading_missing", "heading": heading,
                                 "severity": "WARN"})
    toc_match = re.search(r"^##\s+目次\s*$([\s\S]*?)(?=^##\s+|\Z)", markdown, re.M)
    if toc_match:
        toc_headings = re.findall(r"^\s*-\s+\[([^\]]+)\]\([^)]*\)\s*$", toc_match.group(1), re.M)
        toc_h2_headings = re.findall(r"^-\s+\[([^\]]+)\]\([^)]*\)\s*$", toc_match.group(1), re.M)
        body_h2_headings = [m.group(1).strip() for m in re.finditer(r"^##\s+(.+?)\s*$", markdown, re.M)
                            if m.group(1).strip() not in {"目次", "出典"}]
        if len(toc_h2_headings) != len(body_h2_headings):
            findings.append({"type": "toc_h2_count_mismatch", "severity": "BLOCK",
                             "toc_count": len(toc_h2_headings), "h2_count": len(body_h2_headings),
                             "toc_headings": toc_h2_headings, "h2_headings": body_h2_headings})
        for heading in toc_headings:
            if heading.strip() not in headings:
                findings.append({"type": "toc_target_missing", "heading": heading.strip(),
                                 "severity": "BLOCK"})
        for heading in headings - {"目次", "出典"}:
            if heading not in toc_headings:
                findings.append({"type": "toc_entry_missing", "heading": heading,
                                 "severity": "BLOCK"})
    return findings + _bare_heading_suspects(markdown)


def _translate_image_alt_jargon(article: dict) -> None:
    """画像参照先は不変のまま、非 off rebuild の alt だけを固定規則で平易化する。"""
    replacements: list[tuple[str, str]] = []
    for term, rule in rebuild_policy.JARGON_RULES.items():
        replacements.append((term, str(rule["replacement"])))
        replacements.extend((str(alias), str(rule["replacement"])) for alias in rule.get("aliases", []))
    replacements.sort(key=lambda item: -len(item[0]))

    def replace(match: re.Match) -> str:
        alt, path = match.group(1), match.group(2)
        for source, target in replacements:
            alt = alt.replace(source, target)
        return f"![{alt}]({path})"

    for paras, index in iter_para_slots(article):
        paras[index] = _MARKDOWN_IMAGE_RE.sub(replace, paras[index])


def _restore_missing_source_images(article: dict, source_text: str) -> list[str]:
    """faithful LLM が落とした元記事の画像参照を回収する。パス単位で重複させない。"""
    source_images = [match.group(0) for match in _MARKDOWN_IMAGE_RE.finditer(source_text or "")]
    existing_paths = {match.group(2) for match in _MARKDOWN_IMAGE_RE.finditer(article_plaintext(article))}
    missing = [image for image in source_images if _MARKDOWN_IMAGE_RE.search(image).group(2) not in existing_paths]
    if missing:
        sections = article.setdefault("sections", [])
        if not sections:
            sections.append({"heading": "", "paragraphs": []})
        sections[0].setdefault("paragraphs", []).extend(missing)
    return missing


def _visible_markdown(md: str) -> str:
    """読者に見えない画像/リンクの URL 部分を除き、alt / リンク文字列だけを残す。
    URL 片(例 images/sample.png)への禁止語部分一致による誤検出を防ぐ。"""
    md = _MARKDOWN_IMAGE_RE.sub(lambda m: m.group(1), md or "")          # ![alt](url) -> alt
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)                     # [text](url) -> text
    return md


def deterministic_jargon_gate(article_or_markdown) -> dict:
    """最終公開物の固定内部語ゲート。本文、見出し、画像 alt、可視属性文字列を走査する。"""
    if isinstance(article_or_markdown, dict):
        assembled = assemble(article_or_markdown)
        html = assembled["html"]
        visible_attrs = re.findall(r'(?:alt|title|aria-label)="([^"]*)"', html, flags=re.I)
        html_text = _visible_markdown(unescape(re.sub(r"<[^>]+>", " ", html)))
        text = "\n".join((_visible_markdown(assembled["markdown"]), html_text, *visible_attrs))
    else:
        text = _visible_markdown(str(article_or_markdown or ""))
    hits = rebuild_policy.scan_banned(text)
    return {"ran": True, "judgment": "BLOCK" if hits else "PASS", "blocked": bool(hits),
            "hits": hits, "scanned_chars": len(text)}


def audit_jargon(article_md: str) -> dict:
    """記事本文だけを入力にする、固定契約の独立用語監査。"""
    raw = llm.generate_json(rebuild_policy.AUDIT_JARGON_PROMPT.format(article_md=article_md), "")
    return rebuild_policy.validate_audit_payload(raw, article_md)


def _apply_revision(article: dict, revision: dict, source_text: str) -> tuple[bool, str]:
    quote = str(revision.get("quote") or "")
    replacement = str(revision.get("replacement") or "")
    evidence = str(revision.get("source_evidence") or "")
    if revision.get("status") != "revised":
        return False, "cannot_revise"
    if not quote or not replacement or not evidence or evidence not in source_text:
        return False, "根拠または置換内容が不正です"
    matches = list(re.finditer(re.escape(quote), article_plaintext(article)))
    if len(matches) != 1:
        return False, "skipped_ambiguous"
    for paras, i in iter_para_slots(article):
        if quote in paras[i]:
            if paras[i].count(quote) != 1:
                return False, "skipped_ambiguous"
            paras[i] = paras[i].replace(quote, replacement, 1)
            return True, "revised"
    return False, "quote が段落に存在しません"


def revise_findings(article: dict, findings: list[dict], source_text: str, *,
                    jargon_rules: dict) -> tuple[dict, dict]:
    """finding ごとに唯一の完全一致 quote だけを局所置換する。"""
    log = {"applied": [], "skipped": []}
    for finding in findings:
        quote = str(finding.get("quote") or "")
        if article_plaintext(article).count(quote) != 1:
            log["skipped"].append({"finding": finding, "reason": "skipped_ambiguous"}); continue
        prompt = rebuild_policy.REVISE_FINDINGS_PROMPT.format(
            rules=rebuild_policy.faithful_jargon_prompt(), source_text=source_text,
            quote=quote, finding=json.dumps(finding, ensure_ascii=False))
        parsed = llm.generate_json(prompt, "")
        revisions = parsed.get("revisions") if isinstance(parsed, dict) else []
        revision = revisions[0] if isinstance(revisions, list) and revisions and isinstance(revisions[0], dict) else {}
        applied, reason = _apply_revision(article, revision, source_text)
        record = {"finding": finding, "revision": revision, "reason": reason}
        (log["applied"] if applied else log["skipped"]).append(record)
    return article, log


def audit_important_facts(article_md: str, checklist: dict) -> dict:
    prompt = rebuild_policy.FACT_AUDIT_PROMPT.format(
        checklist=json.dumps(checklist, ensure_ascii=False), article_md=article_md)
    parsed = llm.generate_json(prompt, "")
    return parsed if isinstance(parsed, dict) else {"error": "重要事実監査のJSONが不正です", "findings": []}


def diagnose_structure(article_md: str, outline: dict) -> dict:
    prompt = rebuild_policy.STRUCTURE_DIAGNOSIS_PROMPT.format(
        outline=json.dumps(outline, ensure_ascii=False), article_md=article_md)
    parsed = llm.generate_json(prompt, "")
    return parsed if isinstance(parsed, dict) else {"error": "構造診断のJSONが不正です", "findings": []}


def revise_ledger_findings(article: dict, findings: list[dict], source_text: str, *,
                           checklist: dict) -> tuple[dict, dict]:
    """台帳の欠落・変質指摘を、元記事根拠付きの局所修正に限定する。"""
    log = {"applied": [], "skipped": []}
    for finding in findings:
        if finding.get("status") not in {"missing", "changed"}:
            continue
        prompt = rebuild_policy.LEDGER_REVISE_PROMPT.format(
            source_text=source_text, checklist=json.dumps(checklist, ensure_ascii=False),
            finding=json.dumps(finding, ensure_ascii=False))
        parsed = llm.generate_json(prompt, "")
        revisions = parsed.get("revisions") if isinstance(parsed, dict) else []
        revision = revisions[0] if isinstance(revisions, list) and revisions and isinstance(revisions[0], dict) else {}
        applied, reason = _apply_revision(article, revision, source_text)
        record = {"finding": finding, "revision": revision, "reason": reason}
        (log["applied"] if applied else log["skipped"]).append(record)
    return article, log


def _merge_sections(sections: list[dict]) -> list[dict]:
    """見出しが空のセクション(=前チャンクの続き)を直前に連結する。"""
    out: list[dict] = []
    for s in sections:
        if out and not s.get("heading"):
            out[-1]["paragraphs"] += list(s.get("paragraphs") or [])
        else:
            out.append({"heading": s.get("heading", ""), "paragraphs": list(s.get("paragraphs") or [])})
    return out


def _restructure(sections: list[dict], structure: dict) -> list[dict]:
    """言い換え済みセクションを、段落は書き換えず『並べ替え・再グループ化』だけして型に沿わせる。"""
    listing = "\n".join(
        f"[{i}] {s.get('heading') or '(無題)'} … {(' '.join(s.get('paragraphs') or []))[:60]}"
        for i, s in enumerate(sections))
    chapters = "／".join(structure.get("chapters") or [])
    prompt = (
        "既存の記事セクション群を、内容・段落は一切書き換えずに『並べ替え・再グループ化』だけして、"
        "指定の型の流れに沿った新しい構成にしてください。\n"
        f"# 目標の型: {structure.get('name', '')}({structure.get('framework', '')})\n"
        f"章立ての目安: {chapters}\n"
        "# ルール\n"
        "- 新しい各セクションに、内容に合う見出しを付け、既存セクションの番号(source_ids)を割り当てる。\n"
        "- 既存の全番号を、いずれかの新セクションに漏れなく1回ずつ入れる(欠落・重複は禁止)。\n"
        "- 段落そのものは書き換えない・要約しない(並べ替えるだけ)。\n"
        "# 既存セクション\n" + listing + "\n"
        '# 出力(JSONのみ)\n{"sections":[{"heading":"新見出し","source_ids":[0,3]}]}'
    )
    r = llm.generate_json(prompt, FAITHFUL_SYSTEM)
    used: set = set()
    new: list[dict] = []
    for s in (r.get("sections") or []):
        if not isinstance(s, dict):
            continue
        h = str(s.get("heading") or "").strip()
        paras: list[str] = []
        for i in (s.get("source_ids") or []):
            if isinstance(i, int) and 0 <= i < len(sections) and i not in used:
                used.add(i); paras += list(sections[i].get("paragraphs") or [])
        if paras:
            new.append({"heading": h, "paragraphs": paras})
    missing = [i for i in range(len(sections)) if i not in used]   # 取りこぼしは末尾に温存(全量保持)
    if missing:
        leftover: list[str] = []
        for i in missing:
            leftover += list(sections[i].get("paragraphs") or [])
        if new:
            new[-1]["paragraphs"] += leftover
        else:
            new.append({"heading": sections[missing[0]].get("heading", ""), "paragraphs": leftover})
    return new or sections


def _source_title(source_text: str) -> str:
    # 差異ありのため未統一: article_io.extract_legacy_markdown_title は先頭の
    # `# ` だけを採用するが、ここは従来どおり任意個の `#` を除去した最初の
    # 非空行をフォールバックタイトルにする。変更すると faithful 経路の挙動が変わる。
    for line in (source_text or "").splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:60]
    return "記事"


def _attach_seo_metadata(article: dict, sources=None, updated_at: str | None = None) -> None:
    """呼び出し側のメタだけを加える。現在日時は生成せず、未指定は空/Noneで決定的に保つ。"""
    meta_description = generate_meta_description(article)
    if meta_description:
        article["meta_description"] = meta_description
    article["sources"] = list(sources or [])
    article["updated_at"] = updated_at


_EMAIL_CTA_PREFIX = "__email_signup_cta__:"
_LINE_CTA_RE = re.compile(r"公式\s*LINE|lin\.ee|LINE\s*に\s*登録(?:する)?", re.I)


def _reader_term_replacements(text: str) -> str:
    """非off経路で新規に足す読者向けテキストにも固定の言い換えを適用する。"""
    value = str(text or "")
    pairs: list[tuple[str, str]] = []
    for term, rule in rebuild_policy.JARGON_RULES.items():
        pairs.append((term, str(rule["replacement"])))
        pairs.extend((str(alias), str(rule["replacement"])) for alias in rule.get("aliases", []))
    for source, target in sorted(pairs, key=lambda item: -len(item[0])):
        value = value.replace(source, target)
    return value


def generate_premise_lead(source_text: str, title: str, aud: dict | None) -> dict:
    """元記事に忠実な、検索初見者向けの短い前提と改題を生成する。"""
    audience_text = audience.describe(aud) if aud else "指定なし"
    raw = llm.generate_json(rebuild_policy.PREMISE_LEAD_PROMPT.format(
        source_text=(source_text or "")[:40000], title=title or _source_title(source_text),
        audience=audience_text), "")
    if not isinstance(raw, dict):
        return {"framed_title": title, "lead": ""}
    framed_title = _reader_term_replacements(str(raw.get("framed_title") or "").strip())
    lead = _reader_term_replacements(str(raw.get("lead") or "").strip())
    return {"framed_title": framed_title or title, "lead": lead}


def _cta_pdf_benefit(paragraph: str) -> str:
    """CTA本文からPDF特典名だけを控えめに抜き出す。存在しない特典は補わない。"""
    match = re.search(r"([^\s、。]{1,40}?PDF)", paragraph or "", re.I)
    return match.group(1).strip("「」『』") if match else ""


def transform_cta(article: dict, *, endpoint: str = "#email-signup") -> dict:
    """LINE登録CTAだけをメール登録CTAへ置換し、他のCTAと本文は保持する。"""
    endpoint = str(endpoint or "#email-signup").strip() or "#email-signup"
    replaced = False
    for paras, index in iter_para_slots(article):
        paragraph = str(paras[index] or "")
        if not _LINE_CTA_RE.search(paragraph):
            continue
        if not replaced:
            benefit = _cta_pdf_benefit(paragraph)
            paras[index] = f"{_EMAIL_CTA_PREFIX}{benefit}|{endpoint}"
            replaced = True
        else:
            paras[index] = ""
    drop_empty_paragraphs(article)
    return article


def generate_faithful(source_text: str, title: str, aud: dict | None,
                      structure: dict | None = None, restructure: bool = False,
                      run_gates: bool = True, sources=None, updated_at: str | None = None,
                      include_toc: bool = True, embed_json_ld: bool = False, *,
                      rebuild_policy: str = "off", important_facts: dict | None = None,
                      outline: dict | None = None, premise: bool = False,
                      cta: str = "keep", cta_email_endpoint: str = "#email-signup") -> dict:
    """全量保持の忠実生成。restructure=False:①原文順のまま言い換え / True:②型に沿って並べ替え。"""
    chunks = _chunk_source(source_text)
    raw: list[dict] = []
    failures = []
    chunk_retries: list[dict] = []
    for i, ch in enumerate(chunks, 1):
        # off は従来の generate_json / 最大3試行を一切変えない。
        if rebuild_policy == "off":
            last_failure = "有効な sections を含むJSONが返りませんでした"
            for attempt in range(1, _FAITHFUL_CHUNK_MAX_ATTEMPTS + 1):
                try:
                    sections_for_chunk = _faithful_chunk_sections(ch, aud)
                    if sections_for_chunk:
                        raw += sections_for_chunk
                        if attempt > 1:
                            _LOG.warning("faithful chunk %s recovered after %s retries", i, attempt - 1)
                        break
                    last_failure = "有効な sections を含むJSONが返りませんでした"
                except Exception as e:
                    last_failure = f"{type(e).__name__}: {e}"
                if attempt < _FAITHFUL_CHUNK_MAX_ATTEMPTS:
                    _LOG.warning("faithful chunk %s attempt %s/%s failed; retrying: %s",
                                 i, attempt, _FAITHFUL_CHUNK_MAX_ATTEMPTS, last_failure)
            else:
                _LOG.error("faithful chunk %s failed after %s retries: %s", i,
                           _FAITHFUL_CHUNK_MAX_ATTEMPTS - 1, last_failure)
                failures.append(f"chunk {i}: {last_failure}")
            continue

        last_failure = "有効な sections を含むJSONが返りませんでした"
        sections_for_chunk: list[dict] = []
        generated = ""
        for attempt in range(1, _FAITHFUL_CHUNK_MAX_ATTEMPTS + 1):
            try:
                generated = llm.generate(_faithful_chunk_prompt(ch, aud, jargon_rules=_jargon_rules()),
                                         FAITHFUL_SYSTEM)
                payload, parse_status = _parse_faithful_sections_json(generated)
                sections_for_chunk = _sections_from_payload(payload)
                if sections_for_chunk:
                    raw += sections_for_chunk
                    chunk_retries.append({"chunk": i, "retries": attempt - 1, "status": parse_status})
                    if attempt > 1:
                        _LOG.warning("faithful chunk %s recovered after %s retries (%s)",
                                     i, attempt - 1, parse_status)
                    break
                last_failure = "有効な sections を含むJSONが返りませんでした"
            except Exception as e:
                # CLI 応答・ローカル解析の失敗も、チャンク単位の一時失敗として扱う。
                last_failure = f"{type(e).__name__}: {e}"
            if attempt < _FAITHFUL_CHUNK_MAX_ATTEMPTS:
                _LOG.warning("faithful chunk %s attempt %s/%s failed; retrying: %s",
                             i, attempt, _FAITHFUL_CHUNK_MAX_ATTEMPTS, last_failure)
        else:
            # 同一内容で再現する崩れには、壊れた生応答そのものを一度だけ JSON 整形専用へ渡す。
            try:
                repaired_raw = llm.generate(rebuild_policy.JSON_REPAIR_PROMPT.format(raw_text=generated),
                                             FAITHFUL_SYSTEM)
                payload, _ = _parse_faithful_sections_json(repaired_raw)
                sections_for_chunk = _sections_from_payload(payload)
            except Exception as e:
                last_failure = f"JSON repair {type(e).__name__}: {e}"
                sections_for_chunk = []
            if sections_for_chunk:
                raw += sections_for_chunk
                chunk_retries.append({"chunk": i, "retries": _FAITHFUL_CHUNK_MAX_ATTEMPTS - 1,
                                      "status": "parsed_repaired"})
                continue

            # JSON 出力を諦め、半分のサブチャンクをプレーンテキストで言い換えてローカルで段落化する。
            subchunks = _chunk_source(ch, max_chars=max(500, len(ch) // 2))
            split_sections: list[dict] = []
            try:
                for subchunk in subchunks:
                    part = _faithful_plaintext_sections(subchunk, aud, jargon_rules=_jargon_rules())
                    if not part:
                        raise ValueError("プレーンフォールバックが空でした")
                    split_sections.extend(part)
            except Exception as e:
                last_failure = f"fallback split {type(e).__name__}: {e}"
                split_sections = []
            if split_sections:
                raw += split_sections
                chunk_retries.append({"chunk": i, "retries": _FAITHFUL_CHUNK_MAX_ATTEMPTS - 1,
                                      "status": "fallback_split", "subchunks": len(subchunks)})
                continue
            chunk_retries.append({"chunk": i, "retries": _FAITHFUL_CHUNK_MAX_ATTEMPTS - 1,
                                  "status": "failed", "reason": last_failure})
            _LOG.error("faithful chunk %s failed after JSON and split fallbacks: %s", i, last_failure)
            failures.append(f"chunk {i}: {last_failure}")
    if failures:
        raise RuntimeError("必須チャンクの生成に失敗しました: " + "; ".join(failures))
    sections = _merge_sections(raw)
    if restructure and structure:
        sections = _restructure(sections, structure)
    ttl = title or _source_title(source_text)
    article = sanitize_article({"title": ttl, "lead": [], "sections": sections, "summary": {}}, ttl)
    # 品質ゲートの入力から、Webページのナビゲーション等を先に除く。
    normalize_article(article)
    if rebuild_policy != "off":
        # LLM 応答に画像行が無くても、faithful の約束どおり元記事の参照は落とさない。
        rebuild_images = _restore_missing_source_images(article, source_text)
        _translate_image_alt_jargon(article)
    else:
        rebuild_images = []
    mode = "restructure" if restructure else "faithful"
    gates: dict = {}
    rebuild: dict = {"policy": rebuild_policy}
    if rebuild_policy != "off":
        # --llm-trace には試行ごとの呼び出しが残り、ここには全チャンクの解析段階を残す。
        rebuild["faithful_chunk_retries"] = chunk_retries
    if run_gates and rebuild_policy in {"off", "plain"}:   # 既存/off と plain は初稿に安全網を実行
        gates["fabrication"] = _safe(lambda: fabrication_gate(article, source_text, article["title"]))
    if rebuild_policy in {"audited", "ledger"}:
        initial_md = assemble(article, include_toc=include_toc, embed_json_ld=embed_json_ld)["markdown"]
        audit = audit_jargon(initial_md)
        rebuild["jargon_audit"] = audit
        candidates = []
        for finding in audit.get("findings", []):
            if finding.get("severity") == "BLOCK" or (
                finding.get("severity") == "WARN" and not rebuild_policy_module_allowed(finding)):
                candidates.append(finding)
        if candidates:
            article, revision_log = revise_findings(article, candidates, source_text, jargon_rules=_jargon_rules())
            rebuild["jargon_revisions"] = revision_log
        else:
            rebuild["jargon_revisions"] = {"applied": [], "skipped": [], "reason": "修正対象なし"}
        if run_gates:
            gates["fabrication_after_jargon"] = _safe(
                lambda: fabrication_gate(article, source_text, article["title"]))
    if rebuild_policy == "ledger":
        if important_facts is None or outline is None:
            raise ValueError("ledger には important_facts と outline が必要です")
        fact_audit = audit_important_facts(assemble(article, include_toc=include_toc,
                                                     embed_json_ld=embed_json_ld)["markdown"], important_facts)
        rebuild["important_facts"] = fact_audit
        article, ledger_log = revise_ledger_findings(article, fact_audit.get("findings", []), source_text,
                                                      checklist=important_facts)
        rebuild["ledger_revisions"] = ledger_log
        if run_gates and ledger_log.get("applied"):
            gates["fabrication_after_ledger"] = _safe(
                lambda: fabrication_gate(article, source_text, article["title"]))
        rebuild["structure"] = diagnose_structure(
            assemble(article, include_toc=include_toc, embed_json_ld=embed_json_ld)["markdown"], outline)
    if rebuild_policy != "off":
        # premise の改題有無にかかわらず、以後参照できるよう生成時の原題を保持する。
        article["original_title"] = article["title"]
        if cta == "email":
            transform_cta(article, endpoint=cta_email_endpoint)
            rebuild["cta"] = {"mode": "email", "endpoint": cta_email_endpoint}
        else:
            rebuild["cta"] = {"mode": "keep"}
        if premise:
            original_title = article["original_title"]
            premise_payload = generate_premise_lead(source_text, original_title, aud)
            premise_lead = premise_payload["lead"]
            if premise_lead:
                article["lead"] = [premise_lead] + list(article.get("lead") or [])
            article["original_title"] = original_title
            article["title"] = premise_payload["framed_title"] or original_title
            rebuild["premise"] = {"enabled": True, "lead_added": bool(premise_lead),
                                  "added_claims": rebuild_policy_module.scan_added_claims(
                                      "\n".join((article["title"], premise_lead)), source_text)}
        else:
            rebuild["premise"] = {"enabled": False}
    normalize_article(article)
    _attach_seo_metadata(article, sources, updated_at)
    assembled = assemble(article, include_toc=include_toc, embed_json_ld=embed_json_ld)
    policy = gate_policy(mode, run_gates, source_text, article)
    quality_status = _quality_status(policy, gates, run_gates,
                                     gate_policy(mode, True, source_text, article))
    if rebuild_policy != "off":
        deterministic_gate = deterministic_jargon_gate(article)
        added_claims = rebuild_policy_module.scan_added_claims(assembled["markdown"], source_text)
        publish_gates = {
            "meta_leakage": rebuild_policy_module.scan_meta_leakage(assembled["markdown"]),
            "heading_toc": check_heading_toc({"struct": article, "markdown": assembled["markdown"]}, source_text),
            "quote_changes": check_quote_preservation(source_text, assembled["markdown"]),
        }
        rebuild["images_restored"] = rebuild_images
        rebuild["deterministic_jargon_gate"] = deterministic_gate
        rebuild["added_claims"] = added_claims
        rebuild["publish_gates"] = publish_gates
        gates["deterministic_jargon"] = deterministic_gate
        blocked_publish = [
            (name, [hit for hit in hits if (
                name == "meta_leakage"
                or (name == "heading_toc" and hit.get("severity") == "BLOCK")
                or (name == "quote_changes" and hit.get("binding") is True)
            )])
            for name, hits in publish_gates.items()
        ]
        blocked_publish = [(name, hits) for name, hits in blocked_publish if hits]
        if blocked_publish:
            names = "・".join(name for name, _ in blocked_publish)
            count = sum(len(hits) for _, hits in blocked_publish)
            quality_status = f"BLOCK: 公開ゲート {names} {count}件"
    validation = article_validator(article, assembled["html"], assembled["json_ld"], article.get("sources"), gates,
                                   include_toc=include_toc, embed_json_ld=embed_json_ld)
    result = {"title": article["title"], "audience": aud, "mode": mode,
            "structure": (structure or {}).get("id"),
            "char_count": body_char_count(article), "struct": article,
            "gates": gates, "quality_status": quality_status, "validation": validation,
            "meta_description": article.get("meta_description", ""), "sources": article["sources"],
            "updated_at": article["updated_at"], **assembled}
    if article.get("original_title"):
        result["original_title"] = article["original_title"]
    if rebuild_policy != "off":
        result["rebuild"] = rebuild
    return result


def rebuild_policy_module_allowed(finding: dict) -> bool:
    """WARN のうち、説明済みの許容外部語だけを修正対象から外す。"""
    return rebuild_policy.is_allowed_external_term(str(finding.get("term") or ""),
                                                   str(finding.get("quote") or ""))


# ============================ 画風×型 のAI提案 ============================
def recommend(source_text: str, aud: dict | None = None) -> dict:
    """元ネタから 画風(theme)×型(structure) をAI提案(理由付き・課金ゼロ)。slide-studio recommend の記事版。"""
    theme_list = themes_mod.list_themes()
    themes_desc = "\n".join(f"- {t['name']}: {t['display_name']}" for t in theme_list)
    structs_desc = "\n".join(f"- {s['id']}: {s['name']}({s['framework']}) — {s['desc']}" for s in blog_structures.STRUCTURES)
    aud_line = ""
    if aud:
        aud_line = f"\n## 宛先: {aud.get('reader','')} ／ 目的: {aud.get('purpose','')} ／ トーン: {aud.get('tone','')}\n"
    prompt = f"""# ブログ記事の画風と型を選ぶ
{aud_line}## 選べる画風(theme)
{themes_desc}
## 選べる型(structure=記事の構成)
{structs_desc}
## 指針
型は目的と題材に合わせる: 概念解説→explainer / やり方→how-to / おすすめ列挙→listicle / 選択比較→comparison / 実例→case-study / 出来事→news-commentary / 主張→opinion。
ただし「元ネタがその型の核を支えられない」なら選ばない(空洞になるため): case-study=実体験・具体的なbefore/afterが元ネタに無いなら不可 / comparison=比較できる複数の選択肢が無いなら不可 / how-to=実際の手順が無いなら不可。支えられる型が explainer しか無ければ explainer でよい。
画風は内容の雰囲気に合わせる(温かい/人向け→warm-serif・coral-editorial、技術/先進→modern-sans・midnight、堅い/公式→executive-mincho・indigo-pro)。毎回同じ画風を選ばない。
## 出力(JSONのみ)
{{"theme":"<themeのname>","structure":"<structureのid>","reason":"宛先/目的に触れ、なぜその型・画風か1〜2文"}}
## 元ネタ
{source_text[:8000]}
"""
    r = llm.generate_json(prompt, "")
    if r.get("theme") not in {t["name"] for t in theme_list}:
        r["theme"] = themes_mod.DEFAULT_THEME
    if not blog_structures.get(r.get("structure")):
        r["structure"] = blog_structures.default()["id"]
    r.setdefault("reason", "元ネタの内容に合わせて選びました。")
    r["theme_display"] = next(t["display_name"] for t in theme_list if t["name"] == r["theme"])
    r["structure_display"] = blog_structures.get(r["structure"])["name"]
    return r


# ============================ 整形/構造ヘルパ ============================
def sanitize_article(a: dict, fallback_title: str = "記事") -> dict:
    title = str(a.get("title") or fallback_title).strip() or fallback_title
    lead = [str(p).strip() for p in (a.get("lead") or []) if str(p).strip()]
    secs = []
    for s in (a.get("sections") or []):
        if not isinstance(s, dict):
            continue
        h = str(s.get("heading") or "").strip()
        ps = [str(p).strip() for p in (s.get("paragraphs") or []) if str(p).strip()]
        subs = []
        for sub in (s.get("subsections") or []):
            if not isinstance(sub, dict):
                continue
            sub_h = str(sub.get("heading") or "").strip()
            sub_ps = [str(p).strip() for p in (sub.get("paragraphs") or []) if str(p).strip()]
            if sub_h or sub_ps:
                subs.append({"heading": sub_h, "paragraphs": sub_ps})
        if h or ps or subs:
            sec = {"heading": h, "paragraphs": ps}
            if subs:  # additive: 従来スキーマのsectionはこのキーを持たないまま
                sec["subsections"] = subs
            secs.append(sec)
    if not secs:
        raise SystemExit("記事のsectionが空です(生成失敗)")
    summ = a.get("summary") or {}
    summary = {"heading": str(summ.get("heading") or "まとめ").strip(),
               "paragraphs": [str(p).strip() for p in (summ.get("paragraphs") or []) if str(p).strip()]}
    meta_description = str(a.get("meta_description") or "").strip()
    result = {"title": title, "lead": lead, "sections": secs, "summary": summary}
    if meta_description:
        result["meta_description"] = meta_description
    return result


_SENT_SPLIT = re.compile(r"(?<=[。！？])")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def iter_para_slots(a: dict):
    for j in range(len(a.get("lead") or [])):
        yield (a["lead"], j)
    for sec in a.get("sections") or []:
        ps = sec.setdefault("paragraphs", [])
        for j in range(len(ps)):
            yield (ps, j)
        for sub in sec.get("subsections") or []:
            ps = sub.setdefault("paragraphs", [])
            for j in range(len(ps)):
                yield (ps, j)
    sp = (a.get("summary") or {}).get("paragraphs") or []
    for j in range(len(sp)):
        yield (sp, j)


def drop_empty_paragraphs(a: dict) -> None:
    a["lead"] = [p for p in (a.get("lead") or []) if p.strip()]
    for sec in a.get("sections") or []:
        sec["paragraphs"] = [p for p in (sec.get("paragraphs") or []) if p.strip()]
        for sub in sec.get("subsections") or []:
            sub["paragraphs"] = [p for p in (sub.get("paragraphs") or []) if p.strip()]
    if a.get("summary"):
        a["summary"]["paragraphs"] = [p for p in (a["summary"].get("paragraphs") or []) if p.strip()]


def article_plaintext(a: dict) -> str:
    parts = list(a.get("lead") or [])
    for sec in a.get("sections") or []:
        parts.append(sec.get("heading") or "")
        parts += sec.get("paragraphs") or []
        for sub in sec.get("subsections") or []:
            parts.append(sub.get("heading") or "")
            parts += sub.get("paragraphs") or []
    summ = a.get("summary") or {}
    parts.append(summ.get("heading") or "")
    parts += summ.get("paragraphs") or []
    return "\n".join(p for p in parts if p)


def body_char_count(a: dict) -> int:
    return len(re.sub(r"\s", "", article_plaintext(a)))


def _paragraph_count(a: dict) -> int:
    return sum(1 for _ in iter_para_slots(a))


def gate_policy(mode: str, run_gates: bool, source_text: str, article: dict) -> dict:
    """Return the deterministic applicability policy for each quality gate."""
    if not run_gates:
        return {name: "skipped" for name in ("fabrication", "redundancy", "heading_answer")}
    paragraph_count = _paragraph_count(article)
    return {
        "fabrication": "required" if source_text.strip() and paragraph_count else "not_applicable",
        "redundancy": "required" if mode in ("seo_long", "compress") and paragraph_count >= 8 else "not_applicable",
        "heading_answer": "required" if mode in ("seo_long", "compress") else "not_applicable",
    }


def _quality_status(policy: dict, gates: dict, run_gates: bool, required_policy: dict | None = None) -> str:
    """Turn gate results and the policy table into the persisted status."""
    for name, applicability in (required_policy or policy).items():
        if applicability != "required":
            continue
        if not run_gates:
            return f"品質ゲート未実行: {name}: run_gates=False"
        # faithful の局所修正後は同じ捏造ゲートを after_* キーで再実行する。
        # 正規の fabrication 完走として扱い、表示だけが未完走になるのを防ぐ。
        result = gates.get(name)
        if name == "fabrication" and not isinstance(result, dict):
            result = gates.get("fabrication_after_ledger") or gates.get("fabrication_after_jargon")
        if not isinstance(result, dict):
            return f"品質ゲート未完走: {name}: requiredだがran=None"
        reason = str(result.get("reason") or "")
        if reason.startswith("エラー:"):
            return f"品質ゲートエラー: {name}: {reason}"
        if result.get("ran") is not True:
            return f"品質ゲート未完走: {name}: requiredだがran={result.get('ran')}"
    return "checked"


_JP = r"ぁ-んァ-ヶ一-龠ー、。「」『』（）"


def _normalize_typography(s: str) -> str:
    if not s:
        return s
    s = re.sub(rf"(?<=[{_JP}])\?", "？", s)
    s = re.sub(rf"(?<=[{_JP}])!", "！", s)
    s = re.sub(rf"(?<=[{_JP}])\s?:\s?(?=[{_JP}]|$)", "：", s)
    s = re.sub(r"([、。])\s+", r"\1", s)
    return s


def _explode_para(p: str) -> list[str]:
    p = (p or "").strip()
    if not p:
        return []
    # 表は複数行で一つの Markdown ブロック。行ごとの paragraph 化を防ぐ。
    lines = [line.strip() for line in p.splitlines() if line.strip()]
    if len(lines) >= 2 and all(line.startswith("|") and line.endswith("|") for line in lines):
        return ["\n".join(lines)]
    p = re.sub(r"(?<=[。！？])\s*-\s+", "\n- ", p)
    p = re.sub(r"\s*\n\s*", "\n", p)
    return [seg.strip() for seg in p.split("\n") if seg.strip()]


def _normalize_paras(paras: list[str]) -> list[str]:
    out: list[str] = []
    for p in (paras or []):
        for seg in _explode_para(p):
            out.append(_normalize_typography(seg))
    return out


_WEB_PAGE_HEADING_BLACKLIST = frozenset({
    "関連記事", "関連コラム", "監修", "この記事の監修者", "参考", "参考文献", "目次",
})
_EMPTY_BODY_MARKERS = frozenset({"続きを読む", "readmore", "-", "—", "…", "…", "・"})
_MIN_LABEL_ONLY_BODY_CHARS = 4


def _heading_key(value: str) -> str:
    """比較用の見出しキー。表示文字列そのものは変更しない。"""
    return re.sub(r"\s+", "", _normalize_typography(str(value or "")).strip()).casefold()


def _has_substantive_body(paragraphs: list[str], heading: str = "") -> bool:
    """本文が実質空かを保守的に判定する。短い完全文は本文として温存する。"""
    compact = re.sub(r"\s+", "", "".join(str(p or "") for p in paragraphs))
    if not compact or compact.casefold() in _EMPTY_BODY_MARKERS:
        return False
    # 見出しをそのまま再掲しただけの短いタグ本文は、本文ではない。
    if (len(compact) <= 16 and compact.casefold() == _heading_key(heading)
            and not re.search(r"[。！？!?]", compact)):
        return False
    # 数文字のラベル/記号だけは本文扱いにしない。ただし句点付きの短文は残す。
    if (len(compact) <= _MIN_LABEL_ONLY_BODY_CHARS
            and not re.search(r"[。！？!?]", compact)
            and not re.search(r"[ぁ-んァ-ヶ一-龠A-Za-z0-9]", compact)):
        return False
    return True


def drop_empty_headings(a: dict) -> None:
    """Web抽出の空・定型H2と、本文のないH3を決定的に除去する。

    同一H2は先頭を温存する。本文量だけで自然文を落とさないため、忠実モードの
    実セクションは短文であっても残る。
    """
    kept: list[dict] = []
    seen_headings: set[str] = set()
    for sec in a.get("sections") or []:
        heading = str(sec.get("heading") or "")
        sec["subsections"] = [
            sub for sub in (sec.get("subsections") or [])
            if _has_substantive_body(sub.get("paragraphs") or [], sub.get("heading") or "")
        ]
        key = _heading_key(heading)
        if key in _WEB_PAGE_HEADING_BLACKLIST:
            continue
        if key and key in seen_headings:
            continue
        if not _has_substantive_body(sec.get("paragraphs") or [], heading) and not sec.get("subsections"):
            continue
        if key:
            seen_headings.add(key)
        kept.append(sec)
    a["sections"] = kept


def normalize_article(a: dict) -> None:
    a["title"] = _normalize_typography(a.get("title") or "")
    a["lead"] = _normalize_paras(a.get("lead") or [])
    for sec in a.get("sections") or []:
        sec["heading"] = _normalize_typography(sec.get("heading") or "")
        sec["paragraphs"] = _normalize_paras(sec.get("paragraphs") or [])
        for sub in sec.get("subsections") or []:
            sub["heading"] = _normalize_typography(sub.get("heading") or "")
            sub["paragraphs"] = _normalize_paras(sub.get("paragraphs") or [])
    drop_empty_headings(a)
    summ = a.get("summary") or {}
    if summ:
        summ["heading"] = _normalize_typography(summ.get("heading") or "")
        summ["paragraphs"] = _normalize_paras(summ.get("paragraphs") or [])
    if a.get("meta_description"):
        a["meta_description"] = _normalize_typography(a["meta_description"])


# ============================ 品質ゲート(claude・課金ゼロ) ============================
def redundancy_gate(a: dict, title: str) -> dict:
    slots = list(iter_para_slots(a))
    if len(slots) < 8:
        return {"ran": False, "reason": "段落が少なく再述の余地なし"}
    numbered = "\n".join(f"[{i}] {lst[j]}" for i, (lst, j) in enumerate(slots))
    prompt = (
        "あなたは記事編集者です。次の記事本文には同じ主張・情報が過剰に繰り返される水増しがあるかもしれません。\n"
        "同一の主張/コツ/結論が3回以上繰り返されている場合、その初出は必ず残し、それ以降の冗長な"
        "『言い直しだけの段落』の番号を挙げてください。\n"
        "残すもの: 各主張の初出、導入リード、具体例・手順・理由など新情報を含む段落、最後のまとめ1回分。\n"
        "削るもの: 中身が前出とほぼ同じで情報を足していない再述専用の段落だけ。迷う場合は残す(過剰除去禁止)。\n\n"
        f"記事タイトル: {title}\n## 段落(番号つき)\n{numbered}\n\n"
        'JSONだけ返す: {"redundant_indices": [番号...], "reason": "どの主張がどこで重複しているか簡潔に"}'
    )
    parsed = llm.generate_json(prompt, "")
    idxs = [i for i in (parsed.get("redundant_indices") or []) if isinstance(i, int) and 0 <= i < len(slots)]
    removed: list[str] = []
    for i in sorted(set(idxs)):
        lst, j = slots[i]
        removed.append(lst[j]); lst[j] = ""
    drop_empty_paragraphs(a)
    return {"ran": True, "removed": removed, "reason": str(parsed.get("reason") or "").strip()}


def heading_answer_gate(a: dict, title: str) -> dict:
    secs = a.get("sections") or []
    if not secs:
        return {"ran": False, "reason": "section なし"}
    listing = "\n".join(
        f"[{i}] 見出し: {s.get('heading', '')}\n本文: {' '.join(s.get('paragraphs') or [])[:400]}"
        for i, s in enumerate(secs))
    prompt = (
        "あなたは記事編集者です。各sectionの見出しが『問い・約束』を立てているのに、本文がそのsectionの"
        "中で答えず後送り(焦らし)しているものを見つけてください。\n"
        "deferredと判定したsectionには、その見出しの問いへの結論を1文で先出しする導入文(lead_sentence)を"
        "作ってください。★制約: 記事内にすでにある情報の要約だけ。新しい事実・数字・固有名詞を足さない。\n\n"
        f"記事タイトル: {title}\n記事全文(結論の参照用):\n{article_plaintext(a)[:3500]}\n\n"
        f"## 各section\n{listing}\n\n"
        'JSONだけ: {"judgments":[{"i":0,"verdict":"answered|deferred|partial","lead_sentence":"deferred時のみ"}]}'
    )
    parsed = llm.generate_json(prompt, "")
    injected: list[dict] = []
    for jd in (parsed.get("judgments") or []):
        i = jd.get("i")
        if not isinstance(i, int) or not (0 <= i < len(secs)):
            continue
        if jd.get("verdict") == "deferred":
            lead = str(jd.get("lead_sentence") or "").strip()
            if lead:
                secs[i]["paragraphs"] = [lead] + list(secs[i].get("paragraphs") or [])
                injected.append({"heading": secs[i].get("heading", ""), "lead": lead})
    return {"ran": True, "injected": injected}


def fabrication_gate(a: dict, source_text: str, title: str) -> dict:
    if not source_text.strip():
        return {"ran": False, "reason": "元ネタが空(検証不能)"}
    slots = list(iter_para_slots(a))
    lines: list[str] = []
    sent_map: list[tuple] = []
    slot_sents: list[list[str]] = []
    for si, (lst, j) in enumerate(slots):
        sents = split_sentences(lst[j])
        slot_sents.append(sents)
        for k, s in enumerate(sents):
            lines.append(s); sent_map.append((si, k))
    if not lines:
        return {"ran": False, "reason": "本文なし"}
    numbered = "\n".join(f"[{n + 1}] {s}" for n, s in enumerate(lines))
    prompt = (
        "あなたは事実検証者です。次の記事の各文が、下の『元ネタ』に書かれている内容だけで裏付けられるか検証します。\n"
        "元ネタに無い具体的な数字・統計・固有名詞・企業名・調査結果・日付・実在の事例を新造している文の番号を挙げてください。\n"
        "一般常識レベルの説明・手順・考え方は可(捏造でない)。少しでも疑わしい具体値・固有名は捏造とみなす。\n\n"
        f"記事タイトル: {title}\n## 元ネタ(これだけが根拠)\n{source_text[:12000]}\n\n## 記事の各文\n{numbered}\n\n"
        'JSONだけ: {"fabricated": [番号...], "reason": "何が元ネタに無いか簡潔に"}'
    )
    parsed = llm.generate_json(prompt, "")
    remove: set = set()
    for n in (parsed.get("fabricated") or []):
        if isinstance(n, int) and 1 <= n <= len(sent_map):
            remove.add(sent_map[n - 1])
    removed: list[str] = []
    for si, (lst, j) in enumerate(slots):
        kept: list[str] = []
        for k, s in enumerate(slot_sents[si]):
            (removed if (si, k) in remove else kept).append(s)
        lst[j] = "".join(kept)
    drop_empty_paragraphs(a)
    return {"ran": True, "removed": removed, "reason": str(parsed.get("reason") or "").strip()}


# ============ 差し込み画像(動画バックエンドスライド解説と同方式=内容入りスライド図・課金ゲート) ============
def _valid_theme_name(name: str) -> str:
    valid = {t["name"] for t in themes_mod.list_themes()}
    return name if name in valid else themes_mod.DEFAULT_THEME


def _section_to_slide(sec: dict, i: int, source_text: str = "") -> dict:
    """記事セクション → 1枚の構造化解説スライド(claude が layout+内容を決める・数値は接地)。
    抽象挿絵でなく「内容の入った解説スライド図」にする(slide-studio organize + render_deck_ai と同方式)。"""
    heading = (sec.get("heading") or "").strip()
    body = "\n".join(sec.get("paragraphs") or [])
    layouts = "\n".join(f"- {k}: {v}" for k, v in organize.LAYOUTS.items())
    prompt = f"""次のブログ記事セクションを、1枚の解説スライドの構造化データにしてください。内容に最も合う layout を選び、要点を図解に落とします。
## セクション
見出し: {heading}
本文: {body}
## 使える layout(内容に最も合うものを1つ選ぶ)
{layouts}
## 厳守
- 数値・固有名は本文にある実数のみ(創作しない)。必ず heading を含める。
- 箇条書きに逃げず、対比なら two-column、単一の数値なら big-number、複数指標なら kpi-row、手順なら steps 等、内容に合う型を選ぶ。
## 出力(JSONのみ)
{{"layout":"<layout名>","elements":[{{"kind":"heading","text":"{heading}"}}, ... layout定義の主要素 ...]}}"""
    try:
        r = llm.generate_json(prompt, organize.SYSTEM)
    except Exception:
        r = {}
    lay = r.get("layout") if isinstance(r, dict) and r.get("layout") in organize.LAYOUTS else "title-bullets"
    els = (r.get("elements") if isinstance(r, dict) else None) or [{"kind": "heading", "text": heading}]
    if not any(isinstance(e, dict) and e.get("kind") == "heading" for e in els):
        els.insert(0, {"kind": "heading", "text": heading})
    sid = f"section-{i + 1:02d}"
    try:
        sd = organize.sanitize_deck({"title": "x", "slides": [{"id": sid, "layout": lay, "elements": els}]}, "x")
        slide = sd["slides"][0]
    except Exception:
        slide = {"id": sid, "layout": "title-bullets", "elements": [{"kind": "heading", "text": heading}]}
    slide["id"] = sid
    return slide


def gen_images(article: dict, theme_name: str, outdir, approved: bool = False,
               source_text: str = "", max_images: int | None = None,
               style_override: str | None = None) -> dict:
    """各セクションを解説スライド化 → gpt-image で描画(slide-studio render_deck_ai と同方式・内容入り・画風付き)。
    承認(approved)なしでは概算だけ返し gpt-image を一切叩かない。
    style_override(保存済みの画風の自由文)を渡すと、theme画風でなくその画風で描く=画風の踏襲。"""
    secs = article.get("sections") or []
    # FAQ(よくある質問)には挿絵を入れない。出典等の定型節も除外。
    def _no_figure(h: str) -> bool:
        return bool(re.search(r"FAQ|よくある(?:質問|疑問|ご質問)|出典|参考(?:文献|リンク)", str(h or ""), re.I))
    targets = [i for i in range(len(secs)) if not _no_figure(secs[i].get("heading"))]
    if max_images is not None:
        targets = targets[:max_images]
    if not targets:
        return {"ran": False, "reason": "section なし"}
    outdir = pathlib.Path(outdir)
    n = sum(not (outdir / f"section-{i + 1:02d}.png").exists() for i in targets)
    est = render_ai.estimate(n)
    if not approved:
        return {"needs_approval": True, "estimate": est, "count": n,
                "note": "gpt-image で課金が発生します。承認して再実行すると各セクションを解説スライド図にします。"}
    if n > 0 and not render_ai.key_present():
        raise SystemExit("OPENAI_API_KEY が未設定です(.env を確認)")
    theme_name = _valid_theme_name(theme_name)
    theme = themes_mod.load(theme_name)
    slides = [_section_to_slide(secs[i], i, source_text) for i in targets]
    deck = {"title": article.get("title", "記事"), "slides": slides}
    cost = render_ai.render_deck_ai_full(deck, theme, pathlib.Path(outdir), size="2048x1152",
                                         style_override=style_override)
    image_map = {targets[k]: f"{slides[k]['id']}.png" for k in range(len(slides))}
    return {"ran": True, "theme": theme_name, "image_map": image_map,
            "cost": {"images": cost["images_generated"], "usd": cost["actual_usd"], "jpy": cost["actual_jpy"]}}


# ============================ HTML / Markdown 組立 ============================
def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _paragraphs_html(paras: list[str]) -> str:
    out: list[str] = []
    bullets: list[str] = []

    def flush():
        if bullets:
            out.append("<ul>" + "".join(f"<li>{_esc(b)}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for p in paras or []:
        p = (p or "").strip()
        if not p:
            continue
        if p.startswith(_EMAIL_CTA_PREFIX):
            flush()
            benefit, sep, endpoint = p[len(_EMAIL_CTA_PREFIX):].partition("|")
            if sep and endpoint:
                text = (f"メールアドレスをご登録いただくと、{benefit}をお送りします。"
                        if benefit else "メールアドレスをご登録ください。")
                link_text = "メールアドレスを登録してPDFを受け取る" if benefit else "メールアドレスを登録する"
                out.append(f"<p>{_esc(text)}</p><p><a href=\"{_esc(endpoint)}\">{link_text}</a></p>")
                continue
        if p.startswith("- "):
            bullets.append(p[2:].strip())
        else:
            flush(); out.append(f"<p>{_esc(p)}</p>")
    flush()
    return "\n".join(out)


def _append_markdown_paragraphs(md: list[str], paras: list[str]) -> None:
    """連続する表行/表ブロックの間だけは空行を挟まず、Markdown table を保つ。"""
    in_table = False
    for paragraph in paras or []:
        p = str(paragraph or "").strip()
        if p.startswith(_EMAIL_CTA_PREFIX):
            benefit, sep, endpoint = p[len(_EMAIL_CTA_PREFIX):].partition("|")
            if sep and endpoint:
                text = (f"メールアドレスをご登録いただくと、{benefit}をお送りします。"
                        if benefit else "メールアドレスをご登録ください。")
                link_text = "メールアドレスを登録してPDFを受け取る" if benefit else "メールアドレスを登録する"
                md += [text, "", f"[{link_text}]({endpoint})", ""]
                continue
        is_table = bool(p) and all(line.strip().startswith("|") and line.strip().endswith("|")
                                   for line in p.splitlines() if line.strip())
        if is_table:
            if not in_table:
                md.append(p)
            else:
                md.extend(p.splitlines())
            in_table = True
        else:
            if in_table:
                md.append("")
            if p:
                md += [p, ""]
            in_table = False
    if in_table:
        md.append("")


def _figure_html(fname: str, alt: str) -> str:
    return f'<figure class="wp-block-image size-large"><img src="{fname}" alt="{_esc(alt)}"/></figure>'


def generate_meta_description(a: dict) -> str:
    """本文だけを入力にしてメタ説明を作る。失敗時は空にし、根拠のない補完をしない。"""
    prompt = (
        "次の完成記事の本文だけを要約し、meta_description を作ってください。本文にない情報・助言・\n"
        "煽り表現を足さず、記事タイトルの主キーワードを自然に含めます。120〜160字にしてください。\n"
        "JSONだけ返す: {\"meta_description\":\"...\"}\n\n"
        f"# タイトル\n{a.get('title', '')}\n\n# 本文\n{article_plaintext(a)}"
    )
    try:
        value = str((llm.generate_json(prompt, BLOG_SYSTEM) or {}).get("meta_description") or "").strip()
    except Exception:
        return ""
    return value if 120 <= len(value) <= 160 else ""


def _faq_pairs(a: dict) -> list[tuple[str, str]]:
    """FAQ章内にすでにある Q/A のみを抽出する。本文を補完・再生成しない。"""
    pairs: list[tuple[str, str]] = []
    for sec in a.get("sections") or []:
        containers = [sec] + list(sec.get("subsections") or [])
        for container in containers:
            if not re.search(r"FAQ|よくある(?:質問|疑問)", container.get("heading") or "", re.I):
                continue
            question = ""
            for p in container.get("paragraphs") or []:
                text = (p or "").strip()
                q = re.match(r"^(?:Q[．.：:]\s*)?(.+?[？?])\s*(?:A[．.：:]\s*)?(.+)$", text)
                if q:
                    pairs.append((q.group(1).strip(), q.group(2).strip())); question = ""; continue
                q_only = re.match(r"^(?:Q[．.：:]\s*)?(.+?[？?])$", text)
                if q_only:
                    question = q_only.group(1).strip(); continue
                answer = re.match(r"^A[．.：:]\s*(.+)$", text)
                if question and answer:
                    pairs.append((question, answer.group(1).strip())); question = ""
    return [(q, ans) for q, ans in pairs if q and ans]


def build_json_ld(a: dict, updated_at: str | None = None) -> str:
    """Article と、本文中FAQから機械抽出できた FAQPage を JSON-LD にする。"""
    article = {"@type": "Article", "headline": a.get("title") or "記事",
               "description": a.get("meta_description") or ""}
    if updated_at:
        article["datePublished"] = updated_at
    graph: list[dict] = [article]
    pairs = _faq_pairs(a)
    if pairs:
        graph.append({"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": answer}}
            for q, answer in pairs
        ]})
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False, separators=(",", ":"))


def _source_text(source) -> str:
    if isinstance(source, dict):
        return str(source.get("title") or source.get("name") or source.get("url") or "").strip()
    return str(source or "").strip()


BLOG_STYLE = r"""<style>
/* blog-article : シンプルで上品な日本語ブログ記事デザイン(自己完結・ライト/ダーク・レスポンシブ) */
:root{--bg:#ffffff;--surface:#f7f8f9;--surface-2:#eef0f2;--text:#24292f;--text-soft:#57606a;--text-mute:#8b949e;--heading:#1b1f24;--border:#e2e5e9;--border-2:#d5d9de;--accent:#2f6f4f;--accent-2:#3f8a63;--link:#1f6feb;--link-hover:#0a4fbf;--mark:#fff3c4;--code-bg:#f2f4f6;--measure:720px;}
@media (prefers-color-scheme: dark){:root{--bg:#14171a;--surface:#1c2024;--surface-2:#23282d;--text:#d4d8dd;--text-soft:#a5adb6;--text-mute:#7a828b;--heading:#eef1f4;--border:#2b3138;--border-2:#39414a;--accent:#6fbf95;--accent-2:#57a97f;--link:#6cb0ff;--link-hover:#93c5ff;--mark:#4a431f;--code-bg:#23282d;}}
:root[data-theme="light"]{--bg:#ffffff;--surface:#f7f8f9;--surface-2:#eef0f2;--text:#24292f;--text-soft:#57606a;--text-mute:#8b949e;--heading:#1b1f24;--border:#e2e5e9;--border-2:#d5d9de;--accent:#2f6f4f;--accent-2:#3f8a63;--link:#1f6feb;--link-hover:#0a4fbf;--mark:#fff3c4;--code-bg:#f2f4f6;}
:root[data-theme="dark"]{--bg:#14171a;--surface:#1c2024;--surface-2:#23282d;--text:#d4d8dd;--text-soft:#a5adb6;--text-mute:#7a828b;--heading:#eef1f4;--border:#2b3138;--border-2:#39414a;--accent:#6fbf95;--accent-2:#57a97f;--link:#6cb0ff;--link-hover:#93c5ff;--mark:#4a431f;--code-bg:#23282d;}
html{-webkit-text-size-adjust:100%;}
body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI","Helvetica Neue","Hiragino Sans","Hiragino Kaku Gothic ProN","Noto Sans JP","Yu Gothic","Yu Gothic UI",Meiryo,sans-serif;font-size:17px;line-height:1.9;letter-spacing:.01em;text-rendering:optimizeLegibility;-webkit-font-smoothing:antialiased;}
.blog-article{max-width:var(--measure);margin:0 auto;padding:56px 22px 96px;word-wrap:break-word;overflow-wrap:break-word;}
.blog-article h1{margin:0 0 .7em;font-size:2em;line-height:1.4;font-weight:800;letter-spacing:.005em;color:var(--heading);}
.blog-article h1 + p{font-size:1.06em;color:var(--text-soft);line-height:1.95;margin-bottom:2em;}
.blog-article h2{margin:2.6em 0 1em;padding:.5em 0 .5em .8em;font-size:1.5em;line-height:1.45;font-weight:700;color:var(--heading);border-left:5px solid var(--accent);border-bottom:1px solid var(--border);}
.blog-article h3{margin:2em 0 .8em;padding-left:.55em;font-size:1.2em;line-height:1.5;font-weight:700;color:var(--heading);border-left:3px solid var(--accent-2);}
.blog-article h2,.blog-article h3{scroll-margin-top:20px;}
.blog-article p{margin:0 0 1.5em;line-height:1.9;}
.blog-article strong{font-weight:700;color:var(--heading);}
.blog-article ul,.blog-article ol{margin:0 0 1.6em;padding-left:1.4em;}
.blog-article li{margin:.5em 0;line-height:1.85;padding-left:.2em;}
.blog-article ul > li::marker{color:var(--accent);}
.blog-article ol > li::marker{color:var(--accent);font-weight:600;}
.blog-article .blog-toc{margin:2.4em 0;padding:1.1em 1.4em 1.2em;background:var(--surface);border:1px solid var(--border);border-radius:10px;font-size:.96em;}
.blog-article .blog-toc > p:first-child{margin:0 0 .6em;font-weight:700;font-size:.95em;letter-spacing:.06em;color:var(--text-soft);}
.blog-article .blog-toc ul{list-style:none;margin:0;padding:0;}
.blog-article .blog-toc li{margin:.28em 0;line-height:1.7;}
.blog-article .blog-toc li::before{content:"";display:inline-block;width:.5em;height:.5em;margin-right:.55em;border-radius:50%;background:var(--accent);vertical-align:middle;}
.blog-article .blog-toc li.toc-h3{padding-left:1.4em;font-size:.94em;}
.blog-article .blog-toc li.toc-h3::before{width:.36em;height:.36em;background:transparent;border:1.5px solid var(--accent-2);}
.blog-article .blog-toc a{color:var(--text);text-decoration:none;border-bottom:1px solid transparent;}
.blog-article .blog-toc a:hover{color:var(--link);border-bottom-color:var(--link);}
.blog-article figure.wp-block-image{margin:2.2em 0;}
.blog-article figure.wp-block-image img{display:block;width:100%;height:auto;max-width:100%;border:1px solid var(--border);border-radius:10px;background:var(--surface);}
.blog-article figcaption{margin-top:.7em;font-size:.85em;line-height:1.7;color:var(--text-soft);text-align:center;}
.blog-article a{color:var(--link);text-decoration:none;border-bottom:1px solid rgba(31,111,235,.35);}
.blog-article a:hover{color:var(--link-hover);border-bottom-color:var(--link-hover);}
.blog-article blockquote{margin:1.8em 0;padding:.6em 1.2em;border-left:4px solid var(--border-2);background:var(--surface);color:var(--text-soft);border-radius:0 8px 8px 0;}
.blog-article code{font-family:"SF Mono","Menlo","Consolas",monospace;font-size:.88em;background:var(--code-bg);padding:.12em .4em;border-radius:4px;}
.blog-article hr{border:0;border-top:1px solid var(--border);margin:2.6em 0;}
.blog-article .blog-sources{margin-top:3em;padding:1.1em 1.4em;background:var(--surface);border:1px solid var(--border);border-radius:10px;font-size:.85em;color:var(--text-soft);line-height:1.8;}
.blog-article .blog-sources h2{margin:0 0 .6em;padding:0;font-size:1.05em;border:0;color:var(--text-soft);font-weight:700;}
.blog-article .blog-sources ul{margin:0;padding-left:1.3em;}
.blog-article .blog-sources a{color:var(--text-soft);word-break:break-all;}
.blog-article table{display:block;width:100%;overflow-x:auto;border-collapse:collapse;font-size:.92em;margin:1.6em 0;}
.blog-article th,.blog-article td{border:1px solid var(--border);padding:.5em .8em;text-align:left;}
.blog-article th{background:var(--surface);}
@media (max-width:600px){body{font-size:16px;}.blog-article{padding:36px 16px 72px;}.blog-article h1{font-size:1.65em;}.blog-article h2{font-size:1.32em;}.blog-article h3{font-size:1.12em;}.blog-article .blog-toc{padding:1em 1.1em;}}
</style>"""


def styled_html(body_html: str) -> str:
    """単体プレビュー/共有用: 記事HTMLに自己完結の簡易デザイン(CSS)を付与して返す。
    WordPress貼付は生htmlを使う(WPはテーマCSSで整形)。styled_htmlは見せる/共有用の別出力。"""
    body = body_html or ""
    m = re.search(r"<h2[^>]*>出典</h2>", body)
    if m:
        body = body[:m.start()] + '<div class="blog-sources">' + body[m.start():] + "</div>"
    return f'{BLOG_STYLE}\n<div class="blog-article">{body}</div>'


def standalone_html(body_html: str, title: str = "") -> str:
    """ブラウザで直接開くための完全な HTML 文書。

    article.html は WordPress 貼付用の断片なので doctype も head も持たない。
    そのまま開くと CSS が当たらず素の見た目になり、 さらに charset 宣言が無いため
    日本語環境ではブラウザが文字コードを誤判定して文字化けすることもある。
    貼付用は断片のまま残し、 見せる/共有する用にこちらを別ファイルで出す。
    """
    safe_title = _esc((title or "").strip() or "記事プレビュー")
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ja">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{safe_title}</title>\n"
        "</head>\n"
        "<body>\n"
        f"{styled_html(body_html)}\n"
        "</body>\n"
        "</html>\n"
    )


def assemble(a: dict, image_map: dict | None = None, *, include_toc: bool = True,
             embed_json_ld: bool = False) -> dict:
    """article(構造化) → html(WP貼付可) / markdown。TOCは既定で有効、JSON-LD埋込は既定で無効。"""
    image_map = image_map or {}
    title = (a.get("title") or "").strip() or "無題"
    html: list[str] = [f"<h1>{_esc(title)}</h1>"]
    md: list[str] = [f"# {title}", ""]
    placed = 0
    toc: list[tuple[int, str, str]] = []
    for i, sec in enumerate(a.get("sections") or [], 1):
        heading = (sec.get("heading") or "").strip()
        if heading:
            toc.append((2, f"sec-{i:02d}", heading))
        for j, sub in enumerate(sec.get("subsections") or [], 1):
            heading = (sub.get("heading") or "").strip()
            if heading:
                toc.append((3, f"sec-{i:02d}-{j:02d}", heading))
    summ = a.get("summary") or {}
    if summ.get("paragraphs"):
        toc.append((2, "sec-summary", (summ.get("heading") or "まとめ").strip()))
    lead = a.get("lead") or []
    if lead:
        html.append(_paragraphs_html(lead))
        _append_markdown_paragraphs(md, lead)
    if include_toc and toc:
        html.append('<nav class="blog-toc" aria-label="目次"><p>目次</p><ul>')
        md += ["## 目次", ""]
        for level, anchor, heading in toc:
            indent = ' style="margin-left:1.5em"' if level == 3 else ""
            html.append(f'<li class="toc-h{level}"{indent}><a href="#{anchor}">{_esc(heading)}</a></li>')
            md += [f"{'  ' if level == 3 else ''}- [{heading}](#{anchor})"]
        html.append("</ul></nav>"); md.append("")
    for i, sec in enumerate(a.get("sections") or []):
        h = (sec.get("heading") or "").strip()
        if h:
            anchor = f"sec-{i + 1:02d}"
            h2_attr = f' id="{anchor}"' if include_toc else ""
            html.append(f"<h2{h2_attr}>{_esc(h)}</h2>"); md += [f"## {h}", ""]
        if i in image_map:
            fn = image_map[i]
            html.append(_figure_html(fn, h)); md += [f"![{h}]({fn})", ""]; placed += 1
        paras = sec.get("paragraphs") or []
        html.append(_paragraphs_html(paras))
        _append_markdown_paragraphs(md, paras)
        for j, sub in enumerate(sec.get("subsections") or []):
            sh = (sub.get("heading") or "").strip()
            if sh:
                anchor = f"sec-{i + 1:02d}-{j + 1:02d}"
                h3_attr = f' id="{anchor}"' if include_toc else ""
                html.append(f"<h3{h3_attr}>{_esc(sh)}</h3>"); md += [f"### {sh}", ""]
            sub_paras = sub.get("paragraphs") or []
            html.append(_paragraphs_html(sub_paras))
            _append_markdown_paragraphs(md, sub_paras)
    if summ.get("paragraphs"):
        sh = (summ.get("heading") or "まとめ").strip()
        summary_attr = ' id="sec-summary"' if include_toc else ""
        html.append(f"<h2{summary_attr}>{_esc(sh)}</h2>"); md += [f"## {sh}", ""]
        html.append(_paragraphs_html(summ["paragraphs"]))
        _append_markdown_paragraphs(md, summ["paragraphs"])
    sources = [_source_text(s) for s in (a.get("sources") or [])]
    sources = [s for s in sources if s]
    if sources:
        html.append('<h2 id="sources">出典</h2><ul>' + "".join(f"<li>{_esc(s)}</li>" for s in sources) + "</ul>")
        md += ["## 出典", ""] + [f"- {s}" for s in sources] + [""]
    # TODO(stage2): サイト記事台帳を入力にし、関連度を検証した内部リンクだけをここで組み立てる。
    json_ld = build_json_ld(a, a.get("updated_at"))
    if embed_json_ld:
        html.append(f'<script type="application/ld+json">{json_ld}</script>')
    html_str = "\n".join(html)
    return {"title": title, "html": html_str, "markdown": "\n".join(md).strip() + "\n",
            "images_placed": placed, "json_ld": json_ld, "styled_html": styled_html(html_str)}


def article_validator(struct: dict, html: str, json_ld: str, sources, gates: dict, *,
                      include_toc: bool, embed_json_ld: bool) -> dict:
    """Deterministically validate emitted article artifacts; this never calls an LLM."""
    reasons: list[str] = []
    text = html or ""
    first = re.match(r"\s*<h1(?:\s[^>]*)?>(.*?)</h1>", text, re.I | re.S)
    if not first:
        reasons.append("article_validator: H1が本文先頭にありません")
    elif unescape(re.sub(r"<[^>]+>", "", first.group(1))).strip() != str(struct.get("title") or "").strip():
        reasons.append("article_validator: H1タイトルがstruct.titleと一致しません")

    headings = list(re.finditer(r"<h([23])(?:\s+[^>]*)?>(.*?)</h\1>", text, re.I | re.S))
    for index, match in enumerate(headings):
        level, heading = match.group(1), unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        body = text[match.end():headings[index + 1].start() if index + 1 < len(headings) else len(text)]
        if level == "2" and index + 1 < len(headings) and headings[index + 1].group(1) == "3":
            continue  # 親H2(直後にH3小見出しが続く)は自前の本文不要。空判定はH3側で個別に行う(FAQ親等の過剰拒否回避)
        if not re.search(r"<(?:p|li)\b[^>]*>\s*\S", body, re.I | re.S):
            reasons.append(f"article_validator: 空のH{level}本文: {heading}")

    # FAQ is optional, but every question which is present must have an answer.
    faq_context = False
    pending_question = ""
    for sec in struct.get("sections") or []:
        containers = [sec] + list(sec.get("subsections") or [])
        for container in containers:
            faq_context = bool(re.search(r"FAQ|よくある(?:質問|疑問)", str(container.get("heading") or ""), re.I))
            for paragraph in container.get("paragraphs") or []:
                value = str(paragraph or "").strip()
                inline = re.match(r"^(?:Q[．.：:]\s*)?(.+?[？?])\s*(?:A[．.：:]\s*)?(.+)$", value)
                question = re.match(r"^(?:Q[．.：:]\s*)?(.+?[？?])$", value)
                answer = re.match(r"^A[．.：:]\s*(.+)$", value)
                if inline:
                    pending_question = ""
                elif question and (faq_context or value.startswith("Q")):
                    if pending_question:
                        reasons.append(f"article_validator: FAQ質問に回答がありません: {pending_question}")
                    pending_question = question.group(1).strip()
                elif pending_question and (answer or (faq_context and value)):
                    pending_question = ""
            if pending_question:
                reasons.append(f"article_validator: FAQ質問に回答がありません: {pending_question}")
                pending_question = ""

    # A TOC, if emitted, must reference the exact visible H2/H3 heading.
    # Attribute order is not significant, so collect ids from each H2/H3 tag.
    ids = {}
    for m in re.finditer(r"<h([23])([^>]*)>(.*?)</h\1>", text, re.I | re.S):
        identifier = re.search(r"\bid=[\"']([^\"']+)[\"']", m.group(2), re.I)
        if identifier:
            ids[identifier.group(1)] = unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
    for m in re.finditer(r"<a\s+[^>]*href=[\"']#([^\"']+)[\"'][^>]*>(.*?)</a>", text, re.I | re.S):
        anchor, label = m.group(1), unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if anchor not in ids or ids[anchor] != label:
            reasons.append(f"article_validator: 目次アンカーが実在見出しに一致しません: {anchor}")

    meta = str(struct.get("meta_description") or "")
    if meta and not 120 <= len(meta) <= 160:
        reasons.append(f"article_validator: meta_descriptionは空または120〜160字である必要があります: {len(meta)}字")
    for source in sources or []:
        if not isinstance(source, dict) or not source.get("url"):
            continue
        value = str(source["url"])
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            reasons.append(f"article_validator: 出典URLが不正です: {value}")

    if json_ld:
        try:
            payload = json.loads(json_ld)
        except (TypeError, json.JSONDecodeError):
            reasons.append("article_validator: JSON-LDのJSON構文が不正です")
        else:
            graph = payload.get("@graph") if isinstance(payload, dict) else None
            article_node = next((n for n in (graph or []) if isinstance(n, dict) and n.get("@type") == "Article"), None)
            if not isinstance(article_node, dict) or article_node.get("headline") != struct.get("title") or article_node.get("description") != meta:
                reasons.append("article_validator: JSON-LD Articleが不整合です")
            faq_node = next((n for n in (graph or []) if isinstance(n, dict) and n.get("@type") == "FAQPage"), None)
            expected_faq = [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
                            for q, a in _faq_pairs(struct)]
            if (faq_node is not None and faq_node.get("mainEntity") != expected_faq) or (faq_node is None and expected_faq):
                reasons.append("article_validator: JSON-LD FAQPageが本文FAQと不整合です")
            scripts = re.findall(r"<script\s+[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", text, re.I | re.S)
            if scripts and any(script.strip() != json_ld.strip() for script in scripts):
                reasons.append("article_validator: HTML内JSON-LDがpayloadと不一致です")
    return {"passed": not reasons, "reasons": reasons}


# ============================ オーケストレータ ============================
def generate(source_text: str, title: str, aud: dict | None, mode: str = "seo_long",
             target: int = 3500, structure: dict | None = None, run_gates: bool = True,
             sources=None, updated_at: str | None = None, include_toc: bool = True,
             embed_json_ld: bool = False) -> dict:
    """元ネタ → 記事(構造化+HTML+MD)。画像は含まない(別途 gen_images → assemble で差し込む)。"""
    article = sanitize_article(rewrite(title, source_text, aud, mode, target, structure), title or "記事")
    # LLMがページ定型要素をsection化しても、各ゲートと組立の前に決定的に除去する。
    normalize_article(article)
    gates: dict = {}
    if run_gates:
        if mode in ("seo_long", "compress"):
            gates["redundancy"] = _safe(lambda: redundancy_gate(article, article["title"]))
            gates["heading_answer"] = _safe(lambda: heading_answer_gate(article, article["title"]))
        gates["fabrication"] = _safe(lambda: fabrication_gate(article, source_text, article["title"]))
    normalize_article(article)
    _attach_seo_metadata(article, sources, updated_at)
    assembled = assemble(article, include_toc=include_toc, embed_json_ld=embed_json_ld)
    policy = gate_policy(mode, run_gates, source_text, article)
    quality_status = _quality_status(policy, gates, run_gates,
                                     gate_policy(mode, True, source_text, article))
    validation = article_validator(article, assembled["html"], assembled["json_ld"], article.get("sources"), gates,
                                   include_toc=include_toc, embed_json_ld=embed_json_ld)
    return {"title": article["title"], "audience": aud, "mode": mode,
            "structure": (structure or {}).get("id"),
            "char_count": body_char_count(article), "struct": article,
            "gates": gates, "quality_status": quality_status, "validation": validation,
            "meta_description": article.get("meta_description", ""), "sources": article["sources"],
            "updated_at": article["updated_at"], **assembled}


def _safe(fn):
    try:
        return fn()
    except Exception as e:
        return {"ran": False, "reason": f"エラー: {e}"}
