"""Shared test helpers: load the script as a module, build throwaway git repositories, and a tiny JSON-schema checker (no third-party package)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "deadline-watch"
SCRIPT = SKILL / "scripts" / "deadline_watch.py"
SCHEMA = json.loads((SKILL / "references" / "output.schema.json").read_text())

_spec = importlib.util.spec_from_file_location("deadline_watch", SCRIPT)
dw = importlib.util.module_from_spec(_spec)
import sys  # noqa: E402

sys.modules["deadline_watch"] = dw
_spec.loader.exec_module(dw)

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_NAME": "Test",
           "GIT_COMMITTER_EMAIL": "test@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def git(cwd: Path, *args: str) -> str:
    """Test-side git (this MAY write: it builds the fixtures; the program under test never does)."""
    done = subprocess.run(["git", "-C", str(cwd), "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, env=GIT_ENV, check=True)
    return done.stdout


def commit(repo: Path, name: str = "a.txt", text: str = "x\n", message: str = "c") -> None:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    return path


def make_pushed_repo(tmp: Path, name: str = "work") -> tuple[Path, Path]:
    """(clone, bare remote): one commit, pushed, upstream set."""
    remote = tmp / f"{name}-remote.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "-b", "main")
    clone = make_repo(tmp / name)
    commit(clone)
    git(clone, "remote", "add", "origin", str(remote))
    git(clone, "push", "-q", "-u", "origin", "main")
    return clone, remote


def problems(value, schema, root=None, path="$") -> list[str]:
    """Just enough JSON Schema for output.schema.json: type, enum, const, required, properties, items, minimum, anyOf, local $ref. Returns the list of problems."""
    root = root or schema
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return problems(value, node, root, path)
    if "anyOf" in schema:
        options = [problems(value, option, root, path) for option in schema["anyOf"]]
        return [] if any(not o for o in options) else [f"{path}: matches none of the alternatives"]
    found: list[str] = []
    kinds = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}
    if "type" in schema:
        wanted = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(isinstance(value, kinds[k]) and not (k in ("integer", "number") and isinstance(value, bool)) for k in wanted):
            return [f"{path}: {value!r} is not {wanted}"]
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{path}: {value!r} not in {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        found.append(f"{path}: {value!r} != {schema['const']!r}")
    if "minimum" in schema and value < schema["minimum"]:
        found.append(f"{path}: {value} < {schema['minimum']}")
    if isinstance(value, dict):
        found += [f"{path}: missing {key!r}" for key in schema.get("required", []) if key not in value]
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                found += problems(value[key], sub, root, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for number, item in enumerate(value):
            found += problems(item, schema["items"], root, f"{path}[{number}]")
    return found


def validate(value, schema) -> None:
    found = problems(value, schema)
    assert not found, "; ".join(found[:5])
