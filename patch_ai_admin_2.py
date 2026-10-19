"""
patch_ai_admin_2.py  -  NEW CONTENT. Overwrite your old patch_ai_admin_2.py with this file (same name).

Your main file already runs this file on every start, so this needs NO change in the main file:
it makes ai_admin_cog.py load ai_fix.py (and, through it, owner_guard.py) a moment after the bot is ready.

You only have to put these files in the same folder as ai_admin_cog.py:
    ai_fix.py   owner_guard.py   patch_ai_admin_2.py (this one, overwriting the old one)

If you ALSO load ai_fix in your main file, nothing breaks: it is only loaded once.
"""
import pathlib
import shutil
import sys

path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "ai_admin_cog.py")
if not path.exists():
    sys.exit(f"Can't find {path}.")
src = path.read_text(encoding="utf-8").replace("\r\n", "\n")
if "_load_ai_fix" in src:
    sys.exit("Already installed. Nothing to do.")

OLD = "async def setup(bot: commands.Bot):\n    await bot.add_cog(AIAdmin(bot))"
if src.count(OLD) != 1:
    sys.exit(f"PATCH FAILED - setup() not found exactly once (found {src.count(OLD)}). Nothing was changed.")

NEW = '''async def setup(bot: commands.Bot):
    await bot.add_cog(AIAdmin(bot))

    # ---- added by patch_ai_admin_2.py: load ai_fix.py (voice, automations, bump ping, owner protection ...) once the bot is ready
    async def _load_ai_fix():
        try:
            await bot.wait_until_ready()
            if "ai_fix" not in bot.extensions:
                await bot.load_extension("ai_fix")
        except Exception:
            log.exception("ai_fix.py could not be loaded - is it in the same folder as ai_admin_cog.py?")

    bot._ai_fix_task = asyncio.create_task(_load_ai_fix())'''
src = src.replace(OLD, NEW, 1)

try:
    compile(src, str(path), "exec")
except SyntaxError as e:
    sys.exit(f"PATCH FAILED - the result would not compile ({e}). Nothing was changed.")

shutil.copyfile(path, str(path) + ".bak2")
path.write_text(src, encoding="utf-8")
folder = path.resolve().parent
missing = [f for f in ("ai_fix.py", "owner_guard.py") if not (folder / f).exists()]
print("Installed OK. Backup:", str(path) + ".bak2")
if missing:
    print("WARNING - these files are not next to ai_admin_cog.py yet:", ", ".join(missing))
