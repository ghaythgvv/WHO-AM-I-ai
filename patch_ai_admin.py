"""
patch_ai_admin.py - upgrades your ai_admin_cog.py (v5) to v6 in one go.

    python patch_ai_admin.py                 (run it in the folder that has ai_admin_cog.py)
    python patch_ai_admin.py path/to/ai_admin_cog.py

What it does
  1. removes the OLD bump alert (that is what pinged twice) - the new one lives in auto_cog.py
  2. plugs the new automation tools (bump alert v2, loops, rules, welcome) into the AI
  3. greetings like "yo" are answered directly, and a plain refusal to small talk becomes a normal chat answer
It makes ai_admin_cog.py.bak first and writes NOTHING if one of the required steps doesn't match.
"""
import pathlib
import re
import shutil
import sys

path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "ai_admin_cog.py")
if not path.exists():
    sys.exit(f"Can't find {path} - run this next to ai_admin_cog.py or give the path.")
src = path.read_text(encoding="utf-8").replace("\r\n", "\n")
if "auto_cog" in src:
    sys.exit("Already patched (auto_cog is referenced in the file). Nothing to do.")

done, skipped = [], []


def replace_once(text, old, new, label, required=True):
    if text.count(old) != 1:
        msg = f"{label}: expected 1 match, found {text.count(old)}"
        if required:
            sys.exit("PATCH FAILED - " + msg + "\nNothing was changed.")
        skipped.append(msg + " (optional, skipped)")
        return text
    done.append(label)
    return text.replace(old, new, 1)


# 1) tools: merge auto_cog's tools, new int/bool argument names -------------------------------------------------
src = replace_once(
    src,
    'BOOL_ARGS = ("locked", "enabled", "ping_now", "recreate")',
    '''INT_ARGS = INT_ARGS + ("every_seconds", "delete_after_seconds")
BOOL_ARGS = ("locked", "enabled", "ping_now", "recreate", "dm", "include_bots")

# automations (bump alert v2, loops, rules, welcome) live in auto_cog.py - load it with bot.load_extension("auto_cog")
try:
    from auto_cog import AUTO_CONFIRM, AUTO_READ, AUTO_TOOL_NAMES, AUTO_TOOLS

    TOOLS[:] = [t for t in TOOLS if t["function"]["name"] not in AUTO_TOOL_NAMES] + AUTO_TOOLS
    TOOL_NAMES = TOOL_NAMES | AUTO_TOOL_NAMES
    READ_TOOLS |= AUTO_READ
    CONFIRM_TOOLS |= AUTO_CONFIRM
except ImportError:
    AUTO_TOOL_NAMES = set()
    log.warning("auto_cog.py not found - automations are disabled")''',
    "tools merge",
)

# 2) dispatch: tools that live in auto_cog ----------------------------------------------------------------------
src = replace_once(
    src,
    'fn = getattr(self, f"t_{name}", None)\n',
    'fn = getattr(self, f"t_{name}", None)\n'
    '        if fn is None and name in AUTO_TOOL_NAMES:\n'
    '            fn = getattr(self.bot.get_cog("Automation"), f"t_{name}", None)\n',
    "dispatch",
)

# 3) delete the OLD bump alert block ----------------------------------------------------------------------------
start = re.search(r"^[ \t]*# -+ bump alert[ \t]*\n", src, re.M)
end = re.search(r"^async def setup\(", src, re.M)
if not start or not end or end.start() < start.end():
    sys.exit("PATCH FAILED - old bump block (from '# ---- bump alert' to 'async def setup') not found.\nNothing was changed.")
src = src[: start.start()] + "\n\n" + src[end.start():]
done.append("old bump alert removed")

# 4) wiped channel keeps the automations working (optional) ------------------------------------------------------
src = replace_once(
    src,
    '''        if old_id in self.bump["channels"]:  # keep the bump alert working in a wiped bump channel
            self.bump["channels"].discard(old_id)
            self.bump["channels"].add(new.id)
            notes.append(f"BUMP_CHANNEL_IDS: replace `{old_id}` with `{new.id}`")
''',
    '''        auto = self.bot.get_cog("Automation")  # bump alert + automations follow the new channel (saved by themselves)
        if auto is not None:
            auto.channel_replaced(old_id, new.id)
''',
    "wipe_channel follows automations",
    required=False,
)

# 5) tool groups: which tools to send for which words -----------------------------------------------------------
src = replace_once(
    src,
    '     {"set_bump_alert", "list_roles", "list_channels"}),\n',
    '     {"set_bump_alert", "list_roles", "list_channels"}),\n'
    '    (re.compile(r"every|loop|repeat|automat|schedul|constant|interval|welcome|keyword|rule|random|forever|nonstop|\\bauto"),\n'
    '     {"auto_add", "auto_list", "auto_remove", "auto_toggle", "list_channels", "list_roles"}),\n',
    "tool group",
)

# 6) system prompt: what the AI may promise ----------------------------------------------------------------------
m = re.search(r'"AUTOMATIONS:.*?isn\'t supported\. "\n', src, re.S)
if not m:
    sys.exit("PATCH FAILED - the AUTOMATIONS part of the system prompt wasn't found.\nNothing was changed.")
src = src[: m.start()] + '''"AUTOMATIONS: you can't watch events yourself, so NEVER say 'I'll set it up' unless a tool did it. Tools exist for: "
            "the bump alert (set_bump_alert: 'tag Staff every time someone bumps'), repeating jobs like 'send a random message "
            "in #x every 30 seconds and delete it' (auto_add kind=loop, delete_after_seconds=0 deletes it at once), "
            "'when someone says X in #y reply / react / delete' (auto_add kind=on_message), welcome message / auto role when "
            "someone joins (auto_add kind=on_join), and auto_list / auto_remove / auto_toggle to manage them. Pass channel and "
            "role names exactly as the staff member wrote them. If they want 'random messages' and give no texts, write 8-10 "
            "short varied texts yourself in `texts`. If the channel or the interval is missing, ask ONE short question. "
            "For anything else automatic, say plainly that it isn't supported. "
''' + src[m.end():]
done.append("system prompt")

# 7) greetings + plain-chat fallback ------------------------------------------------------------------------------
src = replace_once(
    src,
    "    def _quick_help(self, text):\n",
    '''    def _quick_greet(self, text):
        """'yo' / 'hey' / 'salam' -> a friendly answer without calling the AI (the model sometimes refused these)"""
        if re.fullmatch(
            r"\\s*(?:yo+|hey+|hi+|hello+|hiya|sup|wassup|what'?s up|salam|salut|hola|bonjour|slm|ahla|aslema|3aslema)\\s*[!.?]*\\s*",
            text, re.I,
        ):
            import random

            return random.choice(["Yo! \\U0001F44B What do you need?", "Hey! What can I do for you?", "Yo, I'm here - what's up?"])
        return None

    async def _chat_fallback(self, ctx, text):
        """the model answered a normal chat message with a bare refusal -> answer it as plain chat (no tools)"""
        model = FALLBACK_MODEL or MODEL
        try:
            r = await self.ai.chat.completions.create(
                model=model, max_tokens=500, temperature=0.6, **self._extra(model),
                messages=[{"role": "system", "content": (
                    f"You are ELITE SYSTEM, the friendly assistant bot of the Discord server '{ctx['guild'].name}'. "
                    f"You are chatting with {ctx['author'].display_name}, a trusted staff member. Reply in English unless they asked "
                    "for another language. Short (1-3 sentences), Discord markdown. Greetings, small talk, jokes and questions are "
                    "always fine. Never reply with only 'I can't' - help, or say what you can do (look things up, moderate, set up "
                    "automations)."
                )}] + list(self.history[ctx["channel"].id])[-4:] + [{"role": "user", "content": text}],
            )
            return (r.choices[0].message.content or "").strip() or "Yo! What do you need?"
        except Exception:
            return "Yo! What do you need?"

    def _quick_help(self, text):
''',
    "greeting + fallback methods",
)
src = replace_once(
    src,
    "self._quick_help(text)\n",
    "self._quick_help(text)\n            or self._quick_greet(text)\n",
    "greeting in quick chain",
)
src = replace_once(
    src,
    "        if _mostly_arabic(final) and not self._wants_other_lang(ctx, text):\n",
    "        if not used_tools and REFUSAL_RE.match(final) and len(final) < 200:\n"
    "            final = await self._chat_fallback(ctx, clean)  # a bare refusal to normal chat -> answer it as chat\n"
    "        if _mostly_arabic(final) and not self._wants_other_lang(ctx, text):\n",
    "refusal fallback",
)

# 8) help text (optional) ------------------------------------------------------------------------------------------
src = replace_once(
    src,
    '"**Automatic:** `tag Staff and UT Staff in #bump every time someone bumps` - `bump alert status` - `bump alert off`\\n"\n',
    '"**Automatic:** `tag Staff and UT Staff in #bump every time someone bumps` - `bump alert status` - `bump alert off` - "\n'
    '    "`send a random message in #chat every 30 seconds and delete it` - `when someone says X in #chat delete it` - "\n'
    '    "`welcome new members in #welcome` - `list automations` - `remove r3`\\n"\n',
    "help text",
    required=False,
)

try:
    compile(src, str(path), "exec")
except SyntaxError as e:
    sys.exit(f"PATCH FAILED - the result would not compile ({e}).\nNothing was changed.")

shutil.copyfile(path, str(path) + ".bak")
path.write_text(src, encoding="utf-8")
print("Patched OK. Backup:", str(path) + ".bak")
for d in done:
    print("  done   -", d)
for s in skipped:
    print("  note   -", s)
print("\nNext: put auto_cog.py next to it, add  await bot.load_extension(\"auto_cog\")  to your main file, redeploy.")
