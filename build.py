"""Release builder — runs PyInstaller over ``TastyFwdFactor.spec`` and packages
the result for the platform it is run on.

    pip install pyinstaller
    python build.py                # build for this platform
    python build.py --clean        # wipe build/ and dist/ first

There is no cross-compiling: run it on Windows for the .exe and on macOS for
the .zip. Artifacts land in ``dist/``:

    Windows   dist/TastyFwdFactor.exe          (double-click to run)
    macOS     dist/TastyFwdFactor-macos-<arch>.zip
              → unzips to TastyFwdFactor.app, drag to /Applications

The macOS zip is written with ``ditto -c -k --keepParent``, not ``zip`` or
``shutil.make_archive``. A .app bundle is a tree of symlinks — every embedded Qt
framework has ``Versions/Current -> A`` and top-level links into it — and an
archiver that dereferences them produces a bundle that is several times larger
and, worse, fails to launch or to verify its own signature. ``ditto`` is also
the only one that carries extended attributes and resource forks across.
"""

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "TastyFwdFactor.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def run(cmd, **kw):
    print("$", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT, **kw)


def build(clean):
    if clean:
        for path in (BUILD, DIST):
            if path.exists():
                print(f"removing {path}")
                shutil.rmtree(path)

    # Invoke through the *current* interpreter so the build uses the venv the
    # dependencies are installed in, rather than whichever pyinstaller is first
    # on PATH.
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", str(SPEC)])


def package_macos():
    app = DIST / "TastyFwdFactor.app"
    if not app.is_dir():
        sys.exit(f"expected {app} — did PyInstaller fail?")

    # Ad-hoc sign the bundle. Unsigned or partially-signed .apps built on Apple
    # silicon are killed on launch ("is damaged"); a `-` identity costs nothing
    # and is enough for a locally-distributed build. --deep because PyInstaller
    # has already written the nested frameworks.
    try:
        run(["codesign", "--force", "--deep", "--sign", "-", str(app)])
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"warning: ad-hoc codesign failed ({exc}); the .app may need "
              f"`xattr -cr` on the target machine", file=sys.stderr)

    zip_path = DIST / f"TastyFwdFactor-macos-{platform.machine()}.zip"
    zip_path.unlink(missing_ok=True)
    # -c create, -k PKZip format, --keepParent so it unzips as the .app itself
    # rather than spilling Contents/ into the download folder. See module
    # docstring for why this is ditto and not zip.
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
         str(app), str(zip_path)])
    return zip_path


def package_windows():
    exe = DIST / "TastyFwdFactor.exe"
    if not exe.is_file():
        sys.exit(f"expected {exe} — did PyInstaller fail?")
    return exe


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clean", action="store_true",
                    help="remove build/ and dist/ before building")
    args = ap.parse_args()

    if not SPEC.is_file():
        sys.exit(f"missing {SPEC}")

    build(args.clean)

    if sys.platform == "darwin":
        artifact = package_macos()
        note = ("Users unzipping a downloaded build may need to clear the "
                "quarantine flag:\n    xattr -cr /Applications/TastyFwdFactor.app")
    elif sys.platform.startswith("win"):
        artifact = package_windows()
        note = ("SmartScreen will warn on an unsigned .exe: "
                "More info → Run anyway.")
    else:
        # Linux isn't a release target, but the spec builds there fine and it is
        # useful for checking that the analysis picks everything up.
        artifact = DIST / "TastyFwdFactor"
        note = "Linux is not a release target; this build is for testing only."

    size = artifact.stat().st_size / 1e6 if artifact.exists() else 0
    print(f"\nBuilt: {artifact}  ({size:.0f} MB)\n{note}")


if __name__ == "__main__":
    main()
