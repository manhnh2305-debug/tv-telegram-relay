"""
Chạy script này trong thư mục chứa file relay (main.py hoặc relay_server.py).
Nó tự tìm và fix lỗi indentation tf_tag.

Cách dùng: python fix_relay_indent.py main.py
"""
import sys

if len(sys.argv) < 2:
    print("Usage: python fix_relay_indent.py <filename>")
    print("Example: python fix_relay_indent.py main.py")
    sys.exit(1)

filename = sys.argv[1]

with open(filename, 'r', encoding='utf-8') as f:
    content = f.read()

# Pattern 1: inline format with 2-space indent (current broken state)
old1 = '  if tf in ("M5", "M15", "M30"):          tf_tag = "\u26a1 SCALP"\n  elif tf in ("H1", "H4", "SWING"):       tf_tag = "\U0001f4ca SWING"\n  else:                                    tf_tag = "\U0001f4c8 POSITION"'

# Pattern 2: inline format with 4-space indent (original v4.3.0 style)
old2 = '    if tf in ("M5", "M15", "M30"):   tf_tag = "\u26a1 SCALP"\n    elif tf in ("H1", "H4"):         tf_tag = "\U0001f4ca SWING"\n    else:                             tf_tag = "\U0001f4c8 POSITION"'

# Pattern 3: inline with SWING already added but wrong indent
old3 = '    if tf in ("M5", "M15", "M30"):          tf_tag = "\u26a1 SCALP"\n    elif tf in ("H1", "H4", "SWING"):       tf_tag = "\U0001f4ca SWING"\n    else:                                    tf_tag = "\U0001f4c8 POSITION"'

# Correct replacement (multi-line, 4-space indent, includes SWING)
fixed = '    if tf in ("M5", "M15", "M30"):\n        tf_tag = "\u26a1 SCALP"\n    elif tf in ("H1", "H4", "SWING"):\n        tf_tag = "\U0001f4ca SWING"\n    else:\n        tf_tag = "\U0001f4c8 POSITION"'

replaced = False
for i, old in enumerate([old1, old2, old3], 1):
    if old in content:
        content = content.replace(old, fixed, 1)
        print(f"\u2705 Fixed pattern {i}")
        replaced = True
        break

if not replaced:
    print("\u274c Could not find any known tf_tag pattern!")
    print("Searching for tf_tag lines:")
    for i, line in enumerate(content.split('\n'), 1):
        if 'tf_tag' in line:
            print(f"  Line {i}: {repr(line)}")
    sys.exit(1)

with open(filename, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"\u2705 Saved to {filename}")
print("Push + deploy to Render")
