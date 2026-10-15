import asyncio
import hmac
import os
import random
import re
import secrets
import shutil
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
from dotenv import load_dotenv
from flask import Flask

# Reads DISCORD_TOKEN, LINK_PASSWORD, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CF_PAGES_PROJECT,
# SURGE_TOKEN, SURGE_LOGIN
# from the .env next to this file. Render has no .env, so it uses its own env vars.
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

# ==========================================
# 1. EMBEDDED WEB SERVER FOR RENDER PINGING
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web_server, daemon=True).start()

# In-memory storage for server configurations (resets on restart)
join_channels = {}
leave_channels = {}
auto_roles = {}

# ==========================================
# 2. DISCORD BOT SETUP
# ==========================================
class CustomBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Re-register the persistent panel view so its button works after restarts
        self.add_view(LinkDispenserView())
        try:
            synced = await self.tree.sync()
            print(f"✅ Synced {len(synced)} global commands.")
        except Exception as e:
            print(f"Sync error: {e}")

bot = CustomBot()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    await bot.change_presence(activity=discord.Game(name="/cmds | Carl-Style"))
    if autolink_channel_id and not autolink_loop.is_running():
        autolink_loop.start()

# ==========================================
# HELPERS
# ==========================================
NO_MENTIONS = discord.AllowedMentions.none()

def can_act_on(ctx: commands.Context, member: discord.Member) -> bool:
    """Role-hierarchy check for kick/ban/mute etc."""
    guild = ctx.guild
    if member == guild.owner or member == ctx.author:
        return False
    if ctx.author != guild.owner and ctx.author.top_role <= member.top_role:
        return False
    return guild.me.top_role > member.top_role

HIERARCHY_MSG = "❌ You (or I) can't act on that member because of role hierarchy."

# ==========================================
# 3. SETUP COMMANDS (Join/Leave/AutoRole)
# ==========================================
@bot.hybrid_group(name="setup", description="Configure bot settings for your server.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def setup(ctx: commands.Context):
    if ctx.invoked_subcommand is None:
        await ctx.send("Use `/setup join`, `/setup leave`, or `/setup autorole` to configure settings.")

@setup.command(name="join", description="Sets the channel where join/welcome messages will be sent.")
@commands.has_permissions(administrator=True)
async def setup_join(ctx: commands.Context, channel: discord.TextChannel):
    join_channels[ctx.guild.id] = channel.id
    await ctx.send(f"✅ Welcome messages will now be sent to {channel.mention}")

@setup.command(name="leave", description="Sets the channel where leave/goodbye messages will be sent.")
@commands.has_permissions(administrator=True)
async def setup_leave(ctx: commands.Context, channel: discord.TextChannel):
    leave_channels[ctx.guild.id] = channel.id
    await ctx.send(f"✅ Leave messages will now be sent to {channel.mention}")

@setup.command(name="autorole", description="Sets the default role automatically assigned to new members.")
@commands.has_permissions(administrator=True)
async def setup_autorole(ctx: commands.Context, role: discord.Role):
    auto_roles[ctx.guild.id] = role.id
    await ctx.send(f"✅ Auto-role set to **{role.name}**. New members will automatically receive this role.")

# ==========================================
# 4. WELCOME & LEAVE EVENTS
# ==========================================
@bot.event
async def on_member_join(member):
    role_id = auto_roles.get(member.guild.id)
    if role_id:
        role = member.guild.get_role(role_id)
        if role:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                print("Could not assign auto-role: Missing permissions.")

    channel_id = join_channels.get(member.guild.id)
    channel = member.guild.get_channel(channel_id) if channel_id else discord.utils.get(member.guild.text_channels, name="welcome")

    if channel:
        embed = discord.Embed(
            title=f"👋 Welcome to {member.guild.name}!",
            description=f"Welcome {member.mention}! We're glad to have you here.\n\nType `/cmds` or `!cmds` to view all available commands.",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Member #{member.guild.member_count}")
        await channel.send(embed=embed)

@bot.event
async def on_member_remove(member):
    channel_id = leave_channels.get(member.guild.id)
    channel = member.guild.get_channel(channel_id) if channel_id else discord.utils.get(member.guild.text_channels, name="goodbye")

    if channel:
        embed = discord.Embed(
            title="📤 Member Left",
            description=f"**{member.name}** has left the server. We hope to see you again!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Remaining members: {member.guild.member_count}")
        await channel.send(embed=embed)

# ==========================================
# 5. INSTANT SYNC COMMAND
# ==========================================
@bot.command(name="sync")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def sync_commands(ctx: commands.Context):
    bot.tree.copy_global_to(guild=ctx.guild)
    synced = await bot.tree.sync(guild=ctx.guild)
    await ctx.send(f"✅ Successfully synced **{len(synced)}** commands instantly to this server!")

# ==========================================
# 6. HELP & UTILITY COMMANDS
# ==========================================
@bot.hybrid_command(name="cmds", description="Lists all available bot commands.")
async def list_commands(ctx: commands.Context):
    embed = discord.Embed(
        title="📜 Carl-Style Bot Commands List",
        description="Here are all active commands loaded on your bot:",
        color=discord.Color.gold()
    )
    embed.add_field(name="⚙️ Configuration",
                    value="`/setup join`, `/setup leave`, `/setup autorole`, `!sync`", inline=False)
    embed.add_field(name="🛡️ Moderation",
                    value="`/kick`, `/ban`, `/unban`, `/warn`, `/mute`, `/unmute`, `/clear`, `/lock`, `/unlock`, `/slowmode`",
                    inline=False)
    embed.add_field(name="🛠️ Utilities & UI",
                    value="`/panel`, `/makelink`, `/autolink`, `/poll`, `/avatar`, `/whois`, `/serverinfo`, `/usercount`, `/ping`, `/say`",
                    inline=False)
    embed.add_field(name="🎲 Fun Games", value="`/8ball`, `/coinflip`, `/roll`", inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="poll", description="Creates a simple voting poll.")
async def poll(ctx: commands.Context, *, question: str):
    embed = discord.Embed(title="📊 Server Poll", description=question, color=discord.Color.blue())
    embed.set_footer(text=f"Poll started by {ctx.author.display_name}")
    message = await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)
    await message.add_reaction("👍")
    await message.add_reaction("👎")

@bot.hybrid_command(name="avatar", description="Displays a user's full profile picture.")
async def avatar(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"Avatar - {member.name}", color=discord.Color.purple())
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="usercount", description="Shows total server members.")
@commands.guild_only()
async def usercount(ctx: commands.Context):
    await ctx.send(f"👥 **{ctx.guild.name}** currently has **{ctx.guild.member_count}** members!")

@bot.hybrid_command(name="say", description="Makes the bot broadcast a message.")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def say(ctx: commands.Context, *, message: str):
    await ctx.send(message, allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="ping", description="Checks the bot's latency.")
async def ping(ctx: commands.Context):
    latency = round(bot.latency * 1000)
    await ctx.send(f"Pong! 🏓 Latency: **{latency}ms**", ephemeral=True)

@bot.hybrid_command(name="whois", description="Displays info about a user.")
@commands.guild_only()
async def whois(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    joined = member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "Unknown"
    embed = discord.Embed(title=f"User Info - {member.name}", color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=member.id, inline=True)
    embed.add_field(name="Joined Server", value=joined, inline=True)
    embed.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d"), inline=True)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="serverinfo", description="Displays info about this server.")
@commands.guild_only()
async def serverinfo(ctx: commands.Context):
    guild = ctx.guild
    embed = discord.Embed(title=f"Server Info - {guild.name}", color=discord.Color.blurple())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=guild.owner, inline=True)
    embed.add_field(name="Members", value=guild.member_count, inline=True)
    embed.add_field(name="Creation Date", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
    await ctx.send(embed=embed)

# ==========================================
# 7. FUN COMMANDS
# ==========================================
@bot.hybrid_command(name="8ball", description="Ask the Magic 8-Ball a question.")
async def eightball(ctx: commands.Context, *, question: str):
    responses = [
        "It is certain.", "Without a doubt.", "Yes definitely.", "Reply hazy, try again.",
        "Ask again later.", "Better not tell you now.", "Don't count on it.",
        "My reply is no.", "My sources say no.", "Outlook not so good.", "Very doubtful."
    ]
    embed = discord.Embed(title="🎱 Magic 8-Ball", color=discord.Color.dark_theme())
    embed.add_field(name="Question:", value=question, inline=False)
    embed.add_field(name="Answer:", value=random.choice(responses), inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="coinflip", description="Flips a coin.")
async def coinflip(ctx: commands.Context):
    result = random.choice(["Heads 🪙", "Tails 🪙"])
    await ctx.send(f"🪙 Coin Flip Result: **{result}**!")

@bot.hybrid_command(name="roll", description="Rolls a random number dice.")
async def roll(ctx: commands.Context, sides: int = 6):
    if sides < 1:
        await ctx.send("❌ The dice needs at least 1 side.", ephemeral=True)
        return
    await ctx.send(f"🎲 Rolled a **{random.randint(1, sides)}** (out of {sides})!")

# ==========================================
# 8. MODERATION COMMANDS
# ==========================================
@bot.hybrid_command(name="warn", description="Issues a warning to a member.")
@commands.guild_only()
@commands.has_permissions(kick_members=True)
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided."):
    embed = discord.Embed(
        title="⚠️ Warning Issued",
        description=f"**{member.display_name}** has been warned.",
        color=discord.Color.orange()
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="kick", description="Kicks a member from the server.")
@commands.guild_only()
@commands.has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided."):
    if not can_act_on(ctx, member):
        await ctx.send(HIERARCHY_MSG, ephemeral=True)
        return
    await member.kick(reason=reason)
    await ctx.send(f"✅ Kicked **{member.display_name}** | Reason: {reason}", allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="ban", description="Bans a member from the server.")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided."):
    if not can_act_on(ctx, member):
        await ctx.send(HIERARCHY_MSG, ephemeral=True)
        return
    await member.ban(reason=reason)
    await ctx.send(f"⛔ Banned **{member.display_name}** | Reason: {reason}", allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="unban", description="Unbans a user using their Discord User ID.")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
async def unban(ctx: commands.Context, user_id: str, *, reason: str = "No reason provided."):
    try:
        user = await bot.fetch_user(int(user_id))
        await ctx.guild.unban(user, reason=reason)
        await ctx.send(f"🔓 Unbanned **{user.name}** | Reason: {reason}", allowed_mentions=NO_MENTIONS)
    except Exception:
        await ctx.send(f"❌ Failed to unban ID {user_id}. Ensure the ID is valid and user is banned.", ephemeral=True)

@bot.hybrid_command(name="mute", description="Timeouts/mutes a member for a specified number of minutes.")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
async def mute(ctx: commands.Context, member: discord.Member, minutes: int, *, reason: str = "No reason provided."):
    if not 1 <= minutes <= 40320:  # Discord's max timeout is 28 days
        await ctx.send("❌ Minutes must be between 1 and 40320 (28 days).", ephemeral=True)
        return
    if not can_act_on(ctx, member):
        await ctx.send(HIERARCHY_MSG, ephemeral=True)
        return
    await member.timeout(timedelta(minutes=minutes), reason=reason)
    await ctx.send(f"🔇 Muted **{member.display_name}** for {minutes} minutes. | Reason: {reason}",
                   allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="unmute", description="Removes timeout from a member.")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx: commands.Context, member: discord.Member):
    await member.timeout(None)
    await ctx.send(f"🔊 Unmuted **{member.display_name}**.", allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="clear", description="Deletes a specified number of messages.")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def clear(ctx: commands.Context, amount: int = 5):
    amount = max(1, min(amount, 100))
    await ctx.defer(ephemeral=True)  # purging can take longer than 3 seconds
    # A typed !clear has its own message to remove too; a slash command does not
    limit = amount + 1 if ctx.interaction is None else amount
    await ctx.channel.purge(limit=limit)
    await ctx.send(f"🧹 Deleted {amount} messages.", ephemeral=True, delete_after=3)

@bot.hybrid_command(name="lock", description="Locks the current channel.")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def lock(ctx: commands.Context):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Channel locked.")

@bot.hybrid_command(name="unlock", description="Unlocks the current channel.")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Channel unlocked.")

@bot.hybrid_command(name="slowmode", description="Sets the slowmode delay for the channel in seconds.")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx: commands.Context, seconds: int):
    seconds = max(0, min(seconds, 21600))  # Discord max is 6 hours
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(f"⏱️ Slowmode set to {seconds} seconds.")

# ==========================================
# 9. LINK-DISPENSER PANEL
# ==========================================
class LinkDispenserView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(Button(label="Website", url="https://example.com", style=discord.ButtonStyle.link))
        self.add_item(Button(label="GitHub", url="https://github.com", style=discord.ButtonStyle.link))
        self.add_item(Button(label="Rules", url="https://discord.com/terms", style=discord.ButtonStyle.link))

    @discord.ui.button(label="Get Secret Link (DM)", style=discord.ButtonStyle.primary, custom_id="secret_link_btn")
    async def secret_link(self, interaction: discord.Interaction, button: Button):
        try:
            await interaction.user.send("📩 Here is your requested link: https://example.com/secret")
            await interaction.response.send_message("Check your Direct Messages!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I couldn't DM you! Please enable DMs.", ephemeral=True)

@bot.hybrid_command(name="panel", description="Posts the Link Dispenser Panel.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def send_panel(ctx: commands.Context):
    embed = discord.Embed(
        title="🔗 Server Link Dispenser",
        description="Click buttons below for resources:",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed, view=LinkDispenserView())

# ==========================================
# 9.5 CLOUDFLARE PAGES LINK GENERATOR (/makelink)
# ==========================================
BASE_DIR = Path(__file__).resolve().parent
SITE_DIR = BASE_DIR / "site-src"  # git submodule: akilesh-him/ubgforever-repo
WRANGLER_BIN = BASE_DIR / "node_modules" / ".bin" / "wrangler"
SITE_SKIP = shutil.ignore_patterns(
    ".git", ".gitignore", "CNAME", "README.md", "build-links.js", "supabase-*.sql"
)
PAGES_URL_RE = re.compile(r"https://[a-z0-9-]+\.[a-z0-9-]+\.pages\.dev")
KEEP_DEPLOYMENTS = 20  # how many recent links stay alive
_background_tasks = set()

async def cleanup_old_deployments():
    """Deletes all but the newest KEEP_DEPLOYMENTS deployments so the project doesn't pile up."""
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    project = os.environ.get("CF_PAGES_PROJECT")
    if not (token and account and project):
        return
    base = f"https://api.cloudflare.com/client/v4/accounts/{account}/pages/projects/{project}/deployments"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(base, params={"per_page": 100}) as r:
                deployments = (await r.json()).get("result", [])
            for d in deployments[KEEP_DEPLOYMENTS:]:  # API returns newest first
                async with session.delete(f"{base}/{d['id']}", params={"force": "true"}) as r:
                    if r.status >= 400:
                        print(f"cleanup: could not delete {d['id']}: {r.status}")
    except Exception as e:
        print(f"cleanup failed: {e!r}")

async def deploy_site() -> str:
    """Copies the site to a temp folder, deploys it to Cloudflare Pages, returns the unique deployment URL."""
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    project = os.environ.get("CF_PAGES_PROJECT")
    if not (token and account and project):
        raise RuntimeError("CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID / CF_PAGES_PROJECT not set")
    if not (SITE_DIR / "index.html").exists():
        raise RuntimeError("site-src is empty (run: git submodule update --init --recursive)")

    wrangler = str(WRANGLER_BIN) if WRANGLER_BIN.exists() else "wrangler"
    tmp = tempfile.mkdtemp(prefix="ubg-")
    try:
        site_copy = Path(tmp) / "site"
        await asyncio.to_thread(shutil.copytree, SITE_DIR, site_copy, ignore=SITE_SKIP)

        proc = await asyncio.create_subprocess_exec(
            wrangler, "pages", "deploy", str(site_copy),
            "--project-name", project, "--branch", "main", "--commit-dirty=true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "WRANGLER_SEND_METRICS": "false"},  # token is read from env, not argv
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("wrangler timed out")
        text = out.decode(errors="ignore") + err.decode(errors="ignore")
        if proc.returncode != 0:
            raise RuntimeError(text[-300:])
        match = PAGES_URL_RE.search(text)
        if not match:
            raise RuntimeError(f"no URL found in wrangler output: {text[-300:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    task = asyncio.create_task(cleanup_old_deployments())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return match.group(0)

SURGE_BIN = BASE_DIR / "node_modules" / ".bin" / "surge"

async def deploy_surge_site() -> str:
    """Copies the site to a temp folder, deploys it to a fresh surge.sh subdomain, returns the URL."""
    token = os.environ.get("SURGE_TOKEN")
    if not token:
        raise RuntimeError("SURGE_TOKEN is not set")
    if not (SITE_DIR / "index.html").exists():
        raise RuntimeError("site-src is empty (run: git submodule update --init --recursive)")

    domain = f"ubg-{secrets.token_hex(3)}.surge.sh"
    surge = str(SURGE_BIN) if SURGE_BIN.exists() else "surge"
    tmp = tempfile.mkdtemp(prefix="ubg-")
    try:
        site_copy = Path(tmp) / "site"
        await asyncio.to_thread(shutil.copytree, SITE_DIR, site_copy, ignore=SITE_SKIP)

        proc = await asyncio.create_subprocess_exec(
            surge, str(site_copy), domain,  # token is read from env, not argv
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "SURGE_TOKEN": token, "SURGE_LOGIN": os.environ.get("SURGE_LOGIN", "")},
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("surge timed out")
        if proc.returncode != 0:
            raise RuntimeError(err.decode(errors="ignore")[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return f"https://{domain}"

HOSTS = {
    "cloudflare": ("☁️ Cloudflare", deploy_site),
    "surge": ("⚡ Surge", deploy_surge_site),
}

async def generate_links(kinds: list[str]) -> list[str]:
    """Deploys to the chosen hosts in parallel. Returns one result line per host (link or failure note)."""
    results = await asyncio.gather(*(HOSTS[k][1]() for k in kinds), return_exceptions=True)
    lines = []
    for k, res in zip(kinds, results):
        label = HOSTS[k][0]
        if isinstance(res, Exception):
            print(f"{k} deploy failed: {res!r}")  # full error stays in your logs only
            lines.append(f"{label}: ❌ failed")
        else:
            lines.append(f"{label}: {res}")
    return lines

# --- Password gate: every command that creates a site asks for LINK_PASSWORD in a private pop-up ---
LINK_PASSWORD = os.environ.get("LINK_PASSWORD", "")

def password_ok(entered: str) -> bool:
    return bool(LINK_PASSWORD) and hmac.compare_digest(entered.encode(), LINK_PASSWORD.encode())

class PasswordModal(discord.ui.Modal):
    password = discord.ui.TextInput(label="Password", placeholder="Enter the password", max_length=100)

    def __init__(self, on_success):
        super().__init__(title="Password required", timeout=300)
        self.on_success = on_success

    async def on_submit(self, interaction: discord.Interaction):
        if not LINK_PASSWORD:
            await interaction.response.send_message(
                "❌ No password is set on the bot yet (LINK_PASSWORD).", ephemeral=True)
            return
        if not password_ok(self.password.value):
            await interaction.response.send_message("❌ Wrong password.", ephemeral=True)
            return
        await self.on_success(interaction)

SLASH_ONLY_MSG = "🔒 Please use the slash command version, since it asks for the password privately."

class LinkTypeView(View):
    """Shown after the password is accepted: the user picks which kind of link to generate."""
    def __init__(self):
        super().__init__(timeout=120)

    async def _run(self, interaction: discord.Interaction, kinds: list[str]):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="⏳ Generating your link...", view=self)
        lines = await generate_links(kinds)
        await interaction.edit_original_response(content="\n".join(lines), view=None)

    @discord.ui.button(label="Cloudflare", emoji="☁️", style=discord.ButtonStyle.primary)
    async def cloudflare_btn(self, interaction: discord.Interaction, button: Button):
        await self._run(interaction, ["cloudflare"])

    @discord.ui.button(label="Surge", emoji="⚡", style=discord.ButtonStyle.primary)
    async def surge_btn(self, interaction: discord.Interaction, button: Button):
        await self._run(interaction, ["surge"])

    @discord.ui.button(label="Both", emoji="🔗", style=discord.ButtonStyle.success)
    async def both_btn(self, interaction: discord.Interaction, button: Button):
        await self._run(interaction, ["cloudflare", "surge"])

async def makelink_after_password(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Which type of link do you want?", view=LinkTypeView(), ephemeral=True)

@bot.hybrid_command(name="makelink", description="Generates a fresh Cloudflare and/or Surge link (password required).")
async def makelink(ctx: commands.Context):
    if ctx.interaction is None:  # typed as !makelink: a password typed in chat would be public
        await ctx.send(SLASH_ONLY_MSG)
        return
    await ctx.interaction.response.send_modal(PasswordModal(makelink_after_password))

# ==========================================
# 9.6 AUTO LINK POSTER (/autolink)
# ==========================================
AUTOLINK_INTERVAL_MIN = 30  # how often a new link is posted (minutes)
autolink_channel_id = int(os.environ.get("AUTOLINK_CHANNEL_ID", "0") or 0)

@tasks.loop(minutes=AUTOLINK_INTERVAL_MIN)
async def autolink_loop():
    channel = bot.get_channel(autolink_channel_id)
    if channel is None:
        return
    lines = await generate_links(["cloudflare", "surge"])
    if all("❌ failed" in line for line in lines):
        print("autolink: both deploys failed, nothing posted")
        return

    try:
        await channel.send("🔗 **New links:**\n" + "\n".join(lines))
    except discord.HTTPException as e:
        print(f"autolink could not post: {e!r}")

@autolink_loop.before_loop
async def before_autolink_loop():
    await bot.wait_until_ready()

@bot.hybrid_group(name="autolink", description="Auto-post fresh Cloudflare + Surge links on a timer.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def autolink(ctx: commands.Context):
    if ctx.invoked_subcommand is None:
        await ctx.send("Use `/autolink start #channel` or `/autolink stop`.")

@autolink.command(name="start", description="Start posting a new link every 30 minutes in a channel.")
@commands.has_permissions(administrator=True)
async def autolink_start(ctx: commands.Context, channel: discord.TextChannel):
    if ctx.interaction is None:  # typed as !autolink start: a password typed in chat would be public
        await ctx.send(SLASH_ONLY_MSG)
        return

    async def after_password(interaction: discord.Interaction):
        global autolink_channel_id
        autolink_channel_id = channel.id
        if not autolink_loop.is_running():
            autolink_loop.start()
        await interaction.response.send_message(
            f"✅ Posting a new link every {AUTOLINK_INTERVAL_MIN} min in {channel.mention}. "
            f"(To keep this after a restart, set `AUTOLINK_CHANNEL_ID={channel.id}` in your environment.)"
        )

    await ctx.interaction.response.send_modal(PasswordModal(after_password))

@autolink.command(name="stop", description="Stop the auto link posting.")
@commands.has_permissions(administrator=True)
async def autolink_stop(ctx: commands.Context):
    autolink_loop.cancel()
    await ctx.send("🛑 Auto link posting stopped.")

# ==========================================
# ERROR HANDLING (one global handler for every command)
# ==========================================
@bot.event
async def on_command_error(ctx: commands.Context, error):
    # Unwrap hybrid/invoke wrappers to get the real error
    while getattr(error, "original", None) is not None:
        error = error.original

    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        msg = "❌ You do not have permission to execute this command."
    elif isinstance(error, commands.NoPrivateMessage):
        msg = "❌ This command only works inside a server."
    elif isinstance(error, commands.CommandOnCooldown):
        msg = f"⏳ Try again in {error.retry_after:.0f}s."
    elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        msg = "❌ Invalid or missing arguments. Check the command usage."
    elif isinstance(error, discord.Forbidden):
        msg = "❌ I don't have permission to do that (check my role and permissions)."
    else:
        print(f"Unhandled error in {ctx.command}: {error!r}")
        msg = "❌ Something went wrong running that command."

    try:
        await ctx.send(msg, ephemeral=True)
    except discord.HTTPException:
        pass

# ==========================================
# 10. RUN BOT
# ==========================================
token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("Error: DISCORD_TOKEN environment variable not set.")