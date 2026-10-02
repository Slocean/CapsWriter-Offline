# -*- coding: utf-8 -*-
"""发版校验与更新清单生成（参考 OneLedger scripts/validate-release.mjs）。

版本唯一来源：installer/version.txt。
公告：installer/announcement.md，首行标题必须包含该版本号。

  python scripts/validate_release.py                     # 校验版本与公告
  python scripts/validate_release.py --notes OUT.md      # 生成 Release 正文
  python scripts/validate_release.py --manifest --portable P.exe --setup S.exe \
      --out-dir DIR                                      # 生成更新清单 + SHA256SUMS
"""
import argparse
import datetime
import hashlib
import json
import pathlib
import re
import sys

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
REPO = pathlib.Path(__file__).resolve().parents[1]
OWNER_REPO = "Slocean/CapsWriter-Offline"


def read_version() -> str:
    version = (REPO / "installer" / "version.txt").read_text(encoding="utf-8").strip()
    if not VERSION_RE.match(version):
        raise SystemExit(f"installer/version.txt 版本无效：{version!r}（必须是无前导零的 X.Y.Z）")
    return version


def read_announcement(version: str) -> tuple:
    text = (REPO / "installer" / "announcement.md").read_text(encoding="utf-8").strip()
    lines = text.splitlines()
    if not lines:
        raise SystemExit("installer/announcement.md 是空的")
    title = lines[0].strip().lstrip("#").strip()
    if version not in title:
        raise SystemExit(f"公告首行 {title!r} 未包含版本号 {version}（公告首项、版本唯一来源必须同步）")
    if len(text) < 80:
        raise SystemExit("更新公告内容过短，请补充中文说明")
    return title, text


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes", metavar="OUT", help="生成 Release 正文 Markdown")
    parser.add_argument("--manifest", action="store_true", help="生成 CapsWriter-Update-Manifest.json 与 SHA256SUMS.txt")
    parser.add_argument("--portable", metavar="EXE")
    parser.add_argument("--setup", metavar="EXE")
    parser.add_argument("--out-dir", metavar="DIR", help="清单输出目录（默认与 --portable 同目录）")
    args = parser.parse_args()

    version = read_version()
    title, announcement = read_announcement(version)
    print(f"OK: 版本 v{version} · {title}")

    if args.notes:
        notes = pathlib.Path(args.notes)
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text(title + "\n\n" + announcement + "\n", encoding="utf-8")
        print(f"Release 正文: {notes}")

    if args.manifest:
        if not args.portable or not args.setup:
            raise SystemExit("--manifest 需要 --portable 与 --setup 两个 EXE 路径")
        portable = pathlib.Path(args.portable).resolve()
        setup = pathlib.Path(args.setup).resolve()
        expected = {
            "portable": f"CapsWriter-Portable-{version}.exe",
            "setup": f"CapsWriter-Setup-{version}.exe",
        }
        if portable.name != expected["portable"]:
            raise SystemExit(f"便携版文件名 {portable.name} 与版本不符，应为 {expected['portable']}")
        if setup.name != expected["setup"]:
            raise SystemExit(f"安装版文件名 {setup.name} 与版本不符，应为 {expected['setup']}")
        for path in (portable, setup):
            if not path.is_file():
                raise SystemExit(f"缺少构建产物: {path}")
        out_dir = pathlib.Path(args.out_dir).resolve() if args.out_dir else portable.parent
        assets = {}
        for flavor, path in (("portable", portable), ("setup", setup)):
            assets[flavor] = {
                "name": path.name,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "url": f"https://github.com/{OWNER_REPO}/releases/download/v{version}/{path.name}",
            }
        manifest = {
            "schema": 1,
            "product": "CapsWriter-Offline-Client",
            "latest": version,
            "title": title,
            "notice": title,
            "announcement": announcement,
            "published_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "channel": f"https://github.com/{OWNER_REPO}/releases/latest/download/CapsWriter-Update-Manifest.json",
            "assets": assets,
        }
        manifest_path = out_dir / "CapsWriter-Update-Manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        sums = out_dir / "SHA256SUMS.txt"
        lines = []
        for flavor in ("portable", "setup"):
            lines.append(f"{assets[flavor]['sha256']} *{assets[flavor]['name']}")
        lines.append(f"{sha256_file(manifest_path)} *{manifest_path.name}")
        sums.write_text("\n".join(lines) + "\n", encoding="ascii")
        print(f"更新清单: {manifest_path}")
        print(f"校验和:   {sums}")
        for flavor in ("portable", "setup"):
            print(f"{flavor}: {assets[flavor]['sha256']} ({assets[flavor]['size']} bytes)")

    if not args.notes and not args.manifest:
        return


if __name__ == "__main__":
    try:
        main()
    except SystemExit as error:
        if isinstance(error.code, str):
            print(error.code, file=sys.stderr)
            sys.exit(1)
        raise
