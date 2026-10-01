"""画風の保存・使い回し(4エージェント共通)。「同じテイストで作りたい」に応える。

- 保存先: studio-agents/styles.json(1ファイル・追記)
- entry: {name, agent, kind, value, created}
    kind="theme"     … slide-studio のテーマ名(Slide/Blog/Slidecast系)
    kind="art_style" … 自由文の画風アートディレクション(Blogcast の gpt-image 用)
- 各エージェントは build 時に使った画風を last-<agent> として自動保存し、
  --save-style NAME で名前を付けて保存、--theme/--art に保存名を渡すと解決して使い回す。

同時実行への備え:
  4 エージェントが同じ 1 ファイルを read-modify-write するため、素朴に書くと
  後勝ちで更新が消える。書き込みは一時ファイル + os.replace で原子的に行い、
  read-modify-write 全体を lock file で囲う。
"""
from __future__ import annotations
import json
import os
import pathlib
import sys
import tempfile
import time

_STORE = pathlib.Path(__file__).resolve().parents[1] / "styles.json"
_LOCK = _STORE.with_name("styles.json.lock")
_LOCK_TIMEOUT = 10.0     # 取れなければ警告して続行する(画風保存で処理を止めない)
_LOCK_STALE = 60.0       # 異常終了で残ったロックを掴んだままにしない


class _StoreLock:
    """styles.json の read-modify-write を守るプロセス間ロック。

    依存を増やしたくないので O_CREAT|O_EXCL による lock file 方式にする
    (Windows / POSIX 共通)。取得できなくても保存自体は続行し、警告だけ出す。
    """

    def __enter__(self) -> "_StoreLock":
        self._fd: int | None = None
        deadline = time.time() + _LOCK_TIMEOUT
        while True:
            try:
                self._fd = os.open(str(_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if _lock_is_stale():
                    _remove_lock()
                    continue
                if time.time() > deadline:
                    print("[warn] styles.json のロックを取得できませんでした。"
                          "同時実行中の場合、画風の保存が競合する可能性があります。",
                          file=sys.stderr)
                    return self
                time.sleep(0.05)
            except OSError:
                return self   # ロックが作れない環境でも保存自体は続ける

    def __exit__(self, *exc) -> bool:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            _remove_lock()
        return False


def _lock_is_stale() -> bool:
    try:
        return (time.time() - _LOCK.stat().st_mtime) > _LOCK_STALE
    except OSError:
        return False


def _remove_lock() -> None:
    try:
        _LOCK.unlink()
    except OSError:
        pass


def _load() -> dict:
    if not _STORE.exists():
        return {"entries": []}
    try:
        d = json.loads(_STORE.read_text(encoding="utf-8-sig"))
        if not isinstance(d, dict) or not isinstance(d.get("entries"), list):
            raise ValueError("entries が配列ではありません")
        return d
    except Exception as e:   # noqa: BLE001
        # 壊れていても黙って空を返さない。空のまま次の save に進むと、
        # 保存済みの画風がすべて消える。退避してから空で始める。
        backup = _STORE.with_name(f"styles.broken-{time.strftime('%Y%m%d-%H%M%S')}.json")
        try:
            _STORE.replace(backup)
            print(f"[warn] styles.json を読めませんでした({e})。"
                  f"{backup.name} に退避し、新しい保存庫で続行します。", file=sys.stderr)
        except OSError:
            print(f"[warn] styles.json を読めませんでした({e})。", file=sys.stderr)
        return {"entries": []}


def _save(d: dict) -> None:
    """一時ファイルに書いてから差し替える。 中断しても既存を壊さない。"""
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_STORE.parent), prefix=".styles-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _STORE)   # 同一ボリューム内では原子的
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def save(name: str, agent: str, kind: str, value: str) -> None:
    """(name, kind) で保存(同名同kindは上書き)。kind が違えば別エントリ=名前衝突で壊さない(B5)。"""
    with _StoreLock():
        d = _load()
        d["entries"] = [e for e in d["entries"]
                        if not (e.get("name") == name and e.get("kind") == kind)]
        d["entries"].append({"name": name, "agent": agent, "kind": kind, "value": value,
                             "created": time.strftime("%Y-%m-%d %H:%M")})
        _save(d)


def get(name: str) -> dict | None:
    for e in _load()["entries"]:
        if e.get("name") == name:
            return e
    return None


def list_all() -> list[dict]:
    return list(_load()["entries"])


def resolve(value_or_name: str, kind: str) -> str | None:
    """引数が保存名(同kind)ならその value を、生値(テーマ名/自由文)ならそのままを返す。
    保存名だが kind 違い(例: slide の --theme に blogcast の画風名)は警告して None(B5:
    黙って既定に落ちたり、名前文字列が画像プロンプトに混入したりするのを防ぐ)。"""
    if not value_or_name:
        return None
    matches = [e for e in _load()["entries"] if e.get("name") == value_or_name]
    if matches:
        same = next((e for e in matches if e.get("kind") == kind), None)
        if same:
            return same.get("value")
        kinds = ",".join(e.get("kind", "?") for e in matches)
        print(f"[warn] 保存スタイル「{value_or_name}」は種類が違います(保存={kinds} / 要求={kind})。"
              f"このエージェントでは使えません。", file=sys.stderr)
        return None
    return value_or_name
