"""ブログ記事の型(構成テンプレート)レジストリ。slide-studio の structures.py の記事版。

recommend が1つ選び、build_prompt がこの章立てを記事の背骨にする(元ネタに無い章は飛ばす)。
mode(faithful/seo_long=肉付けの度合い)とは直交する別軸(structure=記事の骨格)。
"""
from __future__ import annotations

STRUCTURES = [
    {"id": "explainer", "name": "やさしい解説", "framework": "そもそも→仕組み→なぜ重要→具体例→まとめ",
     "desc": "初心者に概念をかみ砕いて説明する",
     "chapters": ["導入(つかみ)", "そもそも何か", "仕組み・全体像", "なぜ重要か", "具体例", "注意点", "まとめ"]},
    {"id": "how-to", "name": "ハウツー(手順)", "framework": "課題→準備→手順→つまずき→まとめ",
     "desc": "やり方を手順で示す",
     "chapters": ["導入(課題)", "必要なもの・前提", "手順1", "手順2", "手順3", "よくあるつまずき", "まとめ"]},
    {"id": "listicle", "name": "まとめ(◯選)", "framework": "導入→項目列挙→選び方→まとめ",
     "desc": "要点やおすすめを列挙する",
     "chapters": ["導入", "項目1", "項目2", "項目3", "項目4", "選び方のコツ", "まとめ"]},
    {"id": "comparison", "name": "比較", "framework": "導入→比較軸→A→B→タイプ別→結論",
     "desc": "複数を軸で比較して選択を助ける",
     "chapters": ["導入", "比較のポイント", "選択肢A", "選択肢B", "タイプ別のおすすめ", "まとめ"]},
    {"id": "case-study", "name": "事例・体験談", "framework": "背景→課題→試したこと→結果→学び",
     "desc": "実例やビフォーアフターで示す",
     "chapters": ["導入", "背景", "直面した課題", "試したこと", "結果", "学び・応用", "まとめ"]},
    {"id": "news-commentary", "name": "ニュース解説", "framework": "何が起きた→背景→影響→今後",
     "desc": "出来事を噛み砕いて解説する",
     "chapters": ["何が起きたか", "背景・経緯", "なぜ重要か", "影響", "今後の見通し", "まとめ"]},
    {"id": "opinion", "name": "オピニオン(主張)", "framework": "主張→根拠→反論への応答→結論",
     "desc": "立場を示して論じる",
     "chapters": ["導入(問題提起)", "主張", "根拠1", "根拠2", "想定される反論", "結論"]},
    {"id": "service-guide", "name": "手続きガイド(可否判定・費用・手順)",
     "framework": "結論→可否判定→最初の行動→選択肢→費用の見積もり→手順と担当→完了後の対応→補足・FAQ",
     "desc": "サービスや手続きを使えるか判断し、費用を見積もり、必要な手順を完了する",
     "chapters": ["結論・最初にすること", "使えるかの判定", "選択肢と選び方", "費用の見積もり",
                  "手順と担当", "完了後の対応", "例外・補足・FAQ"]},
]
_BY_ID = {s["id"]: s for s in STRUCTURES}


def get(sid: str):
    return _BY_ID.get(sid)


def default():
    return STRUCTURES[0]   # explainer
