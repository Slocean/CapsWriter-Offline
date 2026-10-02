# -*- coding: utf-8 -*-
"""从与安装器完全相同的 payload 构建便携版单文件 EXE。

payload 内所有文件（包括 internal/_sounddevice_data/portaudio-binaries/
README.md）都进入自解压缓存；不再按 basename 排除任何 readme。
"""
import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")



def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--payload", required=True)
    ap.add_argument("--out", required=True, help="输出单文件 EXE 路径")
    ap.add_argument("--version", required=True)
    ap.add_argument("--record", required=True, help="构建记录 JSON 输出路径")
    args = ap.parse_args()

    repo = pathlib.Path(args.repo).resolve()
    payload = pathlib.Path(args.payload).resolve()
    out = pathlib.Path(args.out).resolve()
    if not (payload / "CapsWriterDesktop.exe").is_file() or not (payload / "start_client.exe").is_file():
        raise SystemExit(f"payload 不完整: {payload}")
    if (payload / "start_server.exe").exists() or (payload / "start_admin.exe").exists():
        raise SystemExit("payload 混入服务端可执行文件")
    out.parent.mkdir(parents=True, exist_ok=True)

    archive = out.with_suffix(".exe.payload.zip")
    manifest = {}
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(payload.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(payload).as_posix()
            if "__pycache__" in rel.split("/"):
                continue
            if path.is_symlink():
                raise SystemExit(f"payload 不允许符号链接: {path}")
            data = path.read_bytes()
            manifest[rel] = hashlib.sha256(data).hexdigest()
            z.writestr(rel, data)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()

    template = (repo / "installer" / "PortableLauncher.cs").read_text(encoding="utf-8")
    source = template.replace("__PAYLOAD_HASH__", sha).replace("__VERSION__", args.version)
    leftover = re.findall(r"__[A-Z][A-Z_]*__", source)
    if leftover:
        raise SystemExit(f"PortableLauncher 模板存在未替换占位符: {leftover}")
    generated = out.with_suffix(".exe.generated.cs")
    generated.write_text(source, encoding="utf-8-sig")

    csc = pathlib.Path("C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe")
    if not csc.is_file():
        raise SystemExit(f"缺少 C# 编译器: {csc}")
    fx = csc.parent
    subprocess.run(
        [
            str(csc), "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
            f"/out:{out}",
            f"/win32icon:{repo / 'assets' / 'icon.ico'}",
            f"/resource:{archive},CapsWriter.Payload.zip",
            "/reference:System.Windows.Forms.dll",
            f"/reference:{fx / 'System.IO.Compression.dll'}",
            "/reference:System.IO.Compression.FileSystem.dll",
            str(generated),
        ],
        check=True,
    )
    archive.unlink()
    generated.unlink()

    record = {
        "portable_exe": str(out),
        "version": args.version,
        "size_bytes": out.stat().st_size,
        "sha256": sha256_file(out),
        "payload_zip_sha256": sha,
        "files": manifest,
        "contains_no_credentials": not any(
            pathlib.PurePosixPath(n).name.lower() in ("credentials.json", ".env") for n in manifest
        ),
        "no_installation_or_shortcuts": True,
    }
    pathlib.Path(args.record).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in record.items() if k != "files"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
