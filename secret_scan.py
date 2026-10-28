#!/usr/bin/env python3
"""Secret scan of the lines a push would add.  python secret_scan.py [range]   (default origin/main..HEAD)

Prints file, line number and pattern name only, never the matched text. Exit 1 if anything matches.
Known non-secrets: MYAIR_API_KEY in sources_extra.py (ResMed's public app client ID) is skipped.
"""
import re
import subprocess
import sys

PATTERNS = {
    "telegram bot token": r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b",
    "google ical feed (SHIFT_ICS_URL)": r"calendar\.google\.com/calendar/ical/",
    "SHIFT_ICS_URL value": r"SHIFT_ICS_URL\s*=\s*\S+",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "api key / token assignment": r"(?i)\b(api[_-]?key|secret|token|passw(or)?d)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{16,}",
    "google api key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "aws key id": r"\bAKIA[0-9A-Z]{16}\b",
}
SKIP = re.compile(r"MYAIR_API_KEY")


def main() -> int:
    rng = sys.argv[1] if len(sys.argv) > 1 else "origin/main..HEAD"
    diff = subprocess.run(["git", "diff", "--unified=0", rng], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout
    hits, path, lineno = [], "?", 0
    for ln in diff.splitlines():
        if ln.startswith("+++ "):
            path = ln[6:] if ln.startswith("+++ b/") else ln[4:]
        elif ln.startswith("@@"):
            m = re.search(r"\+(\d+)", ln)
            lineno = int(m.group(1)) - 1 if m else 0
        elif ln.startswith("+"):
            lineno += 1
            if SKIP.search(ln) or path == "secret_scan.py":
                continue
            for name, pat in PATTERNS.items():
                if re.search(pat, ln[1:]):
                    hits.append((path, lineno, name))
    for h in hits:
        print("HIT %s:%d  %s" % h)
    print("%d hit(s) in %s" % (len(hits), rng))
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
