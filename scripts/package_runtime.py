# SPDX-License-Identifier: GPL-3.0-only OR MIT
"""维护者工具，从锁定的官方包重建本仓库的 Windows x64 离线卷。"""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VOLUME_BYTES = 32 * 1024 * 1024
ENGINE_SHA = "7bbe9f9a108b46cb9ae2e9164d3fbbfe7f8655a9ab9c377a203064a45a822079"
PYTHON_SHA = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"
POWERSHELL_SHA = "ce7484804c0f0ad4dda10c958e71acdaf7eebfb22505c1b480f89e0e9f1da94d"


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def expect(path, expected):
    if digest(path) != expected:
        raise ValueError(f"文件校验失败：{path.name}")


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError(f"归档出现非法路径：{name}")
    return path


def save_entry(output, target, data):
    info = zipfile.ZipInfo(str(safe_name(str(target))), date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    output.writestr(info, data, compresslevel=6)


def wheel_destination(name):
    path = safe_name(name)
    if not path.parts[0].endswith(".data"):
        return PurePosixPath("Lib/site-packages") / path
    kind = path.parts[1]
    dest = {"purelib": "Lib/site-packages", "platlib": "Lib/site-packages", "scripts": "Scripts",
            "headers": "Include", "data": ""}[kind]
    return PurePosixPath(dest).joinpath(*path.parts[2:])


def python_archive(downloads, wheels, work):
    archive = downloads / "python-3.13.17-embed-amd64.zip"
    expect(archive, PYTHON_SHA)
    lock = json.loads((ROOT / "scripts/wheels.lock.json").read_text(encoding="utf-8"))
    output = work / "python-3.13.17-genshin.zip"
    with zipfile.ZipFile(output, "w") as target, zipfile.ZipFile(archive) as source:
        for item in sorted(source.namelist()):
            if item.endswith("/"):
                continue
            data = source.read(item)
            if item == "python313._pth":
                data = b"python313.zip\r\n.\r\nLib/site-packages\r\n../../server\r\nimport site\r\n"
            save_entry(target, item, data)
        (ROOT / "licenses/python/CPython.txt").write_bytes(source.read("LICENSE.txt"))
        written = {name.casefold() for name in target.namelist()}
        for entry in lock:
            wheel = wheels / entry["filename"]
            expect(wheel, entry["sha256"])
            with zipfile.ZipFile(wheel) as source_wheel:
                for name in sorted(source_wheel.namelist()):
                    if name.endswith("/"):
                        continue
                    location = wheel_destination(name)
                    key = str(location).casefold()
                    if key in written:
                        raise ValueError(f"wheel 文件冲突：{location}")
                    written.add(key)
                    data = source_wheel.read(name)
                    save_entry(target, location, data)
                    if ".dist-info/" in name:
                        remainder = name.split(".dist-info/", 1)[1]
                        if remainder.lower().startswith(("licenses/", "license", "notice", "copying")):
                            notice = ROOT / "licenses/python" / entry["name"] / safe_name(remainder)
                            notice.parent.mkdir(parents=True, exist_ok=True)
                            notice.write_bytes(data)
    return output


def split_bundle(key, version, archive, expected, required):
    expect(archive, expected)
    folder = ROOT / "bundles" / f"{key}-{version}"
    folder.mkdir(parents=True, exist_ok=True)
    parts = []
    with archive.open("rb") as file:
        while block := file.read(VOLUME_BYTES):
            target = folder / f"part-{len(parts) + 1:03d}.bin"
            checksum = hashlib.sha256(block).hexdigest()
            if not target.exists() or digest(target) != checksum:
                temp = target.with_suffix(".partial")
                temp.write_bytes(block)
                temp.replace(target)
            parts.append({"file": target.relative_to(ROOT / "bundles").as_posix(),
                          "bytes": len(block), "sha256": checksum})
    return {"version": version, "archive": archive.name, "bytes": archive.stat().st_size,
            "sha256": expected, "required": required, "parts": parts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", type=Path, default=ROOT / ".build/downloads")
    parser.add_argument("--wheels", type=Path, default=ROOT / ".build/wheels")
    args = parser.parse_args()
    work = ROOT / ".build/pack"
    work.mkdir(parents=True, exist_ok=True)
    pws = args.downloads / "PowerShell-7.6.9-win-x64.zip"
    expect(pws, POWERSHELL_SHA)
    with zipfile.ZipFile(pws) as z:
        for name in ("LICENSE.txt", "ThirdPartyNotices.txt"):
            (ROOT / "licenses/powershell" / name).write_bytes(z.read(name))
    py = python_archive(args.downloads, args.wheels, work)
    components = {
        "bettergi": split_bundle("bettergi", "0.67.0", args.downloads / "BetterGI_v0.67.0.7z", ENGINE_SHA,
                                 ["BetterGI/BetterGI.exe", "BetterGI/Assets/Model/LICENSE"]),
        "python": split_bundle("python", "3.13.17", py, digest(py),
                                ["python.exe", "python313._pth", "Lib/site-packages/cv2/__init__.py"]),
        "powershell": split_bundle("powershell", "7.6.9", pws, POWERSHELL_SHA,
                                    ["pwsh.exe", "UIAutomationClient.dll", "ThirdPartyNotices.txt"]),
    }
    bootstrap = {name: digest(ROOT / "bin" / name) for name in ("node.exe", "7za.exe")}
    data = {"format": 1, "platform": "win32-x64", "bootstrap": bootstrap, "components": components}
    manifest = ROOT / "bundles/manifest.json"
    intermediate = manifest.with_suffix(".partial")
    intermediate.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    intermediate.replace(manifest)
    for name, spec in components.items():
        print(f"{name} {spec['version']}，{len(spec['parts'])} 卷，{spec['bytes']:,} 字节")


if __name__ == "__main__":
    main()
