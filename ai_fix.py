"""
ai_fix.py  -  fixes the AI admin at RUNTIME. It does NOT edit ai_admin_cog.py, so there are no patch scripts that can
silently fail any more (your screenshots show they did not apply).

What it fixes
  1. DOUBLE BUMP MESSAGES  - the old bump alert inside ai_admin_cog (pings the roles at once) ran together with the new one
     in auto_cog, and its message made the new one think "already answered". The old one is switched off, the new one
     (thank-you + count + ONE reminder that turns into the thank-you) is the only one left.
  2. "u join" / "join voice 3" / "join vc <id>" MOVED YOU or asked "which member?"  -> the BOT joins now (no AI call,
     works even when the free limit is used up). "move Ali to voice 3" still moves Ali.
  3. voice tools for the AI: follow me, unfollow, leave, say out loud, music (play/skip/pause/resume/stop/volume/loop/queue)
  4. automation tools (loops, rules, welcome, bump alert v2) are registered even if the old patch did not apply
  5. "I'm not able to ..." counts as a refusal (the AI is pushed to really try the tool)
  6. the AI may only create / remove an automation when your CURRENT message asks for it

!aicheck (administrator) prints a short health report: what is loaded, ffmpeg/opus, bump settings.

LOAD IT LAST (after ai_admin_cog, auto_cog, voice_cog):   await bot.load_extension("ai_fix")
You can delete the "[patch]" block in your main file and patch_ai_admin*.py - this file replaces them.
"""

import importlib.util
import logging
import random
import re
import shutil

import discord
from discord.ext import commands

log = logging.getLogger("ai_fix")

VOICE_MISSING = "voice_cog isn't loaded - add  await bot.load_extension('voice_cog')  in your main file."

EXTRA_PROMPT = (
    " NEVER create, change or remove an automation (auto_add / auto_remove / auto_toggle / set_bump_alert) unless the "
    "staff member's CURRENT message clearly asks for it - earlier messages in the history are finished, never repeat "
    "them. A question or a request to look something up is never an automation. "
    "VOICE: you have your own voice (bot_join_voice, bot_follow, bot_unfollow, bot_leave_voice, bot_speak, bot_music). "
    "'join me / come / join my vc' = the BOT joins the staff member's voice channel (bot_join_voice) - never move the "
    "staff member for that and never ask which channel; 'join voice 3' / 'u join Lounge' = bot_join_voice with "
    "channel=that name (the BOT goes there); 'follow me' = bot_follow; 'say X out loud / speak X' = bot_speak with the "
    "exact text; play / skip / pause / resume / stop / volume / loop / queue = bot_music. voice_action move is ONLY for "
    "moving ANOTHER member, e.g. 'move Ali to voice 3' - never use it when the staff member tells YOU (the bot) to join "
    "or come, and never treat 'u' / 'you' as a member name. Never say you can't join voice without trying the tool. "
)

# ------------------------------------------------------------------ instant (no-AI) patterns
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
_VQ_JOINCH = re.compile(rf"^\s*{_V_PRE}(?:come|join)(?:\s+(?:to|in|into))?\s+(?P<c>.+?){_V_POST}", re.I)
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
)
_GREET = re.compile(
    r"\s*(?:yo+|hey+|hi+|hello+|hiya|sup|wassup|what'?s up|salam|salut|hola|bonjour|slm|ahla|aslema|3aslema)\s*[!.?]*\s*", re.I
)

VOICE_TOOL_METHODS = (
    "t_bot_join_voice", "t_bot_follow", "t_bot_unfollow", "t_bot_leave_voice", "t_bot_voice_status", "t_bot_speak",
    "t_bot_music",
)
AUTO_TOOL_METHODS = ("t_auto_add", "t_auto_list", "t_auto_remove", "t_auto_toggle")


def _voice_tools(m):
    _tool, _s, _i, _b, _MEM = m._tool, m._s, m._i, m._b, m._MEM
    return [
        _tool("bot_join_voice",
              "Make the BOT join a voice channel (default: the voice channel the staff member is in right now). Use it for "
              "'join me / come / join my vc / join voice 3'. This does NOT move the staff member.",
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
              "Text-to-speech: the bot says `text` OUT LOUD in its voice channel (it joins the staff member's channel if "
              "needed). Use the text exactly as the staff member wrote it.",
              {"text": _s(), "lang": _s(enum=["en", "ar", "fr"]), "voice": _s(enum=["female", "male"])}, ["text"]),
        _tool("bot_music",
              "Music in the voice channel. action: play (query = song name or link), skip, pause, resume, stop (clears the "
              "queue), volume (volume 1-150), loop (enabled true/false, leave out = toggle), queue, nowplaying.",
              {"action": _s(enum=["play", "skip", "pause", "resume", "stop", "volume", "loop", "queue", "nowplaying"]),
               "query": _s("song name or link"), "volume": _i("1-150"), "enabled": _b()}, ["action"]),
    ]


class AIFix(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai = None

    # ------------------------------------------------------------------ install
    async def cog_load(self):
        ai = self.bot.get_cog("AIAdmin")
        if ai is None:
            log.warning("ai_fix: the AIAdmin cog isn't loaded - load ai_admin_cog BEFORE ai_fix")
            return
        self.ai = ai
        try:
            import ai_admin_cog as m
        except ImportError:
            log.warning("ai_fix: can't import ai_admin_cog")
            return
        if getattr(ai, "_ai_fix_done", False):
            return
        ai._ai_fix_done = True
        self._tools(m)
        self._bump(ai)
        self._bind(ai)
        self._wrap(ai)
        log.info("ai_fix: installed (voice tools, automation tools, old bump alert off)")

    def _tools(self, m):
        names, new = set(), []
        try:
            from auto_cog import AUTO_CONFIRM, AUTO_READ, AUTO_TOOLS

            new += list(AUTO_TOOLS)
            m.READ_TOOLS |= set(AUTO_READ)
            m.CONFIRM_TOOLS |= set(AUTO_CONFIRM)
        except ImportError:
            log.warning("ai_fix: auto_cog.py not found - automations are not available")
        new += _voice_tools(m)
        names = {t["function"]["name"] for t in new}
        m.TOOLS[:] = [t for t in m.TOOLS if t["function"]["name"] not in names] + new  # in place: other code holds this list
        m.TOOL_NAMES = set(m.TOOL_NAMES) | names
        m.INT_ARGS = tuple(dict.fromkeys(tuple(m.INT_ARGS) + ("every_seconds", "delete_after_seconds", "volume")))
        m.BOOL_ARGS = tuple(dict.fromkeys(tuple(m.BOOL_ARGS) + ("dm", "include_bots")))
        m.TOOL_GROUPS.append((
            re.compile(r"\bjoin\b|\bcome\b|follow|\bleave\b|speak|out ?loud|aloud|\btts\b|music|\bplay|\bsong|\bskip|pause|"
                       r"resume|volume|queue|\bsing|\bvc\b|voice|تعال|اتبع|شغل|غني|تكلم"),
            {"bot_join_voice", "bot_follow", "bot_unfollow", "bot_leave_voice", "bot_voice_status", "bot_speak", "bot_music"},
        ))
        m.TOOL_GROUPS.append((
            re.compile(r"every|loop|repeat|automat|schedul|interval|welcome|keyword|rule|random|forever|nonstop|whenever|"
                       r"when someone|\bauto"),
            {"auto_add", "auto_list", "auto_remove", "auto_toggle", "list_channels", "list_roles"},
        ))
        # "I'm not able to ..." is a refusal too -> the AI gets pushed to really try a tool
        old = m.REFUSAL_RE
        m.REFUSAL_RE = re.compile(f"(?:{old.pattern})|(?:^\\s*(?:i['’]?m not able|i am not able))", old.flags)

    def _bump(self, ai):
        """switch the OLD bump alert (inside ai_admin_cog) off - auto_cog has the new one"""
        if isinstance(getattr(ai, "bump", None), dict):
            ai.bump["on"] = False
        for name, event in (("bump_watch", "on_message"), ("bump_watch_edit", "on_message_edit")):
            fn = getattr(ai, name, None)
            if fn is not None:
                try:
                    self.bot.remove_listener(fn, event)
                except Exception:
                    pass
        for t in list(getattr(ai, "bump_tasks", {}).values()):
            try:
                t.cancel()
            except Exception:
                pass
        auto = self.bot.get_cog("Automation")
        if auto is not None:
            ai.t_set_bump_alert = auto.t_set_bump_alert  # the AI tool now talks to the new bump alert

    def _bind(self, ai):
        auto = self.bot.get_cog("Automation")
        if auto is not None:
            for n in AUTO_TOOL_METHODS:
                if hasattr(auto, n):
                    setattr(ai, n, getattr(auto, n))
        for n in VOICE_TOOL_METHODS:
            setattr(ai, n, getattr(self, n))

    def _wrap(self, ai):
        orig_agent, orig_system, fix = ai._agent, ai._system, self

        async def _agent(ctx, text):
            try:
                if ai._authorized(ctx["author"]):
                    quick = fix._quick_greet(text) or await fix._quick_voice(ctx, text)
                    if quick:
                        ai._remember(ctx, text, quick)
                        return quick
            except Exception:
                log.exception("ai_fix quick path failed")
            return await orig_agent(ctx, text)

        ai._agent = _agent
        ai._system = lambda ctx: orig_system(ctx) + EXTRA_PROMPT

    # ------------------------------------------------------------------ voice tools (the AI calls these)
    def _voice(self):
        return self.bot.get_cog("VoiceDJ")

    @staticmethod
    def _is_me(q):
        """empty / 'me' / 'here' ... = the staff member himself"""
        return q is None or str(q).strip().lower() in (
            "", "me", "i", "myself", "my", "mine", "here", "us", "this", "my vc", "my channel", "my voice channel", "my voice",
        )

    def _voice_chan_for(self, ctx, q):
        """the voice channel named in q (None if q is not a voice channel)"""
        q = str(q).strip().strip('"')
        if self._is_me(q):
            return None
        ch = self.ai._find_channel(ctx["guild"], q)
        return ch if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) else None

    async def t_bot_join_voice(self, ctx, channel=None, member=None):
        v = self._voice()
        if v is None:
            return {"error": VOICE_MISSING}
        g, ch, mem = ctx["guild"], None, None
        if not self._is_me(channel):
            ch = self._voice_chan_for(ctx, channel)
            if ch is None:
                return {"error": f"Voice channel '{channel}' not found."}
        if not self._is_me(member):
            mem, err = await self.ai._one_member(g, member)
            if err:
                return err
        return await v.api_join(ctx, channel=ch, member=mem)

    async def t_bot_follow(self, ctx, member=None):
        v = self._voice()
        if v is None:
            return {"error": VOICE_MISSING}
        mem = None
        if not self._is_me(member):
            mem, err = await self.ai._one_member(ctx["guild"], member)
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

    # ------------------------------------------------------------------ instant shortcuts (no AI call)
    @staticmethod
    def _quick_greet(text):
        if _GREET.fullmatch(text):
            return random.choice(["Yo! \U0001F44B What do you need?", "Hey! What can I do for you?", "Yo, I'm here - what's up?"])
        return None

    async def _quick_voice(self, ctx, text):
        """follow me / join me / join voice 3 / leave / skip / play <song> / volume 60 / say hi out loud"""
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
        elif (m := _VQ_JOINCH.match(t)) and (vch := self._voice_chan_for(ctx, m.group("c"))) is not None:
            call = ("bot_join_voice", {"channel": str(vch.id)})
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
        return self.ai._quick_result(await self.ai._dispatch(call[0], call[1], ctx))

    # ------------------------------------------------------------------ health report
    @commands.command(name="aicheck")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def aicheck(self, ctx: commands.Context):
        """what is loaded / what is missing (administrator)"""
        import ai_admin_cog as m

        ai, auto, voice = self.bot.get_cog("AIAdmin"), self.bot.get_cog("Automation"), self.bot.get_cog("VoiceDJ")
        ok = lambda b: "OK" if b else "MISSING"  # noqa: E731
        lines = [
            f"ai_admin_cog: {ok(ai)} | auto_cog: {ok(auto)} | voice_cog: {ok(voice)}",
            f"voice tools for the AI: {ok('bot_follow' in m.TOOL_NAMES)} | automation tools: {ok('auto_add' in m.TOOL_NAMES)}",
            f"old bump alert off: {ok(ai is not None and not (isinstance(getattr(ai, 'bump', None), dict) and ai.bump.get('on')))}",
            f"ffmpeg: {ok(shutil.which('ffmpeg'))} | opus: {ok(discord.opus.is_loaded())}",
            f"edge-tts: {ok(importlib.util.find_spec('edge_tts'))} | yt-dlp: {ok(importlib.util.find_spec('yt_dlp'))}"
            f" | PyNaCl: {ok(importlib.util.find_spec('nacl'))}",
        ]
        if auto is not None:
            b = auto.data["bump"]
            lines.append(
                f"bump alert (auto_cog): {'on' if b['on'] else 'off'}, roles {len(b['roles'])}, channels {len(b['channels'])}, "
                f"remind {b['remind']} min, rules {len(auto.data['rules'])}"
            )
        await ctx.reply("```\n" + "\n".join(lines) + "\n```", mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIFix(bot))
