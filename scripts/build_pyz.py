"""Build a single-file ``smart-updater.pyz`` that runs on any machine with Python 3.9+.

    python scripts/build_pyz.py            # writes dist/smart-updater.pyz
    python dist/smart-updater.pyz doctor   # Linux and Windows (py dist\\smart-updater.pyz doctor)
"""
import pathlib
import zipapp

root = pathlib.Path(__file__).resolve().parent.parent
out = root / "dist" / "smart-updater.pyz"
out.parent.mkdir(exist_ok=True)
zipapp.create_archive(
    root / "src",
    out,
    interpreter="/usr/bin/env python3",
    main="smart_updater.cli:main",
    filter=lambda p: "__pycache__" not in p.parts and p.suffix != ".pyc",
)
print(f"wrote {out} ({out.stat().st_size // 1024} KiB)")
