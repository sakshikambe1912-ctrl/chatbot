r"""Adds rate limiting to the chat view in chatbot\views.py. Run from the project folder:

    python scripts\add_throttle.py

It makes exactly three changes (and does nothing if they are already there):
  1. imports throttle_classes
  2. imports ChatIPThrottle and ChatSessionThrottle
  3. puts @throttle_classes(...) on the chat view, and only on that view
"""
import re
import sys
from pathlib import Path

PATH = Path("chatbot") / "views.py"
DECORATOR = "@throttle_classes([ChatIPThrottle, ChatSessionThrottle])"


def patch(source):
    nl = "\r\n" if "\r\n" in source else "\n"
    text = source.replace("\r\n", "\n")

    # 1) the decorators import
    m = re.search(r"^from rest_framework\.decorators import (.+)$", text, re.M)
    if not m:
        raise SystemExit("Could not find the 'from rest_framework.decorators import ...' line.")
    if "throttle_classes" not in m.group(1):
        text = text[:m.end()] + ", throttle_classes" + text[m.end():]

    # 2) the throttle classes import (after the AllowAny import)
    if "from .throttles import" not in text:
        m = re.search(r"^from rest_framework\.permissions import AllowAny[^\n]*$", text, re.M)
        if not m:
            raise SystemExit("Could not find the 'from rest_framework.permissions import AllowAny' line.")
        text = text[:m.end()] + "\nfrom .throttles import ChatIPThrottle, ChatSessionThrottle" + text[m.end():]

    # 3) the decorator, directly above 'def chat(request):'
    if DECORATOR not in text:
        pattern = r"(@permission_classes\(\[AllowAny\]\)\n)(def chat\(request\):)"
        if not re.search(pattern, text):
            raise SystemExit("Could not find '@permission_classes([AllowAny])' directly above 'def chat(request):'.\n"
                             "Run: git checkout -- chatbot\\views.py   and then run this script again.")
        text = re.sub(pattern, r"\1" + DECORATOR + r"\n\2", text, count=1)
    return text.replace("\n", nl)


def main():
    if not PATH.exists():
        sys.exit(f"{PATH} not found. Run this from the project folder (the one with manage.py).")
    original = PATH.read_bytes().decode("utf-8")
    new = patch(original)
    compile(new, str(PATH), "exec")                       # never write a file that does not compile
    if new == original:
        print("Nothing to change: the throttle is already set up.")
        return
    PATH.write_bytes(new.encode("utf-8"))
    print("Done. chatbot\\views.py now rate-limits the chat view.")


if __name__ == "__main__":
    main()