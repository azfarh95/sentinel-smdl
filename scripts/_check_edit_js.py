"""Extract the <script> body from _EDIT_HTML and write it to a temp .js so
`node --check` can validate it. Substitutes {{DRAFT_ID}} with a real number.
This catches the unescaped-quote / unterminated-string class of bugs that
HTTP 200 + curl can never surface."""
import re
import sys
from pathlib import Path

src = Path(r"C:\Users\azfar\sentinel-smdl\app\sticker_routes.py").read_text(encoding="utf-8")

# Grab the _EDIT_HTML raw string literal.
m = re.search(r'_EDIT_HTML = r"""(.*?)"""', src, re.S)
if not m:
    print("could not locate _EDIT_HTML"); sys.exit(2)
html = m.group(1).replace("{{DRAFT_ID}}", "123")

# Pull every <script>...</script> with inline body (skip src-only tags).
bodies = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
if not bodies:
    print("no inline <script> body found"); sys.exit(2)
out = Path(r"C:\Users\azfar\sentinel-smdl\scripts\_edit_inline.js")
out.write_text("\n//---script-split---\n".join(bodies), encoding="utf-8")
print(f"wrote {out} ({sum(len(b) for b in bodies)} chars across {len(bodies)} script block(s))")
