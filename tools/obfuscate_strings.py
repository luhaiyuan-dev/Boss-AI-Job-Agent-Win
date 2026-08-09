"""为 Nuitka 构建创建临时源码副本，并混淆核心策略字符串。

正式源码保持可读；只有 ``build/nuitka/staging`` 中的构建副本会被转换。该层
用于阻断 strings/AI 的快速明文提取，不宣称能够抵御有经验的动态逆向。
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import secrets
import shutil
import zlib
from pathlib import Path

PROTECTED_MODULES = (
    "boss_assistant/automation/api_provider.py",
    "boss_assistant/automation/review.py",
    "boss_assistant/automation/policy.py",
    "boss_assistant/automation/matching.py",
    "boss_assistant/automation/mysql_store.py",
    "boss_assistant/storage/repository.py",
)
SQL_MARKERS = (
    "SELECT ",
    "INSERT ",
    "UPDATE ",
    "DELETE ",
    "CREATE TABLE",
    "ALTER TABLE",
    "FROM ",
    "WHERE ",
)


def _is_sensitive(value: str) -> bool:
    if len(value) >= 80:
        return True
    if len(value) >= 12 and any(
        "\u4e00" <= character <= "\u9fff" for character in value
    ):
        return True
    upper = value.upper()
    return len(value) >= 12 and any(marker in upper for marker in SQL_MARKERS)


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    result: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if not node.body or not isinstance(node.body[0], ast.Expr):
            continue
        value = node.body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            result.add(id(value))
    return result


class _StringTransformer(ast.NodeTransformer):
    def __init__(self, docstrings: set[int]) -> None:
        self.docstrings = docstrings
        self.values: dict[str, str] = {}
        self.parents: list[ast.AST] = []

    def generic_visit(self, node: ast.AST) -> ast.AST:
        self.parents.append(node)
        try:
            return super().generic_visit(node)
        finally:
            self.parents.pop()

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        value = node.value
        parent = self.parents[-1] if self.parents else None
        if (
            not isinstance(value, str)
            or id(node) in self.docstrings
            or not _is_sensitive(value)
            or isinstance(parent, (ast.JoinedStr, ast.MatchValue))
        ):
            return node
        token = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
        self.values[token] = value
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="_ps_decode", ctx=ast.Load()),
                args=[ast.Constant(token)],
                keywords=[],
            ),
            node,
        )


def _insert_decoder_import(tree: ast.Module) -> None:
    import_node = ast.ImportFrom(
        module="boss_assistant._protected_strings",
        names=[ast.alias(name="decode", asname="_ps_decode")],
        level=0,
    )
    index = 0
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        index = 1
    while (
        index < len(tree.body)
        and isinstance(tree.body[index], ast.ImportFrom)
        and tree.body[index].module == "__future__"
    ):
        index += 1
    tree.body.insert(index, import_node)


def _keystream(key: bytes, token: str, size: int) -> bytes:
    chunks: list[bytes] = []
    counter = 0
    while sum(map(len, chunks)) < size:
        chunks.append(
            hashlib.blake2b(
                key + token.encode("ascii") + counter.to_bytes(4, "big"),
                digest_size=64,
            ).digest()
        )
        counter += 1
    return b"".join(chunks)[:size]


def _encrypt(value: str, token: str, key: bytes) -> str:
    packed = zlib.compress(value.encode("utf-8"), level=9)
    stream = _keystream(key, token, len(packed))
    encrypted = bytes(left ^ right for left, right in zip(packed, stream, strict=True))
    return base64.b85encode(encrypted).decode("ascii")


def _decoder_module(values: dict[str, str], key: bytes) -> str:
    encrypted = {token: _encrypt(value, token, key) for token, value in values.items()}
    parts = [key[index : index + 8].hex() for index in range(0, len(key), 8)][::-1]
    lines = [
        '"""Generated build-only protected string table."""',
        "",
        "import base64 as _b",
        "import hashlib as _h",
        "import zlib as _z",
        "",
        f"_PARTS = {tuple(parts)!r}",
        "_DATA = {",
    ]
    for token, payload in sorted(encrypted.items()):
        lines.append(f"    {token!r}: {payload!r},")
    lines.extend(
        (
            "}",
            "_CACHE = {}",
            "",
            "def _key():",
            "    return b''.join(bytes.fromhex(part) for part in reversed(_PARTS))",
            "",
            "def decode(token):",
            "    cached = _CACHE.get(token)",
            "    if cached is not None:",
            "        return cached",
            "    payload = _b.b85decode(_DATA[token].encode('ascii'))",
            "    chunks = []",
            "    counter = 0",
            "    key = _key()",
            "    while sum(map(len, chunks)) < len(payload):",
            "        chunks.append(_h.blake2b(key + token.encode('ascii') + counter.to_bytes(4, 'big'), digest_size=64).digest())",
            "        counter += 1",
            "    stream = b''.join(chunks)[:len(payload)]",
            "    packed = bytes(left ^ right for left, right in zip(payload, stream, strict=True))",
            "    value = _z.decompress(packed).decode('utf-8')",
            "    _CACHE[token] = value",
            "    return value",
            "",
        )
    )
    return "\n".join(lines)


def build_staging(project_root: Path, output: Path) -> tuple[int, int]:
    project_root = project_root.resolve()
    output = output.resolve()
    allowed_root = (project_root / "build").resolve()
    if output == allowed_root or allowed_root not in output.parents:
        raise ValueError(f"staging目录必须位于 {allowed_root} 的子目录中")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    shutil.copytree(project_root / "boss_assistant", output / "boss_assistant")
    shutil.copy2(project_root / "run_control_panel.py", output / "run_control_panel.py")
    (output / "tools").mkdir()
    shutil.copy2(
        project_root / "tools" / "open_login_edge.py",
        output / "tools" / "open_login_edge.py",
    )

    all_values: dict[str, str] = {}
    transformed_count = 0
    for relative in PROTECTED_MODULES:
        path = output / relative
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        transformer = _StringTransformer(_docstring_node_ids(tree))
        tree = transformer.visit(tree)
        assert isinstance(tree, ast.Module)
        _insert_decoder_import(tree)
        ast.fix_missing_locations(tree)
        path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")
        all_values.update(transformer.values)
        transformed_count += len(transformer.values)

    decoder = _decoder_module(all_values, secrets.token_bytes(32))
    (output / "boss_assistant" / "_protected_strings.py").write_text(
        decoder, encoding="utf-8"
    )
    return transformed_count, len(all_values)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    transformed, unique = build_staging(args.project_root, args.output)
    print(f"已创建Nuitka临时构建副本：{args.output.resolve()}")
    print(f"已混淆字符串引用 {transformed} 处，唯一明文 {unique} 条。")


if __name__ == "__main__":
    main()
