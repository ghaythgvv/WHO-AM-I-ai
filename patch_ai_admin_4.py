"""
patch_ai_admin_4.py - run AFTER patch_ai_admin_3.py.

Fixes what the new screenshot shows:
  "JOIN VOICE 3"   -> the AI used voice_action and MOVED YOU ("Voice move done on ELT WHO AM I")
  "U JOIN VOICE 3" -> "Which member 'U' should I move?"
Now "join voice 3" / "u join Lounge" / "come to voice 3" is a no-AI shortcut: the BOT joins that voice channel.
Moving ANOTHER member still works ("move Ali to voice 3").

    python patch_ai_admin_4.py                 (next to ai_admin_cog.py)
Makes ai_admin_cog.py.bak4 first; writes nothing if a step doesn't match.
"""
import pathlib
import shutil
import sys

path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "ai_admin_cog.py")
if not path.exists():
    sys.exit(f"Can't find {path}.")
src = path.read_text(encoding="utf-8").replace("\r\n", "\n")
if "_VQ_JOINCH" in src:
    sys.exit("Already patched. Nothing to do.")
if "bot_follow" not in src:
    sys.exit("Run patch_ai_admin_3.py first.")


def once(text, old, new, label):
    if text.count(old) != 1:
        sys.exit(f"PATCH FAILED - {label}: expected 1 match, found {text.count(old)}. Nothing was changed.")
    return text.replace(old, new, 1)


# 1) regex: "join <something>" ------------------------------------------------------------------------------
PLAY_LINE = r'''_VQ_PLAY = re.compile(r"^\s*(?:play|put\s+music)\s+(?P<q>.+?)\s*$", re.I)'''
src = once(
    src,
    PLAY_LINE,
    PLAY_LINE + "\n" + r'''_VQ_JOINCH = re.compile(rf"^\s*{_V_PRE}(?:come|join)(?:\s+(?:to|in|into))?\s+(?P<c>.+?){_V_POST}", re.I)''',
    "join-channel regex",
)

# 2) quick chain: use it only when the text really names a voice channel -------------------------------------
JOIN_CALL = '''        elif _VQ_JOIN.match(t):
            call = ("bot_join_voice", {})
'''
src = once(
    src,
    JOIN_CALL,
    JOIN_CALL
    + '''        elif (m := _VQ_JOINCH.match(t)) and (vch := self._voice_chan_for(ctx, m.group("c"))) is not None:
            call = ("bot_join_voice", {"channel": str(vch.id)})
''',
    "quick chain",
)

# 3) helper -------------------------------------------------------------------------------------------------
src = once(
    src,
    "    async def _quick_voice(self, ctx, text):\n",
    '''    def _voice_chan_for(self, ctx, q):
        """the voice channel named in q (None if q is not a voice channel)"""
        q = str(q).strip().strip('"')
        if self._is_me(q):
            return None
        ch = self._find_channel(ctx["guild"], q)
        return ch if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) else None

    async def _quick_voice(self, ctx, text):
''',
    "helper",
)

# 4) system prompt: join vs move ------------------------------------------------------------------------------
P_OLD = r'''Never say you can't join voice without trying the tool. "'''
P_NEW = (
    P_OLD
    + "\n"
    + r'''            "'join voice 3' / 'u join Lounge' = bot_join_voice with channel=that name (the BOT goes there). voice_action move is ONLY for moving ANOTHER member, e.g. 'move Ali to voice 3' - never use it when the staff member tells YOU (the bot) to join or come, and never treat 'u' / 'you' as a member name. "'''
)
src = once(src, P_OLD, P_NEW, "system prompt")

try:
    compile(src, str(path), "exec")
except SyntaxError as e:
    sys.exit(f"PATCH FAILED - the result would not compile ({e}). Nothing was changed.")

shutil.copyfile(path, str(path) + ".bak4")
path.write_text(src, encoding="utf-8")
print("Patched OK. Backup:", str(path) + ".bak4", "- redeploy.")
