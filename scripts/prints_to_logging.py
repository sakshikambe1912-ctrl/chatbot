r"""Turn print(...) calls in your app code into logging calls.

    python scripts\prints_to_logging.py            preview only (changes nothing)
    python scripts\prints_to_logging.py --write    rewrite the files

Only plain print(...) statements inside chatbot, catalog and ingestion are changed. Test files,
migrations, management commands (they use self.stdout) and demo/scratch scripts are skipped.
A message containing FAILED, EXCEPTION, ERROR, SKIPPED or REJECTED becomes log.warning, the rest log.info.
"""
import argparse
import ast
import re
import sys
from pathlib import Path

DEFAULT_DIRS = ["chatbot", "catalog", "ingestion"]
SKIP_DIRS = {"migrations", "management", "__pycache__", "static", ".venv", "venv"}
SKIP_FILE = re.compile(r"^(test_.*|tests?|.*_test|scratch_.*|.*_demo|conftest)\.py$")
WARN_RE = re.compile(r"FAIL|EXCEPTION|ERROR|SKIPPED|REJECTED|UNAVAILABLE", re.I)


def _is_print_stmt(node):
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name) and node.value.func.id == "print"
            and node.value.args and not node.value.keywords
            and not any(isinstance(a, ast.Starred) for a in node.value.args))


def _logger_name(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in ("log", "logger") and "getLogger" in ast.dump(node.value):
            return node.targets[0].id
    return None


def convert(source):
    """Returns (new_source, number_of_prints_converted, skipped_description_list)."""
    tree = ast.parse(source)
    data = source.encode("utf-8")
    lines = data.splitlines(keepends=True)
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln)
    name = _logger_name(tree) or "log"

    edits = []
    for node in ast.walk(tree):
        if not _is_print_stmt(node):
            continue
        call = node.value
        seg = lambda n: ast.get_source_segment(source, n)
        args = call.args
        first = args[0]
        whole = seg(call) or ""
        level = "warning" if WARN_RE.search(whole) else "info"
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if len(args) == 1:
                new = f"{name}.{level}({seg(first)})"
            else:
                text = first.value.replace("%", "%%") + " %s" * (len(args) - 1)
                new = f"{name}.{level}({text!r}, {', '.join(seg(a) for a in args[1:])})"
        elif isinstance(first, ast.JoinedStr) and len(args) == 1:
            new = f"{name}.{level}({seg(first)})"
        else:
            fmt = " ".join(["%s"] * len(args))
            new = f"{name}.{level}({fmt!r}, {', '.join(seg(a) for a in args)})"
        a = starts[call.lineno - 1] + call.col_offset
        b = starts[call.end_lineno - 1] + call.end_col_offset
        edits.append((a, b, new.encode("utf-8")))

    if not edits:
        return source, 0, []
    for a, b, new in sorted(edits, reverse=True):
        data = data[:a] + new + data[b:]
    out = data.decode("utf-8")

    if _logger_name(tree) is None:                       # add "import logging" and the logger
        nl = "\r\n" if "\r\n" in source else "\n"
        tree2 = ast.parse(out)
        imports = [n for n in tree2.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        has_logging = any(isinstance(n, ast.Import) and any(x.name == "logging" for x in n.names) for n in imports)
        out_lines = out.splitlines(keepends=True)
        future = [n for n in imports if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
        non_future = [n for n in imports if n not in future]
        last = max((n.end_lineno for n in imports), default=0)
        out_lines.insert(last, f"{nl}log = logging.getLogger(__name__){nl}")
        if not has_logging:
            first_line = (non_future[0].lineno - 1) if non_future else (future[-1].end_lineno if future else 0)
            out_lines.insert(first_line, f"import logging{nl}")
        out = "".join(out_lines)
    return out, len(edits), []


def files(roots):
    for root in roots:
        for p in sorted(Path(root).rglob("*.py")):
            if SKIP_DIRS & set(p.parts) or SKIP_FILE.match(p.name):
                continue
            yield p


def main():
    ap = argparse.ArgumentParser(description="Convert print() to logging")
    ap.add_argument("paths", nargs="*", default=DEFAULT_DIRS)
    ap.add_argument("--write", action="store_true", help="rewrite the files (default is a preview)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    total = 0
    for p in files(args.paths):
        src = p.read_bytes().decode("utf-8")
        try:
            out, n, _ = convert(src)
            if n:
                compile(out, str(p), "exec")             # never write a file that does not compile
        except SyntaxError as exc:
            print(f"SKIPPED {p}: {exc}")
            continue
        if not n:
            continue
        total += n
        print(f"{p}: {n} print(s)")
        if args.write:
            p.write_bytes(out.encode("utf-8"))
    print(f"\n{total} print() call(s) {'converted' if args.write else 'would be converted'}."
          + ("" if args.write else "  Run again with --write to apply."))


if __name__ == "__main__":
    main()
