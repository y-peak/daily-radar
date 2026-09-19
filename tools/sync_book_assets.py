"""落地脚本：把 books.py 清单里的书稿（正文 + 插图）从上游仓库同步到本地。

## 为什么需要它

读书模块是**资产型**模块：正文与插图**离线**放在 `radar/static/books/`，
线上阅读完全不依赖上游仓库可达（断网、仓库被删也照读）。
但"离线"不等于"手工拷贝" —— 这一层必须有可重复的入口，否则
"书是哪儿来的、这一版对应哪个 commit"就没人说得清了。

所以规则是：

  * **清单是唯一事实来源**。要同步什么、正文在仓库哪个目录、图从哪来，
    全部来自 `radar/modules/reader/books.py`，这个脚本只负责执行。
  * **按 commit 钉死**。同步完会核对仓库 HEAD 是否等于清单里的 `commit`；
    不一致就**报警并要求显式 --allow-move**，不让内容悄悄漂移 ——
    否则"历史页面不可变"就成了空话。
  * **不做 checkout**。这些书仓库动辄几百 MB，还可能带 Git-LFS、
    以及历史里带 `:` 的非法 Windows 路径（`git checkout` 会直接报
    invalid path 失败）。所以走 `git show <rev>:<path>` 与
    `git cat-file`，只把用得到的 blob 取出来。

## 用法（项目根目录）

    python tools/sync_book_assets.py                # 同步全部书
    python tools/sync_book_assets.py ai-infra       # 只同步某一本
    python tools/sync_book_assets.py ai-infra --cache D:/book-src   # 复用已有克隆
    python tools/sync_book_assets.py --check        # 只体检，不写任何文件

克隆缓存默认放在 `~/.cache/radar-books/<slug>`（刻意放在项目外 —— 几百 MB
的裸仓库不该进 git，也不该被构建的清目录动作波及）。
"""
from __future__ import annotations

import argparse
import os
import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar.modules.reader import books as books_meta  # noqa: E402

IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


def cache_root() -> Path:
    env = os.environ.get("RADAR_BOOK_CACHE")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "radar-books"


def git_env() -> dict:
    env = dict(os.environ)
    key = os.environ.get("GITHUB_SSH_KEY", "~/.ssh/id_ed25519_github")
    env["GIT_SSH_COMMAND"] = f"ssh -i {os.path.expanduser(key)} -o StrictHostKeyChecking=accept-new"
    # 书仓库常挂 LFS，但正文和插图（md/svg/png）通常在普通 git 对象里；
    # 关掉 smudge 以免为无关的大文件白白排队下载。
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    return env


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, env=git_env(), **kw)


def ensure_repo(book: dict, cache: Path, refetch: bool = False) -> tuple[Path, str]:
    """保证本地有一份该仓库的克隆，返回 (目录, HEAD commit)。"""
    slug = book["slug"]
    dest = cache / slug
    if refetch and dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"  克隆 {book['repo']} → {dest}")
        run(["git", "clone", "--depth", "1", "--no-checkout", "--single-branch",
             book["repo"], str(dest)], capture_output=True)

    head = run(["git", "-C", str(dest), "rev-parse", "HEAD"],
               capture_output=True).stdout.decode().strip()
    return dest, head


def blob_at(repo: Path, rev: str, path: str) -> bytes:
    """取某个 revision 下单个文件的原始字节（不落盘仓库、不做 checkout）。"""
    return run(["git", "-C", str(repo), "show", f"{rev}:{path}"],
               capture_output=True).stdout


def tree_blobs(repo: Path, prefix: str) -> dict[str, str]:
    """prefix 下所有 blob 的 path -> oid。"""
    raw = run(["git", "-C", str(repo), "ls-tree", "-r", "-z", "HEAD", prefix + "/"],
              capture_output=True).stdout
    out: dict[str, str] = {}
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == b"blob":
            out[path.decode("utf-8", "surrogateescape")] = parts[2].decode()
    return out


def batch_blobs(repo: Path, oids: list[str]) -> dict[str, bytes]:
    """一次 cat-file --batch 取回全部 blob（本地对象，零网络往返）。"""
    if not oids:
        return {}
    proc = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=git_env())
    out = proc.communicate(("\n".join(oids) + "\n").encode())[0]
    got: dict[str, bytes] = {}
    pos, n = 0, len(out)
    while pos < n:
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        header = out[pos:nl].split()
        pos = nl + 1
        if len(header) < 3 or header[1] != b"blob":
            continue
        size = int(header[2])
        got[header[0].decode()] = out[pos:pos + size]
        pos += size + 1
    return got


def sync_book(book: dict, cache: Path, check_only: bool, allow_move: bool,
              refetch: bool) -> dict:
    slug = book["slug"]
    out_dir = ROOT / "radar" / "static" / books_meta.BOOKS_SUBDIR / slug
    root = book.get("source_root", "").strip("/")
    stat = {"slug": slug, "repo": book["repo"], "chapters": 0, "figures": 0,
            "missing_chapters": [], "missing_figures": [], "drift": False}

    repo, head = ensure_repo(book, cache, refetch)
    want = book.get("commit") or ""
    if want and head != want:
        stat["drift"] = True
        print(f"  ⚠️ HEAD {head[:8]} ≠ 清单 {want[:8]}")
        if not allow_move:
            print("     → 清单与上游不一致。要么核对后更新 books.py 的 commit，"
                  "要么加 --allow-move 强制按当前 HEAD 落地。")
            return stat
    stat["head"] = head

    # 1) 正文：清单里显式写死的章节文件名
    if not check_only:
        out_dir.mkdir(parents=True, exist_ok=True)
    for filename, _slug, _title in book["chapters"]:
        rel = f"{root}/{filename}" if root else filename
        try:
            data = blob_at(repo, head, rel)
        except subprocess.CalledProcessError:
            stat["missing_chapters"].append(filename)
            continue
        if data.startswith(LFS_POINTER):
            stat["missing_chapters"].append(filename + "（是 LFS 指针，非正文）")
            continue
        if not check_only:
            (out_dir / filename).write_bytes(data)
        stat["chapters"] += 1

    # 2) 插图：以"正文里真的引用了哪些"为准，而不是把仓库里的图全搬来
    refs: set[str] = set()
    src_texts: list[str] = []
    for filename, _slug, _title in book["chapters"]:
        p = out_dir / filename
        if check_only or not p.is_file():
            try:
                src_texts.append(blob_at(repo, head, f"{root}/{filename}".strip("/"))
                                 .decode("utf-8", "replace"))
            except subprocess.CalledProcessError:
                continue
        else:
            src_texts.append(p.read_text(encoding="utf-8"))
    for text in src_texts:
        for m in IMG_RE.finditer(text):
            refs.add(m.group(1))

    prefix = f"{root}/" if root else ""
    tmap = tree_blobs(repo, root) if root else tree_blobs(repo, ".")
    want_blobs: dict[str, str] = {}
    for ref in sorted(refs):
        clean = posixpath.normpath(ref.lstrip("./"))
        oid = tmap.get(f"{prefix}{clean}") or tmap.get(clean)
        if oid:
            want_blobs[oid] = clean
        else:
            stat["missing_figures"].append(ref)

    blobs = batch_blobs(repo, list(want_blobs))
    for oid, rel in want_blobs.items():
        data = blobs.get(oid)
        if not data:
            stat["missing_figures"].append(rel)
            continue
        if not check_only:
            dest = out_dir.joinpath(*rel.split("/"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        stat["figures"] += 1

    return stat


def main() -> int:
    ap = argparse.ArgumentParser(description="同步书稿正文与插图到 radar/static/books/")
    ap.add_argument("slugs", nargs="*", help="只同步这些 slug（默认全部）")
    ap.add_argument("--cache", type=Path, default=None, help="克隆缓存目录")
    ap.add_argument("--check", action="store_true", help="只体检，不写文件")
    ap.add_argument("--allow-move", action="store_true",
                    help="允许清单 commit 与上游 HEAD 不一致时仍按 HEAD 落地")
    ap.add_argument("--refetch", action="store_true", help="丢弃缓存重新克隆")
    args = ap.parse_args()

    cache = args.cache or cache_root()
    print(f"克隆缓存：{cache}")
    books = [b for b in books_meta.BOOKS
             if not args.slugs or b["slug"] in args.slugs]
    if not books:
        print("没有匹配的书。可用：" + ", ".join(b["slug"] for b in books_meta.BOOKS))
        return 2

    rc = 0
    for book in books:
        print(f"\n== {book['title']}（{book['slug']}）==")
        st = sync_book(book, cache, args.check, args.allow_move, args.refetch)
        print(f"  正文 {st['chapters']} 章 · 插图 {st['figures']} 张"
              f"{' · [check 模式未写盘]' if args.check else ''}")
        if st["missing_chapters"]:
            print(f"  ⚠️ 缺正文 {len(st['missing_chapters'])}：{st['missing_chapters'][:5]}")
            rc = 1
        if st["missing_figures"]:
            print(f"  ⚠️ 缺插图 {len(st['missing_figures'])}：{st['missing_figures'][:5]}")
            rc = 1
        if st["drift"] and not args.allow_move:
            rc = 1

    print("\n完成。改完书稿记得重建站点：python -m radar build")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
