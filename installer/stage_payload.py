# -*- coding: utf-8 -*-
"""组装客户端交付 payload（安装版与便携版共用），全部输入来自本仓库源码。

反例修正：
- 旧流程用全局 basename 排除 readme.md，把
  internal/_sounddevice_data/portaudio-binaries/README.md 一并误删；
  本脚本不再依赖"按名字排除"，需要的文件逐一放行，最终整树校验。
- 旧 payload 从本机 dist 经 junction robocopy 而来，混入了 core/server
  （178 个文件）。本脚本只复制明确列出的客户端源码子集。
- 客户端包严禁携带任何密钥/凭据文件，出包前整树断言。
"""
import argparse
import hashlib
import json
import pathlib
import re
import shutil
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


PYCACHE = "__pycache__"

# 客户端源码白名单：core 下只带这些（严禁 core/server）
CORE_ROOT_FILES = ["__init__.py", "constants.py", "logger.py", "protocol.py"]
CORE_SUBDIRS = ["client", "tools", "ui"]
ASSET_FILES = ["icon.ico", "icon.png"]
USER_FILES = ["config_client.py", "hot.txt", "hot-server.txt", "hot-rule.txt"]
FORBIDDEN_BASENAMES = ["credentials.json", ".env"]


def copy_file(src: pathlib.Path, dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def copy_tree(src: pathlib.Path, dest: pathlib.Path, skip_parts=frozenset()) -> int:
    count = 0
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in skip_parts or part == PYCACHE for part in rel.parts):
            continue
        if path.is_dir():
            continue
        if path.is_symlink():
            raise SystemExit(f"payload 源码不允许符号链接: {path}")
        copy_file(path, dest / rel)
        count += 1
    return count


def apply_lan_default(config_path: pathlib.Path, lan_addr: str) -> None:
    """与历史交付一致：交付客户端默认连接局域网服务器。"""
    text = config_path.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r"(?m)^(\s*addr\s*=\s*)'[^']*'", r"\g<1>'" + lan_addr + "'", text
    )
    if n != 1:
        raise SystemExit(f"config_client.py 的 addr 赋值匹配了 {n} 处，期望 1 处")
    config_path.write_text(new_text, encoding="utf-8", newline="\n")
    changed = config_path.read_text(encoding="utf-8")
    if f"'{lan_addr}'" not in changed:
        raise SystemExit("LAN 默认地址未写入交付配置")


def verify_payload(payload: pathlib.Path) -> dict:
    errors = []
    for name in ("start_client.exe", "CapsWriterDesktop.exe", "config_client.py", "update-setup-wrapper.ps1"):
        if not (payload / name).is_file():
            errors.append(f"缺少 {name}")
    if not (payload / "internal").is_dir():
        errors.append("缺少 internal/")
    if not (payload / "internal/_sounddevice_data/portaudio-binaries/README.md").is_file():
        errors.append("internal/_sounddevice_data/portaudio-binaries/README.md 缺失（旧版全局排除反例）")
    for name in ("Desktop-README.md", "hot.txt", "hot-server.txt", "hot-rule.txt"):
        if not (payload / name).is_file():
            errors.append(f"缺少 {name}")
    if (payload / "start_server.exe").exists() or (payload / "start_admin.exe").exists():
        errors.append("payload 混入服务端可执行文件")
    if (payload / "core/server").exists():
        errors.append("payload 混入 core/server 源码")
    # 用户数据文件只允许出现在根目录（.iss 的 Excludes 是全局 basename）
    for name in USER_FILES:
        matches = [p for p in payload.rglob(name) if p.relative_to(payload).as_posix() != name]
        if matches:
            errors.append(f"{name} 出现在子目录（会导致安装器全局排除误删）: {matches[:3]}")
    manifest = {}
    for path in sorted(payload.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(payload).as_posix()
        if any(part == PYCACHE for part in path.relative_to(payload).parts):
            errors.append(f"payload 含 __pycache__: {rel}")
            continue
        base = path.name.lower()
        if base in FORBIDDEN_BASENAMES:
            errors.append(f"payload 含疑似凭据文件: {rel}")
        if "dpapi" in rel.lower() and rel.startswith("core/"):
            errors.append(f"payload 含 DPAPI 相关文件: {rel}")
        manifest[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    if errors:
        for e in errors:
            print("payload 校验失败: " + e, file=sys.stderr)
        raise SystemExit(2)
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="仓库根目录")
    ap.add_argument("--collect", required=True, help="PyInstaller COLLECT 输出目录 (CapsWriter-Client-Raw)")
    ap.add_argument("--gui-exe", required=True, help="已构建的 CapsWriterDesktop.exe")
    ap.add_argument("--out", required=True, help="payload 输出目录")
    ap.add_argument("--lan-addr", default="192.168.0.104")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的输出目录")
    args = ap.parse_args()

    repo = pathlib.Path(args.repo).resolve()
    collect = pathlib.Path(args.collect).resolve()
    gui_exe = pathlib.Path(args.gui_exe).resolve()
    out = pathlib.Path(args.out).resolve()

    if not (collect / "start_client.exe").is_file() or not (collect / "internal").is_dir():
        raise SystemExit(f"COLLECT 目录不完整: {collect}")
    if not gui_exe.is_file():
        raise SystemExit(f"GUI 可执行文件不存在: {gui_exe}")
    if out.exists():
        if not args.force:
            raise SystemExit(f"输出目录已存在（--force 覆盖）: {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # 1. PyInstaller 产物：入口 EXE 与 internal 依赖
    copy_file(collect / "start_client.exe", out / "start_client.exe")
    copy_tree(collect / "internal", out / "internal")

    # 2. 客户端源码（白名单，绝不包含 core/server）
    total = 0
    for name in CORE_ROOT_FILES:
        if not (repo / "core" / name).is_file():
            raise SystemExit(f"仓库缺少 core/{name}")
        copy_file(repo / "core" / name, out / "core" / name)
        total += 1
    for sub in CORE_SUBDIRS:
        if not (repo / "core" / sub).is_dir():
            raise SystemExit(f"仓库缺少 core/{sub}/")
        total += copy_tree(repo / "core" / sub, out / "core" / sub)
    print(f"core 源码文件: {total}")

    # 3. LLM 角色目录（客户端润色功能）
    if (repo / "LLM").is_dir():
        copy_tree(repo / "LLM", out / "LLM")

    # 4. 资源与用户可编辑文件
    for name in ASSET_FILES:
        if not (repo / "assets" / name).is_file():
            raise SystemExit(f"仓库缺少 assets/{name}")
        copy_file(repo / "assets" / name, out / "assets" / name)
    for name in USER_FILES:
        copy_file(repo / name, out / name)

    # 5. 桌面 GUI、热更新辅助脚本与说明文档
    copy_file(gui_exe, out / "CapsWriterDesktop.exe")
    if not (repo / "desktop" / "update-setup-wrapper.ps1").is_file():
        raise SystemExit("仓库缺少 desktop/update-setup-wrapper.ps1")
    copy_file(repo / "desktop" / "update-setup-wrapper.ps1", out / "update-setup-wrapper.ps1")
    copy_file(repo / "desktop" / "README.md", out / "Desktop-README.md")

    # 6. 交付默认服务器地址（与已验收 staged 客户端一致）
    apply_lan_default(out / "config_client.py", args.lan_addr)

    manifest = verify_payload(out)
    stamp = out.parent / (out.name + ".payload-manifest.json")
    stamp.write_text(
        json.dumps({"files": manifest, "lan_addr": args.lan_addr}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"payload 就绪: {out}（{len(manifest)} 个文件），清单: {stamp}")


if __name__ == "__main__":
    main()
