"""
patch_ai_admin_2.py - run AFTER patch_ai_admin.py.  Fixes "he only does it when I ask a second time":
  1. the automation tools are always offered to the AI (before, a message without the right keywords never saw them)
  2. an answer like "sure / I'll do it / done" where NO tool was called gets the same push-retry as a refusal

    python patch_ai_admin_2.py                 (next to ai_admin_cog.py)
Makes ai_admin_cog.py.bak2 first; writes nothing if a step doesn't match.
"""
import pathlib
import shutil
import sys

path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "ai_admin_cog.py")
if not path.exists():
    sys.exit(f"Can't find {path}.")
src = path.read_text(encoding="utf-8").replace("\r\n", "\n")
if "LAZY_RE" in src:
    sys.exit("Already patched. Nothing to do.")
if "auto_cog" not in src:
    sys.exit("Run patch_ai_admin.py first.")


def once(text, old, new, label):
    if text.count(old) != 1:
        sys.exit(f"PATCH FAILED - {label}: expected 1 match, found {text.count(old)}. Nothing was changed.")
    return text.replace(old, new, 1)


src = once(src, "    CONFIRM_TOOLS |= AUTO_CONFIRM\n",
           "    CONFIRM_TOOLS |= AUTO_CONFIRM\n    BASE_TOOLS |= AUTO_TOOL_NAMES  # always offered, so the AI never says 'not supported' before seeing them\n",
           "base tools")
src = once(src, "NUDGE = (\n",
           'LAZY_RE = re.compile(r"^\\s*(?:sure|ok(?:ay)?|alright|got it|done|yes|i[\'’]?ll|i will|i[\'’]?ve|created|setting)\\b", re.I)\n'
           "NUDGE = (\n", "lazy regex")
src = once(src, "(not content or REFUSAL_RE.match(content))",
           "(not content or REFUSAL_RE.match(content) or LAZY_RE.match(content))", "nudge condition")
try:
    compile(src, str(path), "exec")
except SyntaxError as e:
    sys.exit(f"PATCH FAILED - result would not compile ({e}). Nothing was changed.")
shutil.copyfile(path, str(path) + ".bak2")
path.write_text(src, encoding="utf-8")
print("Patched OK. Backup:", str(path) + ".bak2", "- redeploy.")
