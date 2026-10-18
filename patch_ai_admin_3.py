"""
patch_ai_admin_3.py - run AFTER patch_ai_admin.py and patch_ai_admin_2.py.

Fixes what the screenshots show:
  1. "join my vc" / "u join me" did nothing: voice_cog.py was loaded but ai_admin_cog had NO tools to talk to it.
     -> adds the voice tools (join, follow, unfollow, leave, speak, music) AND instant no-AI shortcuts for
        "follow me", "join me", "come", "leave", "skip", "pause", "play <song>", "volume 60", "say hi out loud"
        (they work even when the free AI limit is used up, and cost no tokens)
  2. "I'm not able to join a voice channel" was not treated as a refusal -> now it gets the same push-retry
  3. the AI created a second automation (r2) for a question -> new rule: never create automations unless the
     CURRENT message asks for it

    python patch_ai_admin_3.py                 (next to ai_admin_cog.py)
Makes ai_admin_cog.py.bak3 first; writes NOTHING if a step doesn't match.
"""
import pathlib
import shutil
import sys

path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "ai_admin_cog.py")
if not path.exists():
    sys.exit(f"Can't find {path}.")
src = path.read_text(encoding="utf-8").replace("\r\n", "\n")
if "bot_follow" in src:
    sys.exit("Already patched. Nothing to do.")
if "LAZY_RE" not in src or "_quick_greet" not in src:
    sys.exit("Run patch_ai_admin.py and patch_ai_admin_2.py first.")


def once(text, old, new, label):
    if text.count(old) != 1:
        sys.exit(f"PATCH FAILED - {label}: expected 1 match, found {text.count(old)}. Nothing was changed.")
    return text.replace(old, new, 1)


# 1) "I'm not able ..." counts as a refusal ------------------------------------------------------------------
src = once(
    src,
    "i['’]?m unable|i am unable|",
    "i['’]?m unable|i['’]?m not able|i am not able|i am unable|",
    "refusal regex",
)

# 2) module level: voice tools + quick patterns --------------------------------------------------------------
BLOCK = r'''# ---- voice (voice_cog.py): the bot's own voice - follow me / join / speak / music
VOICE_MISSING = "voice_cog isn't loaded - add  await bot.load_extension('voice_cog')  in your main file."
VOICE_TOOLS = [
    _tool("bot_join_voice",
          "Make the BOT join a voice channel (default: the voice channel the staff member is in right now). Use it for "
          "'join me / come / join my vc'. This does NOT move the staff member.",
          {"channel": _s("voice channel name/id; leave out = the staff member's own channel"),
           "member": _s(_MEM + " - join THIS member's channel instead")}),
    _tool("bot_follow",
          "The bot joins a member and then follows them from voice channel to voice channel. 'follow me'. "
          "Default member = the staff member.",
          {"member": _s(_MEM)}),
    _tool("bot_unfollow", "Stop following."),
    _tool("bot_leave_voice", "The bot disconnects from voice and clears its music queue."),
    _tool("bot_voice_status", "Where the bot is in voice, who it follows, what it plays."),
    _tool("bot_speak",
          "Text-to-speech: the bot says `text` OUT LOUD in its voice channel (it joins the staff member's channel if needed). "
          "Use the text exactly as the staff member wrote it.",
          {"text": _s(), "lang": _s(enum=["en", "ar", "fr"]), "voice": _s(enum=["female", "male"])}, ["text"]),
    _tool("bot_music",
          "Music in the voice channel. action: play (query = song name or link), skip, pause, resume, stop (clears the "
          "queue), volume (volume 1-150), loop (enabled true/false, leave out = toggle), queue, nowplaying.",
          {"action": _s(enum=["play", "skip", "pause", "resume", "stop", "volume", "loop", "queue", "nowplaying"]),
           "query": _s("song name or link"), "volume": _i("1-150"), "enabled": _b()}, ["action"]),
]
VOICE_TOOL_NAMES = {t["function"]["name"] for t in VOICE_TOOLS}
TOOLS[:] = [t for t in TOOLS if t["function"]["name"] not in VOICE_TOOL_NAMES] + VOICE_TOOLS
TOOL_NAMES = TOOL_NAMES | VOICE_TOOL_NAMES
INT_ARGS = INT_ARGS + ("volume",)

_V_PRE = r"(?:(?:please|pls|plz|u|you|bot|hey|yo)\s+)*"
_V_POST = r"(?:\s+(?:please|pls|plz|now))?\s*[!.?]*\s*$"
_VQ_FOLLOW = re.compile(rf"^\s*{_V_PRE}follow\s+me{_V_POST}", re.I)
_VQ_UNFOLLOW = re.compile(
    rf"^\s*{_V_PRE}(?:unfollow(?:\s+me)?|stop\s+follow(?:ing)?(?:\s+me)?|don['’]?t\s+follow(?:\s+me)?){_V_POST}", re.I
)
_VQ_JOIN = re.compile(
    rf"^\s*{_V_PRE}(?:come|join)(?:\s+(?:to|in|into))?"
    r"(?:\s+(?:me|us|here|(?:my|the)\s*(?:vc|voice(?:\s*chat|\s*channel)?|channel|room)|vc|voice(?:\s*chat|\s*channel)?))?"
    rf"{_V_POST}",
    re.I,
)
_VQ_LEAVE = re.compile(
    rf"^\s*{_V_PRE}(?:leave|disconnect|get\s+out|go\s+out)"
    r"(?:\s+(?:the\s+|my\s+|from\s+(?:the\s+)?)?(?:vc|voice(?:\s*chat|\s*channel)?|channel))?"
    rf"{_V_POST}",
    re.I,
)
_VQ_CTRL = re.compile(
    r"^\s*(?P<c>skip|next|pause|resume|unpause|stop\s+(?:the\s+)?music|now\s+playing|what['’]?s\s+playing|queue|loop)\s*[!.?]*\s*$",
    re.I,
)
_VQ_VOL = re.compile(r"^\s*(?:set\s+)?volume\s+(?:to\s+)?(?P<v>\d{1,3})\s*%?\s*$", re.I)
_VQ_PLAY = re.compile(r"^\s*(?:play|put\s+music)\s+(?P<q>.+?)\s*$", re.I)
_VQ_SPEAK = re.compile(
    r"^\s*(?:speak|say)\s+(?P<t>.+?)\s+(?:out\s*loud|aloud|in\s+(?:the\s+|my\s+)?(?:vc|voice(?:\s*chat|\s*channel)?))\s*$", re.I
)
_VQ_SPEAK2 = re.compile(
    r"^\s*speak\s+(?!(?:in\s+)?(?:english|arabic|french|tunis\w*|darija|derja|tounsi)\b)(?P<t>.+)$", re.I
)'''
src = once(
    src,
    '    log.warning("auto_cog.py not found - automations are disabled")',
    '    log.warning("auto_cog.py not found - automations are disabled")\n\n' + BLOCK,
    "voice tools block",
)

# 3) tool group: which words make the AI see the voice tools --------------------------------------------------
src = once(
    src,
    '    (re.compile(r"bump|disboard|remind|every ?time|automat|alert"),\n',
    r'''    (re.compile(r"\bjoin\b|\bcome\b|follow|\bleave\b|speak|out ?loud|aloud|\btts\b|music|\bplay|\bsong|\bskip|pause|resume|volume|queue|\bsing|\bvc\b|voice|تعال|اتبع|شغل|غني|تكلم"),
     {"bot_join_voice", "bot_follow", "bot_unfollow", "bot_leave_voice", "bot_voice_status", "bot_speak", "bot_music"}),
''' + '    (re.compile(r"bump|disboard|remind|every ?time|automat|alert"),\n',
    "tool group",
)

# 4) system prompt ---------------------------------------------------------------------------------------------
PROMPT_OLD = r'''For anything else automatic, say plainly that it isn't supported. "'''
PROMPT_NEW = (
    PROMPT_OLD
    + "\n"
    + r'''            "NEVER create, change or remove an automation (auto_add / auto_remove / auto_toggle / set_bump_alert) unless the staff member's CURRENT message clearly asks for it - earlier messages in the history are finished, never repeat them. A question or a request to look something up is never an automation. "'''
    + "\n"
    + r'''            "VOICE: you have your own voice (bot_join_voice, bot_follow, bot_unfollow, bot_leave_voice, bot_speak, bot_music). 'join me / come / join my vc' = the BOT joins the staff member's voice channel (bot_join_voice) - never move the staff member for that and never ask which channel; 'follow me' = bot_follow; 'say X out loud / speak X' = bot_speak with the exact text; play / skip / pause / resume / stop / volume / loop / queue = bot_music. Never say you can't join voice without trying the tool. "'''
)
src = once(src, PROMPT_OLD, PROMPT_NEW, "system prompt")

# 5) instant shortcuts in the quick chain -----------------------------------------------------------------------
src = once(
    src,
    "            or self._quick_greet(text)\n",
    "            or self._quick_greet(text)\n            or await self._quick_voice(ctx, text)\n",
    "quick chain",
)

# 6) the methods (inside the AIAdmin class, right before setup) --------------------------------------------------
METHODS = r'''    # ------------------------------------------------------------ voice (voice_cog.py)
    def _voice(self):
        return self.bot.get_cog("VoiceDJ")

    @staticmethod
    def _is_me(q):
        """empty / 'me' / 'here' ... = the staff member himself"""
        return q is None or str(q).strip().lower() in (
            "", "me", "i", "myself", "my", "mine", "here", "us", "this", "my vc", "my channel", "my voice channel", "my voice",
        )

    async def t_bot_join_voice(self, ctx, channel=None, member=None):
        v = self._voice()
        if v is None:
            return {"error": VOICE_MISSING}
        g, ch, mem = ctx["guild"], None, None
        if not self._is_me(channel):
            ch = self._find_channel(g, channel)
            if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                return {"error": f"Voice channel '{channel}' not found."}
        if not self._is_me(member):
            mem, err = await self._one_member(g, member)
            if err:
                return err
        return await v.api_join(ctx, channel=ch, member=mem)

    async def t_bot_follow(self, ctx, member=None):
        v = self._voice()
        if v is None:
            return {"error": VOICE_MISSING}
        mem = None
        if not self._is_me(member):
            mem, err = await self._one_member(ctx["guild"], member)
            if err:
                return err
        return await v.api_follow(ctx, member=mem)

    async def t_bot_unfollow(self, ctx):
        v = self._voice()
        return await v.api_unfollow(ctx) if v is not None else {"error": VOICE_MISSING}

    async def t_bot_leave_voice(self, ctx):
        v = self._voice()
        return await v.api_leave(ctx) if v is not None else {"error": VOICE_MISSING}

    async def t_bot_voice_status(self, ctx):
        v = self._voice()
        return await v.api_status(ctx) if v is not None else {"error": VOICE_MISSING}

    async def t_bot_speak(self, ctx, text, lang=None, voice=None):
        v = self._voice()
        if v is None:
            return {"error": VOICE_MISSING}
        return await v.api_say(ctx, text, lang=lang, voice=voice)

    async def t_bot_music(self, ctx, action, query=None, volume=None, enabled=None):
        v = self._voice()
        if v is None:
            return {"error": VOICE_MISSING}
        a = re.sub(r"[\s_-]+", "", str(action).lower())
        a = {"next": "skip", "np": "nowplaying", "current": "nowplaying", "unpause": "resume", "start": "play",
             "add": "play", "list": "queue", "vol": "volume", "repeat": "loop"}.get(a, a)
        return await v.api_music(ctx, a, query=query, volume=volume, enabled=enabled)

    async def _quick_voice(self, ctx, text):
        """follow me / join me / leave / skip / play <song> / volume 60 / say hi out loud - instant, no AI call"""
        t = text.strip()
        call = None
        if _VQ_UNFOLLOW.match(t):
            call = ("bot_unfollow", {})
        elif _VQ_FOLLOW.match(t):
            call = ("bot_follow", {})
        elif _VQ_LEAVE.match(t):
            call = ("bot_leave_voice", {})
        elif _VQ_JOIN.match(t):
            call = ("bot_join_voice", {})
        else:
            m = _VQ_CTRL.match(t)
            if m:
                c = re.sub(r"\s+", " ", m.group("c").lower().replace("’", "'"))
                if c.startswith("stop"):
                    a = "stop"
                elif c in ("now playing", "what's playing"):
                    a = "nowplaying"
                else:
                    a = {"next": "skip", "unpause": "resume"}.get(c, c)
                call = ("bot_music", {"action": a})
            elif _VQ_VOL.match(t):
                call = ("bot_music", {"action": "volume", "volume": int(_VQ_VOL.match(t).group("v"))})
            elif _VQ_PLAY.match(t):
                call = ("bot_music", {"action": "play", "query": _VQ_PLAY.match(t).group("q")})
            else:
                m = _VQ_SPEAK.match(t) or _VQ_SPEAK2.match(t)
                if m:
                    call = ("bot_speak", {"text": m.group("t").strip().strip('"')})
        if call is None:
            return None
        return self._quick_result(await self._dispatch(call[0], call[1], ctx))

'''
src = once(
    src,
    "async def setup(bot: commands.Bot):\n    await bot.add_cog(AIAdmin(bot))",
    METHODS + "\nasync def setup(bot: commands.Bot):\n    await bot.add_cog(AIAdmin(bot))",
    "voice methods",
)

try:
    compile(src, str(path), "exec")
except SyntaxError as e:
    sys.exit(f"PATCH FAILED - the result would not compile ({e}). Nothing was changed.")

shutil.copyfile(path, str(path) + ".bak3")
path.write_text(src, encoding="utf-8")
print("Patched OK. Backup:", str(path) + ".bak3", "- redeploy.")
