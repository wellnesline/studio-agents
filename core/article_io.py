"""記事の入出力に使う、副作用を持たない小さな共通関数。"""
from __future__ import annotations

import markdown as md_lib


def _legacy_h1_lines(md: str) -> list[str]:
    """`wp_publish.extract_title` と同じ `# ` 規則で H1 を収集する。"""
    return [line.strip()[2:].strip() for line in md.splitlines()
            if line.strip().startswith("# ")]


def extract_legacy_markdown_title(md: str) -> str | None:
    """現行 WordPress 投稿時のタイトル抽出規則で Markdown のタイトルを返す。

    `wp_publish.extract_title` を正とする。先頭の空行は飛ばす一方、最初の非空行が
    `# ` でない場合は、後続に H1 があってもタイトルなしとする。

    NOTE: `blog_core._source_title` は最初の非空行から任意個の `#` を除去する
    別規則のフォールバックであり、この互換関数には採用しない。
    """
    for line in md.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("# "):
            return text[2:].strip() or None
        return None
    return None


def markdown_to_html_pure(md: str) -> str:
    """画像の置換・アップロードを行わずに Markdown を HTML へ変換する。"""
    return md_lib.markdown(
        md,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
    )


def validate_article_title(article_json: dict, markdown_h1: str | None) -> list[str]:
    """article.json の title と Markdown H1 の不整合を警告文字列で返す。"""
    warnings: list[str] = []
    article_title = str(article_json.get("title") or "")
    if markdown_h1 is None:
        warnings.append("Markdown に先頭 H1 (`# `) がありません")
    elif article_title != markdown_h1:
        warnings.append(
            f'article.json の title ({article_title!r}) と Markdown H1 ({markdown_h1!r}) が一致しません'
        )
    if len(_legacy_h1_lines(str(article_json.get("markdown") or ""))) > 1:
        warnings.append("Markdown に H1 (`# `) が複数あります")
    return warnings
