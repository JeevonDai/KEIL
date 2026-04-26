#!/usr/bin/env python3
# -*- coding: gbk -*-
"""
clangd_index_helper.py

功能:
1) 扫描工程内所有 .h 头文件目录，并写入 .clangd 的 CompileFlags.Add 中(-I)
2) 基于 .clangd 的 Add 参数生成 compile_commands.json
3) 统一补充 Keil ARMCC 标准头路径，解决 stdio.h 等系统头报错
4) 支持双配置: gcc-clangd(默认) / armcc-compat
5) 补充 STM32Cube FW F4 V1.24.1 Third_Party(LwIP) 索引路径
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple


DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".svn",
    ".hg",
    ".idea",
    ".vscode",
    "__pycache__",
    "build",
    "Build",
    "output",
    "Output",
    "listing",
    "Listing",
}

SOURCE_EXTS_C = {".c"}
SOURCE_EXTS_CPP = {".cc", ".cpp", ".cxx"}
SOURCE_EXTS_ASM = {".s", ".S", ".asm"}
HEADER_EXTS = {".h", ".hpp", ".hh", ".hxx"}

# 集中维护 Keil ARMCC include 路径，避免在代码中到处硬编码。
KEIL_ARMCC_INCLUDE = (
    "C:/Users/Administrator/AppData/Local/Keil_v5/ARM/ARMCompiler_506_Windows_x86_b960/include"
)
KEIL_ARMCC_INCLUDE_FLAG = f"-isystem{KEIL_ARMCC_INCLUDE}"
STM32CUBE_F4_1241_THIRD_PARTY = (
    "C:/Users/Administrator/STM32Cube/Repository/STM32Cube_FW_F4_V1.24.1/Middlewares/Third_Party"
)
STM32CUBE_F4_1241_THIRD_PARTY_LWIP_INC = (
    "C:/Users/Administrator/STM32Cube/Repository/STM32Cube_FW_F4_V1.24.1/Middlewares/Third_Party/LwIP/src/include"
)
STM32CUBE_THIRD_PARTY_FLAGS = [
    f"-I{STM32CUBE_F4_1241_THIRD_PARTY}",
    f"-I{STM32CUBE_F4_1241_THIRD_PARTY_LWIP_INC}",
]
PROFILE_BEGIN_MARKER = "# BEGIN PROFILE FLAGS (managed by clangd_index_helper.py)"
PROFILE_END_MARKER = "# END PROFILE FLAGS"
ARMCC_PROFILE_FLAGS = ["-D__CC_ARM", "-D__ARMCC_VERSION=5060960", "-D__MICROLIB"]
COMMON_COMPAT_FLAGS = ["-fms-extensions"]
# Force-include common C runtime headers from Keil toolchain include path
# to reduce unresolved symbols in mixed legacy projects.
KEIL_FORCE_INCLUDE_HEADERS = [
    "stdint.h",
    "stddef.h",
    "stdbool.h",
    "limits.h",
    "float.h",
    "string.h",
    "stdlib.h",
    "stdio.h",
    "ctype.h",
    "math.h",
    "errno.h",
    "time.h",
]


def is_excluded(path: Path, project_root: Path, exclude_dirs: set[str]) -> bool:
    try:
        rel = path.relative_to(project_root)
    except ValueError:
        return True
    for part in rel.parts:
        if part in exclude_dirs:
            return True
    return False


def normalize_rel_include(path: Path, project_root: Path) -> Tuple[str, str]:
    rel = path.relative_to(project_root).as_posix()
    # Keep both styles:
    # -I for <> includes and compatibility
    # -iquote for "" includes to improve project-local header resolution
    return f"-I./{rel}", f"-iquote./{rel}"


def scan_header_dirs(project_root: Path, exclude_dirs: set[str]) -> List[str]:
    include_dirs = set()
    for h in project_root.rglob("*"):
        if not h.is_file():
            continue
        if h.suffix.lower() not in HEADER_EXTS:
            continue
        if is_excluded(h, project_root, exclude_dirs):
            continue
        i_flag, iquote_flag = normalize_rel_include(h.parent, project_root)
        include_dirs.add(i_flag)
        include_dirs.add(iquote_flag)
    return sorted(include_dirs)


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _extract_add_block(lines: List[str]) -> Tuple[int, int, int]:
    """
    返回 (add_start_line, add_end_line_exclusive, item_indent)
    找不到时返回 (-1, -1, -1)
    """
    compile_idx = -1
    add_idx = -1
    for i, line in enumerate(lines):
        if re.match(r"^\s*CompileFlags:\s*$", line):
            compile_idx = i
            break
    if compile_idx < 0:
        return -1, -1, -1

    for i in range(compile_idx + 1, len(lines)):
        if re.match(r"^\S", lines[i]):
            break
        if re.match(r"^\s*Add:\s*$", lines[i]):
            add_idx = i
            break
    if add_idx < 0:
        return -1, -1, -1

    add_indent = len(lines[add_idx]) - len(lines[add_idx].lstrip(" "))
    item_indent = add_indent + 2
    start = add_idx + 1
    end = start
    for i in range(start, len(lines)):
        line = lines[i]
        if not line.strip():
            end = i + 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= add_indent and re.match(r"^\s*[A-Za-z].*:\s*$", line):
            break
        if indent < item_indent and line.strip():
            break
        end = i + 1
    return start, end, item_indent


def parse_clangd_add_flags(clangd_text: str) -> List[str]:
    lines = clangd_text.splitlines()
    start, end, _ = _extract_add_block(lines)
    if start < 0:
        return []
    flags = []
    for line in lines[start:end]:
        m = re.match(r"^\s*-\s*(.+?)\s*$", line)
        if m:
            flags.append(m.group(1))
    return flags


def update_clangd_profile_flags(clangd_path: Path, profile: str) -> None:
    text = read_text(clangd_path)
    if not text.strip():
        return

    lines = text.splitlines()
    start, end, item_indent = _extract_add_block(lines)
    if start < 0:
        return

    existing = lines[start:end]
    indent_spaces = " " * item_indent

    b_idx = -1
    e_idx = -1
    for i, line in enumerate(existing):
        if PROFILE_BEGIN_MARKER in line:
            b_idx = i
        if PROFILE_END_MARKER in line:
            e_idx = i
            break

    profile_block = [
        f"{indent_spaces}{PROFILE_BEGIN_MARKER}",
    ]
    profile_block.append(f"{indent_spaces}# Common compatibility flags")
    profile_block.extend([f"{indent_spaces}- {f}" for f in COMMON_COMPAT_FLAGS])
    profile_block.append(f"{indent_spaces}# Force include Keil runtime headers")
    for hdr in KEIL_FORCE_INCLUDE_HEADERS:
        profile_block.append(f"{indent_spaces}- -include")
        profile_block.append(f"{indent_spaces}- {hdr}")
    if profile == "armcc-compat":
        profile_block.append(f"{indent_spaces}# Profile: armcc-compat")
        profile_block.extend([f"{indent_spaces}- {f}" for f in ARMCC_PROFILE_FLAGS])
    else:
        profile_block.append(f"{indent_spaces}# Profile: gcc-clangd (default)")
    profile_block.append(f"{indent_spaces}{PROFILE_END_MARKER}")

    if b_idx >= 0 and e_idx >= b_idx:
        replaced = existing[:b_idx] + profile_block + existing[e_idx + 1 :]
    else:
        replaced = profile_block + existing

    new_lines = lines[:start] + replaced + lines[end:]
    write_text(clangd_path, "\n".join(new_lines).rstrip() + "\n")


def update_clangd_with_includes(clangd_path: Path, auto_includes: List[str]) -> None:
    begin_marker = "# BEGIN AUTO HEADER INCLUDES"
    end_marker = "# END AUTO HEADER INCLUDES"

    text = read_text(clangd_path)
    if not text.strip():
        generated = [
            "CompileFlags:",
            "  Add:",
            "    - -xc",
            "    - -std=gnu11",
            f"    {begin_marker}",
            *[f"    - {inc}" for inc in auto_includes],
            f"    {end_marker}",
            "",
        ]
        write_text(clangd_path, "\n".join(generated))
        return

    lines = text.splitlines()
    start, end, item_indent = _extract_add_block(lines)

    if start < 0:
        append_block = [
            "",
            "CompileFlags:",
            "  Add:",
            "    - -xc",
            "    - -std=gnu11",
            f"    {begin_marker}",
            *[f"    - {inc}" for inc in auto_includes],
            f"    {end_marker}",
        ]
        write_text(clangd_path, text.rstrip() + "\n" + "\n".join(append_block) + "\n")
        return

    existing = lines[start:end]
    indent_spaces = " " * item_indent

    b_idx = -1
    e_idx = -1
    for i, line in enumerate(existing):
        if begin_marker in line:
            b_idx = i
        if end_marker in line:
            e_idx = i
            break

    auto_block = [
        f"{indent_spaces}{begin_marker}",
        *[f"{indent_spaces}- {inc}" for inc in auto_includes],
        f"{indent_spaces}{end_marker}",
    ]

    if b_idx >= 0 and e_idx >= b_idx:
        replaced = existing[:b_idx] + auto_block + existing[e_idx + 1 :]
    else:
        if existing and existing[-1].strip():
            replaced = existing + [f"{indent_spaces}{begin_marker}"]
            replaced = replaced[:-1] + auto_block
        else:
            replaced = existing + auto_block

    new_lines = lines[:start] + replaced + lines[end:]
    write_text(clangd_path, "\n".join(new_lines).rstrip() + "\n")


def iter_files(project_root: Path, exts: set[str], exclude_dirs: set[str]) -> Iterable[Path]:
    for p in project_root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in exts:
            continue
        if is_excluded(p, project_root, exclude_dirs):
            continue
        yield p


def build_compile_command(
    directory: Path, compiler: str, flags: Sequence[str], src_file: Path, lang: str
) -> dict:
    src = src_file.as_posix()
    if lang == "c":
        xflag = ["-x", "c"]
    elif lang == "cpp":
        xflag = ["-x", "c++"]
    elif lang == "header":
        xflag = ["-x", "c-header"]
    else:
        xflag = ["-x", "assembler-with-cpp"]
    command_list = [compiler, *flags, *xflag, "-c", src, "-o", "NUL"]
    return {
        "directory": directory.as_posix(),
        "file": src,
        "arguments": command_list,
    }


def generate_compile_commands(
    project_root: Path,
    clangd_flags: List[str],
    out_path: Path,
    exclude_dirs: set[str],
) -> Tuple[int, int, int]:
    entries = []
    c_cnt = h_cnt = asm_cnt = 0

    compiler = "clang"
    filtered_flags = []
    for f in clangd_flags:
        if f.startswith("-x"):
            continue
        filtered_flags.append(f)

    has_target = any(f.startswith("--target=") or f == "--target" for f in filtered_flags)
    if not has_target:
        filtered_flags.insert(0, "--target=arm-none-eabi")
    if KEIL_ARMCC_INCLUDE_FLAG not in filtered_flags:
        filtered_flags.insert(1, KEIL_ARMCC_INCLUDE_FLAG)
    for ext_flag in STM32CUBE_THIRD_PARTY_FLAGS:
        if ext_flag not in filtered_flags:
            filtered_flags.append(ext_flag)

    for c in iter_files(project_root, SOURCE_EXTS_C, exclude_dirs):
        entries.append(build_compile_command(project_root, compiler, filtered_flags, c, "c"))
        c_cnt += 1
    for cpp in iter_files(project_root, SOURCE_EXTS_CPP, exclude_dirs):
        entries.append(build_compile_command(project_root, compiler, filtered_flags, cpp, "cpp"))
    for h in iter_files(project_root, HEADER_EXTS, exclude_dirs):
        entries.append(build_compile_command(project_root, compiler, filtered_flags, h, "header"))
        h_cnt += 1
    for s in iter_files(project_root, SOURCE_EXTS_ASM, exclude_dirs):
        entries.append(build_compile_command(project_root, compiler, filtered_flags, s, "asm"))
        asm_cnt += 1

    write_text(out_path, json.dumps(entries, indent=2, ensure_ascii=False) + "\n")
    return c_cnt, h_cnt, asm_cnt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="扫描头文件更新 .clangd，并生成 compile_commands.json"
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="工程根目录，默认当前目录",
    )
    parser.add_argument(
        "--clangd",
        default=".clangd",
        help=".clangd 文件路径(相对 project-root)",
    )
    parser.add_argument(
        "--compile-commands",
        default="compile_commands.json",
        help="compile_commands.json 输出路径(相对 project-root)",
    )
    parser.add_argument(
        "--exclude",
        default="",
        help="额外排除目录，逗号分隔，如: build,Output,.cache",
    )
    parser.add_argument(
        "--no-update-clangd",
        action="store_true",
        help="只生成 compile_commands，不更新 .clangd",
    )
    parser.add_argument(
        "--no-generate-commands",
        action="store_true",
        help="只更新 .clangd，不生成 compile_commands",
    )
    parser.add_argument(
        "--profile",
        choices=["gcc-clangd", "armcc-compat"],
        default="gcc-clangd",
        help="clangd 宏配置档位: gcc-clangd(默认) 或 armcc-compat",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    project_root = Path(args.project_root).resolve()
    clangd_path = (project_root / args.clangd).resolve()
    compile_commands_path = (project_root / args.compile_commands).resolve()

    exclude_dirs = set(DEFAULT_EXCLUDE_DIRS)
    if args.exclude.strip():
        exclude_dirs.update({x.strip() for x in args.exclude.split(",") if x.strip()})

    header_dirs = scan_header_dirs(project_root, exclude_dirs)
    print(f"[INFO] 扫描到头文件目录: {len(header_dirs)}")

    if not args.no_update_clangd:
        update_clangd_with_includes(clangd_path, header_dirs)
        update_clangd_profile_flags(clangd_path, args.profile)
        print(f"[INFO] 已更新 .clangd: {clangd_path}")
        print(f"[INFO] 当前 profile: {args.profile}")

    clangd_text = read_text(clangd_path)
    clangd_flags = parse_clangd_add_flags(clangd_text)
    if not clangd_flags:
        print("[WARN] 未从 .clangd 解析到 CompileFlags.Add，compile_commands 可能不完整")

    if not args.no_generate_commands:
        c_cnt, h_cnt, asm_cnt = generate_compile_commands(
            project_root, clangd_flags, compile_commands_path, exclude_dirs
        )
        print(f"[INFO] 已生成 compile_commands.json: {compile_commands_path}")
        print(
            "[INFO] 索引条目统计: "
            f".c={c_cnt}, .h={h_cnt}, .asm/.s={asm_cnt}"
        )

    print("[DONE] 完成。建议执行: clangd: Restart language server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

