"""POC 用の読者向け用語ポリシー。実行時に外部ファイルを読まない。"""
from __future__ import annotations

import re

AUDIT_CONTRACT_VERSION = "1"

# Gate B: 元Markdown見出しの3割以上が失われると、目次から本文へ辿れない。
# non-off rebuild だけで使い、従来の off 経路には影響させない。
SOURCE_HEADING_RETENTION_RATIO = 0.7

# 用語の対照表と、POC 固定の禁止/許容規則。
# 例示用の架空の社内用語と言い換え(実際の語彙表は同梱しない。利用側が自分の語彙で差し替える)。
JARGON_RULES: dict[str, dict] = {
    "sample_dict": {"replacement": "用語辞書", "aliases": ["sample_dict.yaml"]},
    "review-loop": {"replacement": "複数視点で原稿を見直す工程", "aliases": ["多視点レビュー"]},
    "draft_spec": {"replacement": "下書きの設計図", "aliases": ["draft_spec.yaml"]},
    "render_main": {"replacement": "動画を組み立てる本体処理", "aliases": ["make_video.py"]},
    "agent-tool": {"replacement": "担当別の小さなAI", "aliases": ["サブ担当"]},
    "item-NNN": {"replacement": "別記事", "aliases": ["item-001", "item-002"]},
    "QA": {"replacement": "品質チェック", "aliases": []},
    "BLOCK": {"replacement": "公開を止める判定", "aliases": []},
    "多層ガード": {"replacement": "複数段階のチェック", "aliases": []},
    "フィルタチェーン": {"replacement": "変換処理の設定", "aliases": []},
    "仕様内の挙動": {"replacement": "エラーにならず処理が続く動き", "aliases": []},
}
# LLM の監査結果に依存しない、公開物用の固定照合語。短い語も含むため、ここは
# 読者向け rebuild を明示した経路だけで使う（従来の --rebuild-policy off は不変）。
# 表記ゆれも明示列挙し、正規表現の曖昧な推測はしない。
# 日常語・一般的なIT用語（ブロック、パイプライン、スコアリング、メタデータ等）は
# hard-BLOCK にしない。JARGON_RULES の生成時言い換えと LLM 監査で文脈を見て扱う。
BANNED_TERMS = frozenset({
    "QA", "ＱＡ", "qa", "BLOCK", "多層ガード", "フィルタチェーン", "フィルター・チェーン",
    "仕様内の挙動", "仕様内挙動", "内部語", "工程語",
    "sample_dict", "sample_dict.yaml", "review-loop", "多視点レビュー", "draft_spec", "draft_spec.yaml",
    "render_main", "make_video.py", "agent-tool", "item-NNN",
})# 互換用の旧名。公開ゲートの正は BANNED_TERMS / scan_banned である。
BLOCKED_TERMS = BANNED_TERMS

# 完全な意味照合ではなく、元記事に無かった属性を人手確認へ送る粗い検出器。
ADDED_CLAIM_TERMS = frozenset({
    "無料", "有料", "非圧縮", "無圧縮", "最速", "最短", "世界初", "国内初", "業界初",
    "唯一", "必ず", "絶対", "完全保証", "100％", "100%",
})


_ASCII_TERM_RE = re.compile(r"^[\x00-\x7f]+$")

# 一般の記事で使う単語だけでは判定しない。生成指示をそのまま読者向け本文へ
# 漏らした場合に限る、具体的な組合せに固定する。
_META_LEAKAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("polished_article", re.compile(r"整えた本文です")),
    ("json_only", re.compile(r"JSON\s*(?:のみ|だけ)|JSON\s*を\s*返(?:す|し)")),
    ("instruction_text", re.compile(r"(?:冒頭の)?指示文.{0,48}(?:JSON|見出し|相反|指定)")),
    ("heading_notation", re.compile(r"見出し記法.{0,48}(?:外[しす]|指示|##)")),
    ("paragraph_instruction", re.compile(r"段落を空行で区切")),
    ("preserve_list", re.compile(r"内容[・、]順序[・、]数値[・、]具体例[・、]固有名詞")),
    ("ending_instruction", re.compile(r"そのままに[、,]文末を")),
    ("preface_instruction", re.compile(r"以下[、,:：].{0,80}(?:本文|整え)")),
    ("supplement_instruction", re.compile(r"補足[：:].{0,100}(?:指示|JSON|見出し)")),
)


def scan_meta_leakage(text: str) -> list[dict]:
    """本文に混入した生成指示・自己言及だけを高精度に検出する。"""
    value = str(text or "")
    hits: list[dict] = []
    for kind, pattern in _META_LEAKAGE_PATTERNS:
        for match in pattern.finditer(value):
            start, end = match.span()
            hits.append({"type": kind, "start": start, "end": end,
                         "match": match.group(0),
                         "context": value[max(0, start - 32):min(len(value), end + 48)]})
    return sorted(hits, key=lambda item: (item["start"], item["end"], item["type"]))


def scan_banned(text: str) -> list[dict]:
    """固定語の決定論的な照合。位置は 0-based の文字オフセット。
    英数字のみの語(QA/BLOCK 等)は前後が英数字・下線でない単語境界でのみ一致させ、
    ファイル名やURL片(例 images/sample.png)への部分一致による誤検出を避ける。
    日本語を含む語は従来どおり部分一致。"""
    value = str(text or "")
    hits: list[dict] = []
    # 長い語を先にして、同じ開始位置で短い表記ゆれが先に出ないよう安定化する。
    for term in sorted(BANNED_TERMS, key=lambda item: (-len(item), item)):
        if _ASCII_TERM_RE.match(term):
            pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])",
                                 re.IGNORECASE)
            for m in pattern.finditer(value):
                index = m.start()
                hits.append({"term": term, "start": index, "end": index + len(term),
                             "context": value[max(0, index - 24):index + len(term) + 24]})
        else:
            start = 0
            while True:
                index = value.find(term, start)
                if index < 0:
                    break
                hits.append({"term": term, "start": index, "end": index + len(term),
                             "context": value[max(0, index - 24):index + len(term) + 24]})
                start = index + len(term)
    hits.sort(key=lambda item: (item["start"], -len(item["term"]), item["term"]))
    # 同一位置の重複(例 大小文字違いの QA/qa が IGNORECASE で二重ヒット)を排除。
    deduped: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for h in hits:
        key = (h["start"], h["end"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)
    return deduped


def scan_added_claims(output_text: str, source_text: str) -> list[dict]:
    """出力だけに現れる具体属性語を列挙する（誤検出は人手確認前提）。"""
    output, source = str(output_text or ""), str(source_text or "")
    hits: list[dict] = []
    for term in sorted(ADDED_CLAIM_TERMS, key=lambda item: (-len(item), item)):
        if term in source:
            continue
        start = 0
        while True:
            index = output.find(term, start)
            if index < 0:
                break
            hits.append({"term": term, "start": index, "end": index + len(term),
                         "context": output[max(0, index - 24):index + len(term) + 24]})
            start = index + len(term)
    return sorted(hits, key=lambda item: (item["start"], -len(item["term"]), item["term"]))
EXTERNAL_TERMS = frozenset({"VOICEVOX", "ffmpeg", "WAV", "kHz", "サンプリングレート"})
_EXTERNAL_EXPLANATIONS = {
    "VOICEVOX": ("音声合成",), "ffmpeg": ("動画", "音声", "加工", "結合", "コマンドライン"),
    "WAV": ("音声ファイル", "ファイル形式"), "kHz": ("1秒間", "音を記録", "設定"),
    "サンプリングレート": ("音質設定", "1秒間", "音を記録"),
}

FAITHFUL_JARGON_PROMPT = """# 読者向け用語の翻訳規則
次の内部・工程語は原語のまま残さず、意味を増減させずに読者向け表現へ言い換える。
{rules}
外部製品名・技術語（VOICEVOX、ffmpeg、WAV、kHz、サンプリングレート）は、元の文章にある場合だけ残せる。初出の同じ文または直後の一文で役割を平易に説明する。元ネタにない一般知識・手順・背景・属性（無料、非圧縮など）は追加しない。"""

JSON_REPAIR_PROMPT = """次のテキストは、記事チャンクを忠実に言い換えた JSON の出力ですが、構文だけが壊れています。
内容、段落、見出し、語順、数値、固有名詞を変更・要約・補足・削除せず、有効な JSON に整形し直してください。説明や Markdown フェンスは付けません。記事本文以外（指示文、補足、JSONへの言及、自己言及）は絶対に書きません。「」で囲まれた直接引用は一字一句変えません。

返す JSON のスキーマは必ず次だけです:
{{"sections":[{{"heading":"見出し","paragraphs":["段落"]}}]}}

# 壊れた生テキスト
{raw_text}"""

PREMISE_LEAD_PROMPT = """元記事だけを根拠に、検索から初めて来た読者のための前提リードと検索向けタイトルを作ってください。

# 絶対ルール
- 分野は元記事から読み取れるものに限る。動画制作と決め打ちしない。
- 前提リードは「筆者が何をしている人で、この記事は何の話か」が分かる平易な日本語の1〜2文にする。
- 前提リードは読者を案内する短い前置きだけを許す。それ以外の事実、数値、固有名詞、実績、因果、誇張、一般知識は元記事にないものを足さない。
- 内部資料名・工程名・社内用語は使わない。必要なら読者向けの言葉に言い換える。
- 検索向けタイトルは原題の意味を変えず、分野または前提の文脈を一つだけ足す。煽りや内容改変はしない。
- 根拠が足りず安全に補えない場合、lead は空文字、framed_title は原題をそのまま返す。

# 原題
{title}

# 想定読者
{audience}

# 元記事
{source_text}

JSONのみ: {{"framed_title":"検索向けに前提を1つ足した改題", "lead":"初見読者向けの前提リード(1〜2文)"}}"""

AUDIT_JARGON_PROMPT = """あなたは日本語の一般読者として、次の記事本文だけを読んで用語を監査します。内部資料や外部知識は使わないでください。理解できない内部用語、未説明の専門語・外来語、曖昧な工程語を指摘します。各 quote は記事本文の完全一致の短い文にしてください。
JSONのみ: {{"contract_version":"1","summary":{{"judgment":"PASS|REVISE|BLOCKED","reason":"簡潔な総評"}},"findings":[{{"term":"語","severity":"BLOCK|WARN|INFO","type":"internal_jargon|unexplained_technical_term|unexplained_foreign_term|ambiguous_process_term","quote":"本文の完全一致引用","why":"理由","suggestion":"平易な案"}}]}}

# 記事本文
{article_md}"""

REVISE_FINDINGS_PROMPT = """元記事だけを根拠に、対象記事の指定された一文だけを局所修正してください。元記事にない具体的な主張、数値、因果、固有名詞、一般知識、属性（無料、非圧縮など）を追加してはいけません。根拠が足りない場合は cannot_revise にしてください。前後の段落・見出し・順序は変更しません。
# 翻訳規則
{rules}
# 元記事
{source_text}
# 対象記事の一文
{quote}
# 指摘
{finding}
JSONのみ: {{"revisions":[{{"quote":"修正前の完全一致文","replacement":"修正後の当該文だけ","source_evidence":"元記事の短い完全一致引用","status":"revised|cannot_revise"}}]}}"""

FACT_AUDIT_PROMPT = """記事本文だけと、人手で固定された重要事実チェックリストを照合してください。各事実について保持・欠落・変質を判定し、本文の対応引用を返してください。チェックリスト以外の事実を推測しないでください。
# チェックリスト
{checklist}
# 記事本文
{article_md}
JSONのみ: {{"findings":[{{"id":"F01","status":"kept|missing|changed","quote":"本文の対応引用または空","why":"簡潔な理由","source_evidence":"チェックリストの根拠引用"}}]}}"""

STRUCTURE_DIAGNOSIS_PROMPT = """固定アウトラインと記事本文を照合し、構造上の問題だけを診断してください。自動修正は提案しません。type は claim_missing, contradiction, transition_gap, term_before_definition, outline_mismatch のいずれかです。
# 固定アウトライン
{outline}
# 記事本文
{article_md}
JSONのみ: {{"findings":[{{"type":"claim_missing|contradiction|transition_gap|term_before_definition|outline_mismatch","severity":"FAIL|WARN|INFO","quote":"本文の完全一致引用または空","why":"簡潔な理由"}}]}}"""

LEDGER_REVISE_PROMPT = """元記事と重要事実チェックリストを根拠に、記事の指定された一文だけを修正してください。欠落した重要事実を補う必要がある場合も、元記事の根拠引用を必ず示し、それ以外は変更しません。
# 元記事
{source_text}
# チェックリスト
{checklist}
# 指摘
{finding}
JSONのみ: {{"revisions":[{{"quote":"修正前の完全一致文または空","replacement":"修正後の一文","source_evidence":"元記事の短い完全一致引用","status":"revised|cannot_revise"}}]}}"""


def faithful_jargon_prompt() -> str:
    rows = []
    for term, rule in JARGON_RULES.items():
        aliases = "、".join(rule.get("aliases", []))
        rows.append(f"- {term}{('（' + aliases + '）') if aliases else ''} → {rule['replacement']}")
    return FAITHFUL_JARGON_PROMPT.format(rules="\n".join(rows))


def is_allowed_external_term(term: str, quote: str) -> bool:
    if term not in EXTERNAL_TERMS:
        return False
    return any(token in (quote or "") for token in _EXTERNAL_EXPLANATIONS[term])


def validate_audit_payload(payload: dict, article_md: str = "") -> dict:
    """契約違反の findings を捨て、監査失敗は明示的に返す。"""
    if not isinstance(payload, dict) or payload.get("contract_version") != AUDIT_CONTRACT_VERSION:
        return {"valid": False, "error": "contract_version が不正です", "findings": []}
    summary = payload.get("summary")
    if not isinstance(summary, dict) or summary.get("judgment") not in {"PASS", "REVISE", "BLOCKED"}:
        return {"valid": False, "error": "summary が不正です", "findings": []}
    valid, rejected = [], []
    for finding in payload.get("findings", []):
        if not isinstance(finding, dict):
            rejected.append({"reason": "finding が object ではありません"}); continue
        if finding.get("severity") not in {"BLOCK", "WARN", "INFO"} or finding.get("type") not in {"internal_jargon", "unexplained_technical_term", "unexplained_foreign_term", "ambiguous_process_term"}:
            rejected.append({"finding": finding, "reason": "severity/type が不正です"}); continue
        quote = str(finding.get("quote") or "")
        if not quote or (article_md and quote not in article_md):
            rejected.append({"finding": finding, "reason": "quote が本文に完全一致しません"}); continue
        valid.append({k: str(finding.get(k) or "") for k in ("term", "severity", "type", "quote", "why", "suggestion")})
    return {"valid": not rejected, "summary": {"judgment": summary["judgment"], "reason": str(summary.get("reason") or "")}, "findings": valid, "rejected": rejected}
