import asyncio
import os
import random
import secrets
import shutil
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View
from dotenv import load_dotenv
from flask import Flask

load_dotenv()  # reads DISCORD_TOKEN, SURGE_TOKEN, SURGE_LOGIN from .env (ignored on Render, which uses its own env vars)

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

# In-memory storage for server configurations
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

# ==========================================
# 3. SETUP COMMANDS (Join/Leave/AutoRole)
# ==========================================

@bot.hybrid_group(name="setup", description="Configure bot settings for your server.")
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
    # Auto-role assignment
    role_id = auto_roles.get(member.guild.id)
    if role_id:
        role = member.guild.get_role(role_id)
        if role:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                print("Could not assign auto-role: Missing permissions.")

    # Welcome message
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
    embed.add_field(
        name="⚙️ Configuration",
        value="`/setup join`, `/setup leave`, `/setup autorole`, `!sync`",
        inline=False
    )
    embed.add_field(
        name="🛡️ Moderation",
        value="`/kick`, `/ban`, `/unban`, `/warn`, `/mute`, `/unmute`, `/clear`, `/lock`, `/unlock`, `/slowmode`",
        inline=False
    )
    embed.add_field(
        name="🛠️ Utilities & UI",
        value="`/panel`, `/makelink`, `/poll`, `/avatar`, `/whois`, `/serverinfo`, `/usercount`, `/ping`, `/say`",
        inline=False
    )
    embed.add_field(
        name="🎲 Fun Games",
        value="`/8ball`, `/coinflip`, `/roll`",
        inline=False
    )
    await ctx.send(embed=embed)

@bot.hybrid_command(name="poll", description="Creates a simple voting poll.")
async def poll(ctx: commands.Context, *, question: str):
    embed = discord.Embed(
        title="📊 Server Poll",
        description=question,
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Poll started by {ctx.author.display_name}")
    message = await ctx.send(embed=embed)
    await message.add_reaction("👍")
    await message.add_reaction("👎")

@bot.hybrid_command(name="avatar", description="Displays a user's full profile picture.")
async def avatar(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"Avatar - {member.name}", color=discord.Color.purple())
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="usercount", description="Shows total server members.")
async def usercount(ctx: commands.Context):
    await ctx.send(f"👥 **{ctx.guild.name}** currently has **{ctx.guild.member_count}** members!")

@bot.hybrid_command(name="say", description="Makes the bot broadcast a message.")
@commands.has_permissions(manage_messages=True)
async def say(ctx: commands.Context, *, message: str):
    await ctx.send(message)

@bot.hybrid_command(name="ping", description="Checks the bot's latency.")
async def ping(ctx: commands.Context):
    latency = round(bot.latency * 1000)
    await ctx.send(f"Pong! 🏓 Latency: **{latency}ms**", ephemeral=True)

@bot.hybrid_command(name="whois", description="Displays info about a user.")
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
    answer = random.choice(responses)
    embed = discord.Embed(title="🎱 Magic 8-Ball", color=discord.Color.dark_theme())
    embed.add_field(name="Question:", value=question, inline=False)
    embed.add_field(name="Answer:", value=answer, inline=False)
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
    result = random.randint(1, sides)
    await ctx.send(f"🎲 Rolled a **{result}** (out of {sides})!")

# ==========================================
# 8. EXTENDED MODERATION COMMANDS
# ==========================================

@bot.hybrid_command(name="warn", description="Issues a warning to a member.")
@commands.has_permissions(kick_members=True)
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided."):
    embed = discord.Embed(
        title="⚠️ Warning Issued",
        description=f"**{member.display_name}** has been warned.",
        color=discord.Color.orange()
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="kick", description="Kicks a member from the server.")
@commands.has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided."):
    await member.kick(reason=reason)
    await ctx.send(f"✅ Kicked **{member.display_name}** | Reason: {reason}")

@bot.hybrid_command(name="ban", description="Bans a member from the server.")
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided."):
    await member.ban(reason=reason)
    await ctx.send(f"⛔ Banned **{member.display_name}** | Reason: {reason}")

@bot.hybrid_command(name="unban", description="Unbans a user using their Discord User ID.")
@commands.has_permissions(ban_members=True)
async def unban(ctx: commands.Context, user_id: str, *, reason: str = "No reason provided."):
    try:
        user = await bot.fetch_user(int(user_id))
        await ctx.guild.unban(user, reason=reason)
        await ctx.send(f"🔓 Unbanned **{user.name}** | Reason: {reason}")
    except Exception:
        await ctx.send(f"❌ Failed to unban ID {user_id}. Ensure the ID is valid and user is banned.")

@bot.hybrid_command(name="mute", description="Timeouts/mutes a member for a specified number of minutes.")
@commands.has_permissions(moderate_members=True)
async def mute(ctx: commands.Context, member: discord.Member, minutes: int, *, reason: str = "No reason provided."):
    duration = timedelta(minutes=minutes)
    await member.timeout(duration, reason=reason)
    await ctx.send(f"🔇 Muted **{member.display_name}** for {minutes} minutes. | Reason: {reason}")

@bot.hybrid_command(name="unmute", description="Removes timeout from a member.")
@commands.has_permissions(moderate_members=True)
async def unmute(ctx: commands.Context, member: discord.Member):
    await member.timeout(None)
    await ctx.send(f"🔊 Unmuted **{member.display_name}**.")

@bot.hybrid_command(name="clear", description="Deletes a specified number of messages.")
@commands.has_permissions(manage_messages=True)
async def clear(ctx: commands.Context, amount: int = 5):
    await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"🧹 Deleted {amount} messages.", delete_after=3)

@bot.hybrid_command(name="lock", description="Locks the current channel.")
@commands.has_permissions(manage_channels=True)
async def lock(ctx: commands.Context):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Channel locked.")

@bot.hybrid_command(name="unlock", description="Unlocks the current channel.")
@commands.has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Channel unlocked.")

@bot.hybrid_command(name="slowmode", description="Sets the slowmode delay for the channel in seconds.")
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx: commands.Context, seconds: int):
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
@commands.has_permissions(administrator=True)
async def send_panel(ctx: commands.Context):
    embed = discord.Embed(
        title="🔗 Server Link Dispenser",
        description="Click buttons below for resources:",
        color=discord.Color.green()
    )
    view = LinkDispenserView()
    await ctx.send(embed=embed, view=view)

# ==========================================
# 9.5 SURGE LINK GENERATOR (/makelink)
# ==========================================
BASE_DIR = Path(__file__).resolve().parent
SITE_DIR = BASE_DIR / "site-src"  # git submodule: akilesh-him/ubgforever-repo
SURGE_BIN = BASE_DIR / "node_modules" / ".bin" / "surge"
SITE_SKIP = shutil.ignore_patterns(
    ".git", ".gitignore", "CNAME", "README.md", "build-links.js", "supabase-*.sql"
)

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
            surge, str(site_copy), domain, "--token", token,
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

@bot.hybrid_command(name="makelink", description="Generates a fresh ubgforever surge.sh link.")
@commands.cooldown(1, 60, commands.BucketType.user)
async def makelink(ctx: commands.Context):
    await ctx.defer(ephemeral=True)
    try:
        url = await deploy_surge_site()
    except Exception as e:
        print(f"makelink failed: {e!r}")
        # TEMP DEBUG: shows the real error (only you can see it). Swap back to a generic message once it works.
        await ctx.send(f"❌ makelink failed: `{str(e)[:1500] or repr(e)}`", ephemeral=True)
        return
    await ctx.send(f"✅ Here's your link: {url}", ephemeral=True)

# ==========================================
# ERROR HANDLING
# ==========================================
@setup.error
@setup_join.error
@setup_leave.error
@setup_autorole.error
@sync_commands.error
@warn.error
@kick.error
@ban.error
@unban.error
@mute.error
@unmute.error
@clear.error
@lock.error
@unlock.error
@slowmode.error
@say.error
@send_panel.error
async def permissions_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You do not have permission to execute this command.", ephemeral=True)

@makelink.error
async def makelink_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Try again in {error.retry_after:.0f}s.", ephemeral=True)

# ==========================================
# 10. RUN BOT
# ==========================================
token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("Error: DISCORD_TOKEN environment variable not set.")