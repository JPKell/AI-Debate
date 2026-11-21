"""Tree-sitter based symbol, import, and call-graph extractor for Python and JS."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_javascript as tsjs
import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

PY_LANGUAGE = Language(tspython.language())
JS_LANGUAGE = Language(tsjs.language())


@dataclass
class ExtractedSymbol:
    name: str
    qualified_name: str
    kind: str  # class | function | method
    start_line: int
    end_line: int
    signature: str
    docstring: str = ""


@dataclass
class ExtractedEdge:
    source_symbol: str
    target: str
    edge_type: str  # imports | calls
    line_number: int


@dataclass
class ParsedFile:
    language: str
    line_count: int
    symbols: list[ExtractedSymbol] = field(default_factory=list)
    edges: list[ExtractedEdge] = field(default_factory=list)


def _node_text(node: Node, src_bytes: bytes) -> str:
    return src_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _clean_docstring(raw: str) -> str:
    s = raw.strip()
    for quote in ('"""', "'''", '"', "'"):
        if s.startswith(quote) and s.endswith(quote) and len(s) >= 2 * len(quote):
            s = s[len(quote) : -len(quote)]
            break
    return " ".join(line.strip() for line in s.splitlines()).strip()[:400]


def _extract_py_docstring(body_node: Node | None, src_bytes: bytes) -> str:
    if body_node is None:
        return ""
    for child in body_node.named_children:
        if child.type == "expression_statement" and child.named_children:
            expr = child.named_children[0]
            if expr.type == "string":
                return _clean_docstring(_node_text(expr, src_bytes))
        break
    return ""


def _collect_py_calls(node: Node, src_bytes: bytes, owner: str, out: list[ExtractedEdge]) -> None:
    if node.type == "call":
        func = node.child_by_field_name("function")
        if func is not None:
            target = _node_text(func, src_bytes).strip()
            if target and len(target) <= 120:
                out.append(
                    ExtractedEdge(
                        source_symbol=owner,
                        target=target,
                        edge_type="calls",
                        line_number=node.start_point.row + 1,
                    )
                )
    for child in node.named_children:
        # Don't attribute nested function/class bodies to the outer owner
        if child.type in {"function_definition", "class_definition"}:
            continue
        _collect_py_calls(child, src_bytes, owner, out)


def _parse_python(src_bytes: bytes) -> ParsedFile:
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(src_bytes)
    root = tree.root_node
    result = ParsedFile(
        language="python",
        line_count=len(src_bytes.splitlines()),
    )

    def walk(node: Node, class_prefix: str = "") -> None:
        ntype = node.type

        if ntype == "import_statement":
            for child in node.named_children:
                if child.type in {"dotted_name", "aliased_import"}:
                    target = _node_text(child, src_bytes).split(" as ")[0].strip()
                    result.edges.append(
                        ExtractedEdge(
                            source_symbol="<module>",
                            target=target,
                            edge_type="imports",
                            line_number=node.start_point.row + 1,
                        )
                    )
            return

        if ntype == "import_from_statement":
            mod_node = node.child_by_field_name("module_name")
            mod_name = _node_text(mod_node, src_bytes).strip() if mod_node else ""
            imported_names: list[str] = []
            for child in node.named_children:
                if child is mod_node:
                    continue
                if child.type in {"dotted_name", "aliased_import"}:
                    name = _node_text(child, src_bytes).split(" as ")[0].strip()
                    imported_names.append(f"{mod_name}.{name}" if mod_name else name)
            if imported_names:
                for target in imported_names:
                    result.edges.append(
                        ExtractedEdge(
                            source_symbol="<module>",
                            target=target,
                            edge_type="imports",
                            line_number=node.start_point.row + 1,
                        )
                    )
            elif mod_name:
                result.edges.append(
                    ExtractedEdge(
                        source_symbol="<module>",
                        target=mod_name,
                        edge_type="imports",
                        line_number=node.start_point.row + 1,
                    )
                )
            return

        if ntype == "class_definition":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node:
                name = _node_text(name_node, src_bytes)
                qname = f"{class_prefix}.{name}" if class_prefix else name
                first_line = _node_text(node, src_bytes).splitlines()[0].strip()
                result.symbols.append(
                    ExtractedSymbol(
                        name=name,
                        qualified_name=qname,
                        kind="class",
                        start_line=node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        signature=first_line,
                        docstring=_extract_py_docstring(body, src_bytes),
                    )
                )
                if body:
                    for child in body.named_children:
                        walk(child, qname)
            return

        if ntype == "function_definition":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node:
                name = _node_text(name_node, src_bytes)
                qname = f"{class_prefix}.{name}" if class_prefix else name
                kind = "method" if class_prefix else "function"
                header_text = _node_text(node, src_bytes).split(":", 1)[0].strip()
                header_text = " ".join(header_text.split())
                result.symbols.append(
                    ExtractedSymbol(
                        name=name,
                        qualified_name=qname,
                        kind=kind,
                        start_line=node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        signature=header_text,
                        docstring=_extract_py_docstring(body, src_bytes),
                    )
                )
                if body:
                    _collect_py_calls(body, src_bytes, qname, result.edges)
            return

        if ntype == "decorated_definition":
            for child in node.named_children:
                if child.type in {"function_definition", "class_definition"}:
                    walk(child, class_prefix)
            return

        for child in node.named_children:
            walk(child, class_prefix)

    for child in root.named_children:
        walk(child)
    return result


def _collect_js_calls(node: Node, src_bytes: bytes, owner: str, out: list[ExtractedEdge]) -> None:
    if node.type == "call_expression":
        func = node.child_by_field_name("function")
        if func is not None:
            target = _node_text(func, src_bytes).strip()
            if target and len(target) <= 120:
                out.append(
                    ExtractedEdge(
                        source_symbol=owner,
                        target=target,
                        edge_type="calls",
                        line_number=node.start_point.row + 1,
                    )
                )
    for child in node.named_children:
        if child.type in {"function_declaration", "class_declaration", "method_definition"}:
            continue
        _collect_js_calls(child, src_bytes, owner, out)


def _parse_javascript(src_bytes: bytes) -> ParsedFile:
    parser = Parser(JS_LANGUAGE)
    tree = parser.parse(src_bytes)
    root = tree.root_node
    result = ParsedFile(
        language="javascript",
        line_count=len(src_bytes.splitlines()),
    )

    def walk(node: Node, class_prefix: str = "") -> None:
        ntype = node.type

        if ntype == "import_statement":
            source_node = node.child_by_field_name("source")
            if source_node:
                target = _node_text(source_node, src_bytes).strip("'\"")
                result.edges.append(
                    ExtractedEdge(
                        source_symbol="<module>",
                        target=target,
                        edge_type="imports",
                        line_number=node.start_point.row + 1,
                    )
                )
            return

        if ntype == "class_declaration":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node:
                name = _node_text(name_node, src_bytes)
                qname = f"{class_prefix}.{name}" if class_prefix else name
                first_line = _node_text(node, src_bytes).splitlines()[0].strip()
                result.symbols.append(
                    ExtractedSymbol(
                        name=name,
                        qualified_name=qname,
                        kind="class",
                        start_line=node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        signature=first_line,
                    )
                )
                if body:
                    for child in body.named_children:
                        walk(child, qname)
            return

        if ntype in {"function_declaration", "method_definition"}:
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node:
                name = _node_text(name_node, src_bytes)
                qname = f"{class_prefix}.{name}" if class_prefix else name
                kind = "method" if ntype == "method_definition" else "function"
                first_line = _node_text(node, src_bytes).split("{", 1)[0].strip()
                result.symbols.append(
                    ExtractedSymbol(
                        name=name,
                        qualified_name=qname,
                        kind=kind,
                        start_line=node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        signature=" ".join(first_line.split()),
                    )
                )
                if body:
                    _collect_js_calls(body, src_bytes, qname, result.edges)
            return

        for child in node.named_children:
            walk(child, class_prefix)

    for child in root.named_children:
        walk(child)
    return result


def parse_file(rel_path: Path, source: str) -> ParsedFile:
    """Parse a Python or JavaScript source file using Tree-sitter."""
    src_bytes = source.encode("utf-8")
    if rel_path.suffix == ".py":
        return _parse_python(src_bytes)
    if rel_path.suffix == ".js":
        return _parse_javascript(src_bytes)
    return ParsedFile(language="unknown", line_count=len(source.splitlines()))
