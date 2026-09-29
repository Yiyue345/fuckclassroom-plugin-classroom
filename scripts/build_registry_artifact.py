from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path


PLUGIN_ID = "classroom"
SEMVER_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
ROOT_FILES = (
    "__init__.py",
    "plugin.json",
    "plugin.py",
    "accounts.py",
    "lifecycle.py",
    "routes.py",
    "services.py",
    "settings_context.py",
    "worker.py",
    "requirements.txt",
    "README.md",
)
TREE_DIRS = ("templates", "static")


def validate_manifest(manifest: dict[str, object], version: str) -> None:
    required = {
        "id", "version", "api_version", "app_requires", "execution", "entry",
        "requires", "python_requires", "route_prefix", "rpc_api_version",
        "rpc_permissions",
    }
    missing = sorted(required.difference(manifest))
    if missing:
        raise ValueError(f"plugin.json missing fields: {', '.join(missing)}")
    if manifest["id"] != PLUGIN_ID:
        raise ValueError("plugin id must be classroom")
    if manifest["version"] != version:
        raise ValueError("plugin.json version differs from artifact version")
    if manifest["api_version"] != 1:
        raise ValueError("plugin api_version must be 1")
    if manifest["execution"] != "in_process":
        raise ValueError("classroom must use in_process execution")
    if manifest["entry"] != "plugin.py":
        raise ValueError("plugin entry must be plugin.py")
    if manifest["requires"] != ["core_ui"]:
        raise ValueError("classroom must require core_ui")
    if (
        manifest["route_prefix"] is not None
        or manifest["rpc_api_version"] is not None
        or manifest["rpc_permissions"] != []
    ):
        raise ValueError("in-process plugin cannot declare RPC runtime fields")
    expected_python = [{
        "requirement": "playwright>=1.45",
        "import": "playwright",
        "label": "Playwright",
        "optional": False,
    }]
    if manifest["python_requires"] != expected_python:
        raise ValueError("python_requires must use Registry v1 structured records")


def build(version: str, root: Path) -> tuple[Path, Path]:
    if not SEMVER_RE.fullmatch(version):
        raise ValueError(f"invalid SemVer: {version}")

    source_manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    base_version = str(source_manifest["version"]).split("-", 1)[0]
    if version.split("-", 1)[0] != base_version:
        raise ValueError(
            f"release {version} does not match manifest base version {base_version}"
        )

    artifact = root / f"{PLUGIN_ID}-{version}.zip"
    checksum = root / f"{artifact.name}.sha256"
    artifact.unlink(missing_ok=True)
    checksum.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="classroom-package-") as temp:
        package = Path(temp)
        for relative in ROOT_FILES:
            source = root / relative
            if not source.is_file():
                raise FileNotFoundError(relative)
            shutil.copy2(source, package / relative)

        for directory in TREE_DIRS:
            source = root / directory
            if not source.is_dir():
                raise FileNotFoundError(directory)
            shutil.copytree(source, package / directory)

        manifest_path = package / "plugin.json"
        packaged_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        packaged_manifest["version"] = version
        validate_manifest(packaged_manifest, version)
        manifest_path.write_text(
            json.dumps(packaged_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with zipfile.ZipFile(
            artifact, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for source in sorted(package.rglob("*")):
                if source.is_file():
                    archive.write(source, source.relative_to(package).as_posix())

    with zipfile.ZipFile(artifact) as archive:
        names = archive.namelist()
        if names.count("plugin.json") != 1:
            raise ValueError("Registry v1 artifact must contain one root plugin.json")
        if any(name.startswith(f"{PLUGIN_ID}/") for name in names):
            raise ValueError("Registry v1 artifact must not have a wrapper directory")
        packaged_manifest = json.loads(archive.read("plugin.json").decode("utf-8"))
        validate_manifest(packaged_manifest, version)
        if packaged_manifest["entry"] not in names:
            raise ValueError("plugin entry is missing from artifact")

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")
    return artifact, checksum


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    artifact, checksum = build(args.version, root)
    print(artifact.name)
    print(checksum.name)


if __name__ == "__main__":
    main()
