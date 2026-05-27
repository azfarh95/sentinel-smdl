"""Phase-3 route-gating pass for SMDL miniapp.py.

Line-by-line state machine (a regex was used originally but the 2878-
line file with embedded HTML caused catastrophic backtracking).

Usage:  python _phase3_gate_routes.py <input.py> <output.py>
"""
import re
import sys


DECORATOR_RE = re.compile(r'^@router\.(?:get|post|delete|patch|put)\("([^"]+)"')
ASYNC_DEF_RE = re.compile(r'^async def \w+\(')
VERIFY_LINE = "p = await _verify(request)"


def scope_for(path):
    if path.startswith("/api/miniapp/admin/"): return "smdl.admin"
    if path == "/api/miniapp/restart": return "smdl.admin"
    if path.startswith("/api/miniapp/onedrive/"): return "smdl.downloader"
    if path == "/api/miniapp/downloads": return "smdl.downloader"
    if path == "/api/miniapp/test_url": return "smdl.downloader"
    if path == "/api/miniapp/active": return "smdl.streamtracker"
    if path.startswith("/api/miniapp/watchlist"): return "smdl.streamtracker"
    if path.startswith("/api/miniapp/stream/"): return "smdl.streamtracker"
    return None


def main():
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        lines = f.read().splitlines(keepends=True)

    out = []
    i = 0
    inserted = []
    while i < len(lines):
        out.append(lines[i])
        m = DECORATOR_RE.match(lines[i].strip())
        if m:
            path = m.group(1)
            scope = scope_for(path)
            if scope is None:
                i += 1
                continue
            # Peek next line: should be `async def <name>(...)` (possibly
            # followed by multi-line signature). Allow a small lookahead.
            j = i + 1
            # Skip any continuation of the decorator line if multi-line
            while j < len(lines) and not ASYNC_DEF_RE.match(lines[j].strip()):
                out.append(lines[j])
                j += 1
                if j - i > 6:
                    break  # not a normal route — bail
            if j >= len(lines) or not ASYNC_DEF_RE.match(lines[j].strip()):
                i = j
                continue
            # Copy the async-def line and following body lines until we hit
            # `p = await _verify(request)` (or the next @router decorator).
            out.append(lines[j])
            k = j + 1
            already_gated = False
            inserted_here = False
            while k < len(lines):
                # Stop at next decorator or top-level def — different route.
                stripped = lines[k].strip()
                if stripped.startswith("@router.") or (
                    stripped.startswith("def ") and not lines[k].startswith(" ")
                ):
                    break
                if (
                    not inserted_here
                    and not already_gated
                    and f'require_scope(p, "{scope}")' in stripped
                ):
                    already_gated = True
                out.append(lines[k])
                if (
                    not inserted_here
                    and not already_gated
                    and VERIFY_LINE in stripped
                ):
                    # Compute indent of this line.
                    line = lines[k]
                    indent = line[: len(line) - len(line.lstrip())]
                    out.append(f'{indent}require_scope(p, "{scope}")\n')
                    inserted.append((path, scope))
                    inserted_here = True
                k += 1
            i = k
            continue
        i += 1

    with open(sys.argv[2], "w", encoding="utf-8") as f:
        f.writelines(out)

    print(f"INSERTED {len(inserted)} require_scope() calls")
    by_scope = {}
    for path, scope in inserted:
        by_scope.setdefault(scope, []).append(path)
    for scope in sorted(by_scope):
        print(f"  {scope}: {len(by_scope[scope])} route(s)")
        for p in by_scope[scope]:
            print(f"    - {p}")


if __name__ == "__main__":
    main()
