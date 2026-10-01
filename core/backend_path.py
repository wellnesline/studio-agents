"""別途用意する動画バックエンドの配置を解決する共通規約。"""
from __future__ import annotations

import os
from pathlib import Path


STUDIO_AGENTS_ROOT = Path(__file__).resolve().parent.parent


def load_studio_agents_env() -> None:
    """Load missing variables from studio-agents/.env without overriding the shell.

    API clients in slide-studio have their own loader and override policy.  This
    loader intentionally only fills gaps so that this package can provide its
    deployment-only settings (notably VIDEO_BACKEND_DIR) without conflicting with
    that policy.
    """
    env_file = STUDIO_AGENTS_ROOT / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


# Every CLI imports this module directly or through its backend resolver.
load_studio_agents_env()


def looks_like_backend(path: Path) -> bool:
    """解決先が実際に別途用意する動画バックエンドかを確かめる。

    存在するだけの同名ディレクトリを掴むと、 後段で原因の分かりにくいエラーになる。
    """
    return (path / "api").is_dir() and (path / "tools" / "slidestudio_build.py").exists()


def resolve_backend_dir() -> Path:
    """環境変数、続いて隣接配置の順で backend を解決する。

    ★ slide-studio/api/video_el.py の同名関数と探索順を必ず揃えること。
      片方だけ変えると、 studio-agents の CLI と slide-studio の描画側が
      別の backend を掴み、 症状が出るまで気づけない。
    """
    configured = os.environ.get("VIDEO_BACKEND_DIR", "").strip()
    if configured:
        # 明示指定は利用者の意思なので、 中身が backend に見えなくても尊重する。
        return Path(configured).expanduser().resolve()

    studio_agents = STUDIO_AGENTS_ROOT
    distributed = studio_agents.parent / "video-backend" / "backend"
    if looks_like_backend(distributed):
        return distributed.resolve()

    return distributed.resolve()
