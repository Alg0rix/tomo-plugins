"""Validate catalog metadata without importing or running plugins."""
import ast
import json
from pathlib import Path
import sys

import jsonschema

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / "marketplace.json").read_text())
jsonschema.validate(manifest, json.loads((root / "schema/marketplace.schema.json").read_text()))
ids = [entry["id"] for entry in manifest["plugins"]]
assert len(ids) == len(set(ids)), "Duplicate plugin identities"
if (root / "scripts/build_catalog.py").exists():
    submitted = [json.loads(path.read_text()) for path in sorted((root / "plugins").glob("*.json"))]
    assert manifest["plugins"] == submitted, "Run python scripts/build_catalog.py and commit marketplace.json"
else:
    for entry in manifest["plugins"]:
        path = root / entry["source"]["subdirectory"]
        assert path.resolve().is_relative_to(root), "Plugin path escapes collection"
        metadata = json.loads((path / "tomo-plugin.json").read_text())
        assert all(metadata[key] == entry[key] for key in ("id", "version", "sdk_version"))
        ast.parse((path / "plugin.py").read_text())
print(f"Validated {len(ids)} catalog entries; no plugin code executed")
