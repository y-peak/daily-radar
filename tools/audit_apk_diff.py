"""审计工具：把两个 APK 的字节布局摊开对比。

起因：1.1 和 1.2 的**文件总大小完全相同**（都是 111757），
但 1.2 的 classes.dex 明显更大（16204 vs 11232）。
正常情况下 1.2 应该更大 —— 一定有什么东西在反向补偿。
这个脚本就是为了把补偿项找出来，而不是靠猜。
"""
from __future__ import annotations

import struct
import sys
import zipfile
from pathlib import Path

SIG_MAGIC = b"APK Sig Block 42"


def last_local_end(b: bytes, z: zipfile.ZipFile) -> int:
    """所有 local file entry 的结束偏移。"""
    end = 0
    for i in z.infolist():
        h = i.header_offset
        name_len, extra_len = struct.unpack("<HH", b[h + 26:h + 30])
        end = h + 30 + name_len + extra_len + i.compress_size
    return end


def analyze(p: Path) -> dict:
    b = p.read_bytes()
    z = zipfile.ZipFile(p)
    eocd = b.rfind(b"PK\x05\x06")
    cd_size, cd_off = struct.unpack("<II", b[eocd + 12:eocd + 20])

    # APK Signing Block 布局：[size(8)][id-value 对][size(8)][magic(16)]
    #   magic 在**块的末尾**。那个 size 字段在 magic 之前 8 字节 ——
    #   ⚠️ 一开始我读成了 magic 之后 8 字节（读进 magic 里去了），
    #   结果算出一个天文数字，整个"填充量"字段是废的。别再犯。
    j = b.rfind(SIG_MAGIC, 0, cd_off)
    blk_start = blk_end = blk_total = None
    if j != -1:
        blk_end = j + len(SIG_MAGIC)
        sz = struct.unpack("<Q", b[j - 8:j])[0]
        blk_total = 8 + sz                     # 含开头那个 size 字段
        blk_start = blk_end - blk_total

    le = last_local_end(b, z)
    ent_sum = sum(i.compress_size for i in z.infolist())
    return {
        "size": len(b),
        "entries_comp": ent_sum,
        "last_local_end": le,
        "sig_block_start": blk_start,
        "sig_block_total": blk_total,
        "filler_before_sig": (blk_start - le) if blk_start is not None else None,
        "cd_off": cd_off,
        "cd_size": cd_size,
        "cd_align4096": cd_off % 4096,
        "eocd": eocd,
        "tail": len(b) - (eocd + 22),
        "n_entries": len(z.infolist()),
        "dex_comp": next((i.compress_size for i in z.infolist()
                          if i.filename == "classes.dex"), 0),
        "arsc_raw": next((i.file_size for i in z.infolist()
                          if i.filename == "resources.arsc"), 0),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("用法: audit_apk_diff.py <a.apk> <b.apk>", file=sys.stderr)
        return 2
    a, b = Path(argv[1]), Path(argv[2])
    ra, rb = analyze(a), analyze(b)
    keys = list(ra.keys())
    print(f"{'字段':<20} {'A=' + a.name:>22} {'B=' + b.name:>22} {'B-A':>10}")
    print("-" * 78)
    for k in keys:
        va, vb = ra[k], rb[k]
        if isinstance(va, tuple):
            print(f"{k:<20} {str(va):>22} {str(vb):>22}")
            continue
        d = (vb - va) if (va is not None and vb is not None) else ""
        print(f"{k:<20} {va:>22} {vb:>22} {d if d == '' else f'{d:+d}':>10}")

    print()
    print("=== 推断补偿来源 ===")
    da = (rb["size"] - ra["size"])
    print(f"  总大小差            {da:+d}   ← 若为 0，说明下面两项正好抵消")
    d_comp = rb["entries_comp"] - ra["entries_comp"]
    dfill = (rb["filler_before_sig"] or 0) - (ra["filler_before_sig"] or 0)
    dtail = rb["tail"] - ra["tail"]
    print(f"  条目压缩和之差      {d_comp:+d}")
    print(f"  签名块前填充之差    {dfill:+d}")
    print(f"  尾部(CD+EOCD)之差   {dtail:+d}")
    print(f"  三项之和            {d_comp + dfill + dtail:+d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
