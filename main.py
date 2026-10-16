import asyncio
import datetime
import hmac
import json
import os
import platform
import random
import re
import secrets
import shutil
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Literal

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, View
from dotenv import load_dotenv
from flask import Flask

# Reads DISCORD_TOKEN, LINK_PASSWORD, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CF_PAGES_PROJECT,
# SURGE_TOKEN, SURGE_LOGIN, VERCEL_TOKEN, VERCEL_ORG_ID, VERCEL_PROJECT_ID
# Optional: AUTOLINK_CHANNEL_ID, AUTOLINK_POST, AUTOLINK_HOSTS, DATA_FILE, DAILY_PULL_LIMIT, MAX_POOL
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
warnings_db = {}  # (guild_id, user_id) -> [(reason, moderator), ...]

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
        self.add_view(PanelView())
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
                    value="`/kick`, `/ban`, `/unban`, `/warn`, `/mute`, `/unmute`, `/clear`, `/lock`, `/unlock`, `/slowmode`, `/warnings`, `/clearwarnings`, `/nick`, `/addrole`, `/removerole`",
                    inline=False)
    embed.add_field(name="🛠️ Utilities & UI",
                    value="`/makelink`, `/autolink`, `/poll`, `/avatar`, `/whois`, `/serverinfo`, `/usercount`, `/ping`, `/say`, `/embed`, `/botinfo`, `/uptime`, `/roleinfo`, `/channelinfo`, `/roles`, `/choose`, `/remind`",
                    inline=False)
    embed.add_field(name="🔗 Link Dispenser",
                    value="`/links source`, `/links panel`, `/links scan`, `/links add`, `/links import`, `/links generate`, `/links status`, `/links clear`",
                    inline=False)
    embed.add_field(name="🎲 Fun Games", value="`/8ball`, `/coinflip`, `/roll`, `/rps`, `/joke`, `/fact`, `/ship`, `/rate`, `/reverse`, `/hug`", inline=False)
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
    key = (ctx.guild.id, member.id)
    warnings_db.setdefault(key, []).append((reason, str(ctx.author)))
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_footer(text=f"Total warnings: {len(warnings_db[key])}")
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
# 9. LINK DISPENSER (stores links, shows a panel, hands them out)
# ==========================================
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
DAILY_PULL_LIMIT = int(os.environ.get("DAILY_PULL_LIMIT", "2"))
MAX_POOL = int(os.environ.get("MAX_POOL", "500"))  # when auto-generating, the oldest links are dropped past this size
URL_REGEX = re.compile(r"https?://[^\s<>\)\]]+")

# ---------- persistence (JSON file) ----------
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            print(f"Could not read {DATA_FILE}, starting empty: {e!r}", flush=True)
    return {"guilds": {}}

def save_data(data):
    # Write to a temp file first so a crash mid-write can't corrupt the real file
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)

data = load_data()

def get_guild_config(guild_id: int) -> dict:
    gid = str(guild_id)
    if gid not in data["guilds"]:
        data["guilds"][gid] = {
            "source_channel": None,
            "panel_channel": None,
            "panel_message": None,
            "links": [],
            "user_state": {},
        }
    cfg = data["guilds"][gid]
    cfg.setdefault("user_state", {})
    return cfg

def today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

def get_user_state(cfg: dict, user_id: int) -> dict:
    uid = str(user_id)
    today = today_utc()
    state = cfg["user_state"].get(uid)

    if state is None:
        state = {"day": today, "pull_count": 0, "seen_urls": []}
        cfg["user_state"][uid] = state
        return state

    state.setdefault("seen_urls", [])

    if state.get("day") != today:
        state["day"] = today
        state["pull_count"] = 0

    return state

# ---------- link helpers ----------
def extract_urls(text: str) -> list[str]:
    """Finds every URL in a block of text (trailing punctuation trimmed, duplicates removed, order kept)."""
    seen = set()
    urls = []
    for raw in URL_REGEX.findall(text or ""):
        url = raw.rstrip(".,;:!?\"'")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls

def message_urls(message: discord.Message) -> list[str]:
    """URLs from a message's text AND its embeds (bots often post links inside embeds)."""
    parts = [message.content or ""]
    for e in message.embeds:
        for text in (e.url, e.title, e.description):
            if text:
                parts.append(text)
        for f in e.fields:
            if f.value:
                parts.append(f.value)
    return extract_urls("\n".join(parts))

def _norm(url: str) -> str:
    return url.rstrip("/")

def add_links(cfg: dict, urls: list[str], author_id: int, author: str,
              message_id: int = 0, generated: bool = False) -> int:
    """Adds URLs that aren't already in the pool. Returns how many were actually new."""
    links = cfg["links"]
    existing = {_norm(l["url"]) for l in links}
    added = 0
    for url in urls:
        if _norm(url) in existing:
            continue
        existing.add(_norm(url))
        links.append({
            "url": url,
            "author_id": author_id,
            "author": author,
            "message_id": message_id,
            "generated": generated,
        })
        added += 1
    return added

# ---------- panel ----------
def build_panel_embed(guild_id: int) -> discord.Embed:
    cfg = get_guild_config(guild_id)
    description = "Click the button below to grab a random link!"
    if cfg.get("source_channel"):
        description += "\nWant to add a link? Just post it in the source channel."
    embed = discord.Embed(title="📎 Link Panel", description=description, color=discord.Color.blurple())
    embed.add_field(name="Links available", value=str(len(cfg["links"])), inline=True)
    embed.add_field(name="Daily limit", value=f"{DAILY_PULL_LIMIT} pulls / user", inline=True)
    embed.set_footer(text="One button. Infinite(ish) links.")
    return embed

class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Get a Link!", style=discord.ButtonStyle.primary, emoji="🎲",
                       custom_id="linkpanel:getlink")
    async def get_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        cfg = get_guild_config(interaction.guild.id)
        links = cfg["links"]
        if not links:
            await interaction.response.send_message("📭 No links yet!", ephemeral=True)
            return

        state = get_user_state(cfg, interaction.user.id)

        if state["pull_count"] >= DAILY_PULL_LIMIT:
            await interaction.response.send_message(
                f"⏳ You've hit your daily limit of **{DAILY_PULL_LIMIT}** links. Come back tomorrow (UTC).",
                ephemeral=True,
            )
            return

        seen = set(state["seen_urls"])
        available = [l for l in links if l["url"] not in seen]

        if not available:
            await interaction.response.send_message(
                "🎉 You've already received every link in the pool! Wait for new links to be added.",
                ephemeral=True,
            )
            return

        entry = random.choice(available)

        state["pull_count"] += 1
        state["seen_urls"].append(entry["url"])
        save_data(data)

        remaining = DAILY_PULL_LIMIT - state["pull_count"]
        origin = "Freshly generated" if entry.get("generated") else f"Originally shared by {entry['author']}"

        embed = discord.Embed(title="🎲 Your random link", description=f"<{entry['url']}>",
                              color=discord.Color.green())
        embed.set_footer(text=f"{origin} • {remaining} pull(s) left today")
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def refresh_panel(guild: discord.Guild):
    cfg = get_guild_config(guild.id)
    panel_id = cfg.get("panel_channel")
    if not panel_id:
        return None
    channel = guild.get_channel(panel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(panel_id)
        except discord.HTTPException:
            return None
    embed = build_panel_embed(guild.id)
    msg_id = cfg.get("panel_message")
    view = PanelView()
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
            return msg
        except (discord.NotFound, discord.HTTPException):
            cfg["panel_message"] = None
    try:
        msg = await channel.send(embed=embed, view=view)
    except discord.HTTPException as e:
        print(f"[panel] could not post panel: {e!r}", flush=True)
        return None
    cfg["panel_message"] = msg.id
    save_data(data)
    try:
        await msg.pin()
    except discord.HTTPException:
        pass
    return msg

# ---------- collecting links from the source channel ----------
@bot.event
async def on_message(message: discord.Message):
    # Other bots' messages ARE read; only this bot's own messages are skipped
    if message.guild and message.author.id != bot.user.id:
        cfg = get_guild_config(message.guild.id)
        if cfg.get("source_channel") == message.channel.id:
            urls = message_urls(message)
            if urls and add_links(cfg, urls, message.author.id, str(message.author), message.id):
                save_data(data)
                await refresh_panel(message.guild)
                try:
                    await message.add_reaction("📎")
                except discord.HTTPException:
                    pass
    await bot.process_commands(message)  # keep prefix commands like !sync working

# ---------- generating links straight into the pool ----------
async def generate_into_pool(guild: discord.Guild, kinds: list[str]):
    """Deploys the site to the chosen hosts, adds the new URLs to this server's pool and refreshes the panel."""
    results = await generate_results(kinds)
    urls = []
    for kind, res in results:
        if isinstance(res, str):
            urls.append(res)
        else:
            print(f"{kind} deploy failed: {res!r}", flush=True)
    cfg = get_guild_config(guild.id)
    added = add_links(cfg, urls, bot.user.id, "auto-generated", generated=True)
    if len(cfg["links"]) > MAX_POOL:
        del cfg["links"][:len(cfg["links"]) - MAX_POOL]
    save_data(data)
    await refresh_panel(guild)
    return results, added

# ---------- /links commands ----------
links_group = app_commands.Group(
    name="links",
    description="Manage the link dispenser",
    default_permissions=discord.Permissions(administrator=True),
    guild_only=True,
)

@links_group.command(name="source", description="Set the channel to watch for links")
@app_commands.checks.has_permissions(administrator=True)
async def links_source(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_guild_config(interaction.guild.id)
    cfg["source_channel"] = channel.id
    save_data(data)
    await interaction.response.send_message(
        f"✅ Source channel set to {channel.mention}.\n"
        "New links posted there are collected automatically. "
        "To also collect the links already in the channel, run `/links scan`.",
        ephemeral=True,
    )

@links_group.command(name="panel", description="Set the channel where the link panel lives")
@app_commands.checks.has_permissions(administrator=True)
async def links_panel(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_guild_config(interaction.guild.id)
    cfg["panel_channel"] = channel.id
    cfg["panel_message"] = None
    save_data(data)
    await interaction.response.defer(ephemeral=True)
    msg = await refresh_panel(interaction.guild)
    if msg is None:
        await interaction.followup.send(
            f"⚠️ Panel channel set to {channel.mention}, but I couldn't post there. "
            "Check that I can view the channel, send messages and embed links.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(f"✅ Panel channel set to {channel.mention}.", ephemeral=True)

@links_group.command(name="status", description="Show the current setup")
@app_commands.checks.has_permissions(administrator=True)
async def links_status(interaction: discord.Interaction):
    cfg = get_guild_config(interaction.guild.id)
    src = f"<#{cfg['source_channel']}>" if cfg.get("source_channel") else "❌ not set"
    pnl = f"<#{cfg['panel_channel']}>" if cfg.get("panel_channel") else "❌ not set"
    generated = sum(1 for l in cfg["links"] if l.get("generated"))
    auto = f"✅ every {AUTOLINK_INTERVAL_MIN} min ({', '.join(AUTOLINK_HOSTS)})" if autolink_loop.is_running() else "❌ off (`/autolink start`)"
    embed = discord.Embed(title="⚙️ Link Dispenser Setup", color=discord.Color.green())
    embed.add_field(name="Source channel", value=src, inline=False)
    embed.add_field(name="Panel channel", value=pnl, inline=False)
    embed.add_field(name="Links stored", value=f"{len(cfg['links'])} ({generated} generated)", inline=False)
    embed.add_field(name="Auto-generate", value=auto, inline=False)
    embed.add_field(name="Daily limit", value=f"{DAILY_PULL_LIMIT} / user", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@links_group.command(name="clear", description="Wipe all stored links and user history")
@app_commands.checks.has_permissions(administrator=True)
async def links_clear(interaction: discord.Interaction):
    cfg = get_guild_config(interaction.guild.id)
    cfg["links"].clear()
    cfg["user_state"] = {}
    cfg["panel_message"] = None
    save_data(data)
    await interaction.response.defer(ephemeral=True)
    await refresh_panel(interaction.guild)
    await interaction.followup.send("🗑️ Cleared all links and user history.", ephemeral=True)

@links_group.command(name="scan", description="Read the source channel's past messages and store every link found")
@app_commands.describe(limit="How many recent messages to read (0 = the whole channel)")
@app_commands.checks.has_permissions(administrator=True)
async def links_scan(interaction: discord.Interaction, limit: app_commands.Range[int, 0, 1000000] = 0):
    cfg = get_guild_config(interaction.guild.id)
    source_id = cfg.get("source_channel")
    if not source_id:
        await interaction.response.send_message("❌ Set a source channel first with `/links source`.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(source_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(source_id)
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ I can't find the source channel anymore. Set it again with `/links source`.", ephemeral=True)
            return

    await interaction.response.defer(ephemeral=True, thinking=True)

    scanned = found = added = from_bots = unreadable = 0
    try:
        async for message in channel.history(limit=limit or None, oldest_first=True):
            scanned += 1
            if message.author.id == bot.user.id:
                continue
            if message.author.bot:
                from_bots += 1
            urls = message_urls(message)
            if not urls:
                if not message.content and not message.embeds:
                    unreadable += 1
                continue
            found += len(urls)
            added += add_links(cfg, urls, message.author.id, str(message.author), message.id)
    except discord.Forbidden:
        await interaction.followup.send(
            f"❌ I can't read the history of {channel.mention}. "
            "Give me **View Channel** and **Read Message History** there.", ephemeral=True)
        return
    except discord.HTTPException as e:
        print(f"[scan] failed: {e!r}", flush=True)
        await interaction.followup.send("❌ Discord returned an error while reading the channel. Try again in a bit.",
                                        ephemeral=True)
        return

    save_data(data)
    await refresh_panel(interaction.guild)

    result = (
        f"✅ Scanned **{scanned}** message(s) in {channel.mention}.\n"
        f"Links found: **{found}** | New: **{added}** | Already stored: **{found - added}**\n"
        f"Total pool now: **{len(cfg['links'])}**\n"
        f"Messages from other bots (included): **{from_bots}**"
    )
    if unreadable:
        result += (f"\n⚠️ **{unreadable}** message(s) had no readable text. If that's a lot, check that "
                   "**Message Content Intent** is on in the developer portal (or they may just be images/files).")
    try:
        await interaction.followup.send(result, ephemeral=True)
    except discord.HTTPException:
        try:  # the interaction expired during a very long scan
            await channel.send(result)
        except discord.HTTPException:
            pass

@links_group.command(name="add", description="Paste text: every URL in it gets added to the pool")
@app_commands.describe(text="Paste anything containing URLs (one per line, sentences, whatever)")
@app_commands.checks.has_permissions(administrator=True)
async def links_add(interaction: discord.Interaction, text: str):
    urls = extract_urls(text)
    if not urls:
        await interaction.response.send_message("❌ No URLs found in that text.", ephemeral=True)
        return
    cfg = get_guild_config(interaction.guild.id)
    added = add_links(cfg, urls, interaction.user.id, f"added by {interaction.user}")
    save_data(data)
    await refresh_panel(interaction.guild)
    await interaction.response.send_message(
        f"✅ Added **{added}** new link(s) ({len(urls) - added} already stored). "
        f"Total pool now: **{len(cfg['links'])}**", ephemeral=True)

@links_group.command(name="import", description="Import links from a .txt file (one URL per line)")
@app_commands.describe(file="A .txt file with one URL per line", password="The link password (LINK_PASSWORD)")
@app_commands.checks.has_permissions(administrator=True)
async def links_import(interaction: discord.Interaction, file: discord.Attachment, password: str):
    if not LINK_PASSWORD:
        await interaction.response.send_message("❌ Importing is disabled: no LINK_PASSWORD is set on the bot.",
                                                ephemeral=True)
        return
    if not password_ok(password):
        await interaction.response.send_message("❌ Wrong password.", ephemeral=True)
        return
    if not file.filename.lower().endswith(".txt"):
        await interaction.response.send_message("❌ Only `.txt` files are supported.", ephemeral=True)
        return
    if file.size > 2 * 1024 * 1024:
        await interaction.response.send_message("❌ File too large (max 2 MB).", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        raw = await file.read()
        text = raw.decode("utf-8", errors="ignore")
    except Exception as e:
        await interaction.followup.send(f"❌ Could not read file: `{e}`", ephemeral=True)
        return

    cfg = get_guild_config(interaction.guild.id)
    urls = extract_urls(text)
    added = add_links(cfg, urls, interaction.user.id, f"imported by {interaction.user}")
    save_data(data)
    await refresh_panel(interaction.guild)
    await interaction.followup.send(
        f"✅ Import complete. Added **{added}** new ({len(urls) - added} already stored). "
        f"Total pool now: **{len(cfg['links'])}**", ephemeral=True)

@links_group.command(name="generate", description="Generate fresh links now and add them to the pool (password required)")
@app_commands.checks.has_permissions(administrator=True)
async def links_generate(interaction: discord.Interaction):
    guild = interaction.guild

    async def after_password(inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        results, added = await generate_into_pool(guild, AUTOLINK_HOSTS)
        lines = [f"✅ Added **{added}** new link(s). Pool now: **{len(get_guild_config(guild.id)['links'])}**"]
        for kind, res in results:
            if isinstance(res, Exception):
                reason = (str(res) or repr(res))[-200:].replace("`", "'")
                lines.append(f"{HOSTS[kind][0]}: ❌ `{type(res).__name__}: {reason}`")
        await inter.followup.send("\n".join(lines), ephemeral=True)

    await interaction.response.send_modal(PasswordModal(after_password))

bot.tree.add_command(links_group)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need the Administrator permission to use this."
    else:
        print(f"[command error] {error!r}", flush=True)
        msg = "❌ Something went wrong running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass

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

VERCEL_BIN = BASE_DIR / "node_modules" / ".bin" / "vercel"
VERCEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.vercel\.app")

async def deploy_vercel_site() -> str:
    """Copies the site to a temp folder, deploys it to Vercel, returns the unique deployment URL."""
    token = os.environ.get("VERCEL_TOKEN")
    org_id = os.environ.get("VERCEL_ORG_ID")
    project_id = os.environ.get("VERCEL_PROJECT_ID")
    if not (token and org_id and project_id):
        raise RuntimeError("VERCEL_TOKEN / VERCEL_ORG_ID / VERCEL_PROJECT_ID not set")
    if not (SITE_DIR / "index.html").exists():
        raise RuntimeError("site-src is empty (run: git submodule update --init --recursive)")

    vercel = str(VERCEL_BIN) if VERCEL_BIN.exists() else "vercel"
    tmp = tempfile.mkdtemp(prefix="ubg-")
    try:
        site_copy = Path(tmp) / "site"
        await asyncio.to_thread(shutil.copytree, SITE_DIR, site_copy, ignore=SITE_SKIP)

        proc = await asyncio.create_subprocess_exec(
            vercel, "deploy", str(site_copy), "--yes",  # token and project come from env, not argv
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "VERCEL_TOKEN": token, "VERCEL_ORG_ID": org_id, "VERCEL_PROJECT_ID": project_id},
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("vercel timed out")
        out_text = out.decode(errors="ignore")
        err_text = err.decode(errors="ignore")
        if proc.returncode != 0:
            raise RuntimeError((err_text or out_text)[-400:])
        # Vercel prints the deployment URL on stdout; fall back to stderr just in case
        match = VERCEL_URL_RE.search(out_text) or VERCEL_URL_RE.search(err_text)
        if not match:
            raise RuntimeError(f"no URL found in vercel output: {(out_text + err_text)[-300:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return match.group(0)

HOSTS = {
    "cloudflare": ("☁️ Cloudflare", deploy_site),
    "surge": ("⚡ Surge", deploy_surge_site),
    "vercel": ("🔺 Vercel", deploy_vercel_site),
}

async def generate_results(kinds: list[str]):
    """Deploys to the chosen hosts in parallel. Returns [(kind, url_or_exception), ...]."""
    results = await asyncio.gather(*(HOSTS[k][1]() for k in kinds), return_exceptions=True)
    return list(zip(kinds, results))

async def generate_links(kinds: list[str], show_errors: bool = False) -> list[str]:
    """Deploys to the chosen hosts in parallel. Returns one result line per host (link or failure note).
    With show_errors=True the failure reason is included in the line (only used for private replies)."""
    lines = []
    for k, res in await generate_results(kinds):
        label = HOSTS[k][0]
        if isinstance(res, Exception):
            print(f"{k} deploy failed: {res!r}", flush=True)
            detail = ""
            if show_errors:
                reason = (str(res) or repr(res))[-600:].replace("`", "'")
                detail = f"\n```{type(res).__name__}: {reason}```"
            lines.append(f"{label}: ❌ failed{detail}")
        else:
            lines.append(f"{label}: <{res}>")  # <> stops Discord from showing a link preview
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

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"PasswordModal error: {error!r}", flush=True)
        msg = f"❌ Something went wrong: {type(error).__name__}: {str(error)[-300:]}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

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
        lines = await generate_links(kinds, show_errors=True)  # private reply, safe to show details
        await interaction.edit_original_response(content="\n".join(lines), view=None)

    @discord.ui.button(label="Cloudflare", emoji="☁️", style=discord.ButtonStyle.primary)
    async def cloudflare_btn(self, interaction: discord.Interaction, button: Button):
        await self._run(interaction, ["cloudflare"])

    @discord.ui.button(label="Surge", emoji="⚡", style=discord.ButtonStyle.primary)
    async def surge_btn(self, interaction: discord.Interaction, button: Button):
        await self._run(interaction, ["surge"])

    @discord.ui.button(label="Vercel", emoji="🔺", style=discord.ButtonStyle.primary)
    async def vercel_btn(self, interaction: discord.Interaction, button: Button):
        await self._run(interaction, ["vercel"])

    @discord.ui.button(label="All", emoji="🔗", style=discord.ButtonStyle.success)
    async def all_btn(self, interaction: discord.Interaction, button: Button):
        await self._run(interaction, list(HOSTS))

async def makelink_after_password(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Which type of link do you want?", view=LinkTypeView(), ephemeral=True)

@bot.hybrid_command(name="makelink", description="Generates a fresh Cloudflare, Surge and/or Vercel link (password required).")
async def makelink(ctx: commands.Context):
    if ctx.interaction is None:  # typed as !makelink: a password typed in chat would be public
        await ctx.send(SLASH_ONLY_MSG)
        return
    await ctx.interaction.response.send_modal(PasswordModal(makelink_after_password))

# ==========================================
# 9.6 AUTO LINK POSTER (/autolink)
# ==========================================
AUTOLINK_INTERVAL_MIN = 30  # how often new links are generated (minutes)
autolink_channel_id = int(os.environ.get("AUTOLINK_CHANNEL_ID", "0") or 0)
# True = also post each batch publicly in that channel. False (default) = only add them to the dispenser pool.
autolink_post = os.environ.get("AUTOLINK_POST", "0").strip().lower() in ("1", "true", "yes")
# Which hosts the timer deploys to. Override with AUTOLINK_HOSTS=cloudflare,surge,vercel (any subset).
AUTOLINK_HOSTS = [h.strip() for h in os.environ.get("AUTOLINK_HOSTS", "cloudflare,surge,vercel").split(",") if h.strip() in HOSTS] or list(HOSTS)

@tasks.loop(minutes=AUTOLINK_INTERVAL_MIN)
async def autolink_loop():
    channel = bot.get_channel(autolink_channel_id)
    if channel is None or getattr(channel, "guild", None) is None:
        return
    results, added = await generate_into_pool(channel.guild, AUTOLINK_HOSTS)
    ok = [(k, r) for k, r in results if isinstance(r, str)]
    if not ok:
        print("autolink: all deploys failed, nothing added", flush=True)
        return

    if autolink_post:
        lines = [f"{HOSTS[k][0]}: <{r}>" for k, r in ok]
        try:
            await channel.send("🔗 **New links:**\n" + "\n".join(lines))
        except discord.HTTPException as e:
            print(f"autolink could not post: {e!r}", flush=True)

@autolink_loop.before_loop
async def before_autolink_loop():
    await bot.wait_until_ready()

@bot.hybrid_group(name="autolink", description="Auto-generate fresh links on a timer.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def autolink(ctx: commands.Context):
    if ctx.invoked_subcommand is None:
        await ctx.send("Use `/autolink start #channel` or `/autolink stop`.")

@autolink.command(name="start", description="Generate new links every 30 minutes into this server's dispenser pool.")
@commands.has_permissions(administrator=True)
async def autolink_start(ctx: commands.Context, channel: discord.TextChannel, post: bool = False):
    if ctx.interaction is None:  # typed as !autolink start: a password typed in chat would be public
        await ctx.send(SLASH_ONLY_MSG)
        return

    async def after_password(interaction: discord.Interaction):
        global autolink_channel_id, autolink_post
        autolink_channel_id = channel.id
        autolink_post = post
        if not autolink_loop.is_running():
            autolink_loop.start()
        where = f" and posting them in {channel.mention}" if post else " (not posting them publicly)"
        await interaction.response.send_message(
            f"✅ Generating new links every {AUTOLINK_INTERVAL_MIN} min into the dispenser pool{where}.\n"
            f"To keep this after a restart, set `AUTOLINK_CHANNEL_ID={channel.id}`"
            f"{' and `AUTOLINK_POST=1`' if post else ''} in your environment.",
            ephemeral=True,
        )

    await ctx.interaction.response.send_modal(PasswordModal(after_password))

@autolink.command(name="stop", description="Stop the auto link generation.")
@commands.has_permissions(administrator=True)
async def autolink_stop(ctx: commands.Context):
    autolink_loop.cancel()
    await ctx.send("🛑 Auto link generation stopped.")

# ==========================================
# 11. EXTRA COMMANDS
# ==========================================
START_TIME = time.time()

def can_manage_role(ctx: commands.Context, role: discord.Role) -> bool:
    """True if both the author and the bot sit above this role and it's a normal, assignable role."""
    if role.is_default() or role.managed:
        return False
    if ctx.author != ctx.guild.owner and ctx.author.top_role <= role:
        return False
    return ctx.guild.me.top_role > role

# ---------- Moderation extras ----------
@bot.hybrid_command(name="warnings", description="Shows the warnings a member has received.")
@commands.guild_only()
@commands.has_permissions(kick_members=True)
async def warnings_cmd(ctx: commands.Context, member: discord.Member):
    items = warnings_db.get((ctx.guild.id, member.id), [])
    if not items:
        await ctx.send(f"✅ **{member.display_name}** has no warnings.", allowed_mentions=NO_MENTIONS)
        return
    embed = discord.Embed(title=f"⚠️ Warnings for {member.display_name}", color=discord.Color.orange())
    for i, (reason, mod) in enumerate(items[-10:], start=max(1, len(items) - 9)):
        embed.add_field(name=f"#{i}", value=f"{reason}\n*by {mod}*", inline=False)
    embed.set_footer(text=f"Total warnings: {len(items)} (resets when the bot restarts)")
    await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="clearwarnings", description="Clears all warnings for a member.")
@commands.guild_only()
@commands.has_permissions(kick_members=True)
async def clearwarnings(ctx: commands.Context, member: discord.Member):
    removed = len(warnings_db.pop((ctx.guild.id, member.id), []))
    await ctx.send(f"🧹 Cleared {removed} warning(s) for **{member.display_name}**.", allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="nick", description="Changes (or resets) a member's nickname.")
@commands.guild_only()
@commands.has_permissions(manage_nicknames=True)
async def nick(ctx: commands.Context, member: discord.Member, *, nickname: str = None):
    if nickname and len(nickname) > 32:
        await ctx.send("❌ Nicknames can be at most 32 characters.", ephemeral=True)
        return
    self_ok = member == ctx.author and member != ctx.guild.owner and ctx.guild.me.top_role > member.top_role
    if not self_ok and not can_act_on(ctx, member):
        await ctx.send(HIERARCHY_MSG, ephemeral=True)
        return
    await member.edit(nick=nickname)
    if nickname:
        await ctx.send(f"✏️ Nickname for **{member.name}** set to **{nickname}**.", allowed_mentions=NO_MENTIONS)
    else:
        await ctx.send(f"✏️ Nickname for **{member.name}** reset.", allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="addrole", description="Gives a role to a member.")
@commands.guild_only()
@commands.has_permissions(manage_roles=True)
async def addrole(ctx: commands.Context, member: discord.Member, role: discord.Role):
    if not can_manage_role(ctx, role):
        await ctx.send("❌ I can't give that role (it's above me or you, or it's managed).", ephemeral=True)
        return
    await member.add_roles(role, reason=f"Added by {ctx.author}")
    await ctx.send(f"✅ Gave **{role.name}** to **{member.display_name}**.", allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="removerole", description="Removes a role from a member.")
@commands.guild_only()
@commands.has_permissions(manage_roles=True)
async def removerole(ctx: commands.Context, member: discord.Member, role: discord.Role):
    if not can_manage_role(ctx, role):
        await ctx.send("❌ I can't remove that role (it's above me or you, or it's managed).", ephemeral=True)
        return
    await member.remove_roles(role, reason=f"Removed by {ctx.author}")
    await ctx.send(f"✅ Removed **{role.name}** from **{member.display_name}**.", allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="embed", description="Posts a custom embed message (admins only).")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def embed_cmd(ctx: commands.Context, title: str, description: str, channel: discord.TextChannel = None):
    target = channel or ctx.channel
    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    await target.send(embed=embed, allowed_mentions=NO_MENTIONS)
    await ctx.send(f"✅ Embed posted in {target.mention}.", ephemeral=True)

# ---------- Info ----------
@bot.hybrid_command(name="botinfo", description="Shows info about this bot.")
async def botinfo(ctx: commands.Context):
    embed = discord.Embed(title=f"🤖 {bot.user.name}", color=discord.Color.blurple())
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name="Servers", value=len(bot.guilds), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Uptime", value=format_uptime(), inline=True)
    embed.add_field(name="discord.py", value=discord.__version__, inline=True)
    embed.add_field(name="Python", value=platform.python_version(), inline=True)
    await ctx.send(embed=embed)

def format_uptime() -> str:
    seconds = int(time.time() - START_TIME)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{days}d {hours}h {minutes}m {secs}s"

@bot.hybrid_command(name="uptime", description="Shows how long the bot has been online.")
async def uptime(ctx: commands.Context):
    await ctx.send(f"⏱️ Online for **{format_uptime()}**.")

@bot.hybrid_command(name="roleinfo", description="Shows info about a role.")
@commands.guild_only()
async def roleinfo(ctx: commands.Context, role: discord.Role):
    embed = discord.Embed(title=f"Role Info - {role.name}", color=role.color)
    embed.add_field(name="ID", value=role.id, inline=True)
    embed.add_field(name="Members", value=len(role.members), inline=True)
    embed.add_field(name="Position", value=role.position, inline=True)
    embed.add_field(name="Color", value=str(role.color), inline=True)
    embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No", inline=True)
    embed.add_field(name="Created", value=role.created_at.strftime("%Y-%m-%d"), inline=True)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="channelinfo", description="Shows info about a text channel.")
@commands.guild_only()
async def channelinfo(ctx: commands.Context, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    embed = discord.Embed(title=f"Channel Info - #{channel.name}", color=discord.Color.blurple())
    embed.add_field(name="ID", value=channel.id, inline=True)
    embed.add_field(name="Category", value=channel.category.name if channel.category else "None", inline=True)
    embed.add_field(name="Slowmode", value=f"{channel.slowmode_delay}s", inline=True)
    embed.add_field(name="NSFW", value="Yes" if channel.is_nsfw() else "No", inline=True)
    embed.add_field(name="Created", value=channel.created_at.strftime("%Y-%m-%d"), inline=True)
    if channel.topic:
        embed.add_field(name="Topic", value=channel.topic[:1000], inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="roles", description="Lists all roles in this server.")
@commands.guild_only()
async def roles_cmd(ctx: commands.Context):
    names = [r.mention for r in reversed(ctx.guild.roles[1:])]  # skip @everyone
    text = ", ".join(names) or "No roles."
    if len(text) > 1000:
        text = text[:1000].rsplit(",", 1)[0] + ", ..."
    embed = discord.Embed(title=f"Roles in {ctx.guild.name} ({len(names)})", description=text, color=discord.Color.blurple())
    await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)

# ---------- Utility ----------
@bot.hybrid_command(name="choose", description="Picks one option from a comma-separated list.")
async def choose(ctx: commands.Context, *, options: str):
    items = [o.strip() for o in options.split(",") if o.strip()]
    if len(items) < 2:
        await ctx.send("❌ Give me at least two options separated by commas.", ephemeral=True)
        return
    await ctx.send(f"🤔 I choose: **{random.choice(items)}**", allowed_mentions=NO_MENTIONS)

async def _send_reminder(channel, user, message):
    try:
        await channel.send(f"⏰ {user.mention} reminder: {message}",
                           allowed_mentions=discord.AllowedMentions(users=[user], everyone=False, roles=False))
    except discord.HTTPException as e:
        print(f"reminder could not be sent: {e!r}", flush=True)

@bot.hybrid_command(name="remind", description="Reminds you in a channel after a number of minutes (up to 24h).")
async def remind(ctx: commands.Context, minutes: int, *, message: str):
    if not 1 <= minutes <= 1440:
        await ctx.send("❌ Minutes must be between 1 and 1440 (24 hours).", ephemeral=True)
        return
    await ctx.send(f"⏰ Okay, I'll remind you in **{minutes}** minute(s). (Lost if the bot restarts.)", ephemeral=True)

    async def later():
        await asyncio.sleep(minutes * 60)
        await _send_reminder(ctx.channel, ctx.author, message)

    task = asyncio.create_task(later())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

# ---------- More fun ----------
@bot.hybrid_command(name="rps", description="Play rock, paper, scissors against the bot.")
async def rps(ctx: commands.Context, choice: Literal["rock", "paper", "scissors"]):
    bot_choice = random.choice(["rock", "paper", "scissors"])
    beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    if choice == bot_choice:
        result = "It's a tie!"
    elif beats[choice] == bot_choice:
        result = "You win! 🎉"
    else:
        result = "I win! 😎"
    await ctx.send(f"You: **{choice}** | Me: **{bot_choice}**\n{result}")

JOKES = [
    "Why don't scientists trust atoms? Because they make up everything!",
    "I told my computer I needed a break, and now it won't stop sending me vacation ads.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "What do you call fake spaghetti? An impasta.",
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I'd tell you a UDP joke, but you might not get it.",
    "Why was the math book sad? It had too many problems.",
    "What did the ocean say to the beach? Nothing, it just waved.",
]

FACTS = [
    "Octopuses have three hearts.",
    "A day on Venus is longer than a year on Venus.",
    "Bananas are berries, but strawberries aren't.",
    "Wombat droppings are cube-shaped.",
    "Sharks have been around longer than trees have.",
    "Honey found in ancient Egyptian tombs was still edible.",
    "The Eiffel Tower can grow a few centimeters taller in hot weather because the metal expands.",
    "Sea otters often hold hands while they sleep so they don't drift apart.",
]

@bot.hybrid_command(name="joke", description="Tells a random joke.")
async def joke(ctx: commands.Context):
    await ctx.send(f"😂 {random.choice(JOKES)}")

@bot.hybrid_command(name="fact", description="Shares a random fun fact.")
async def fact(ctx: commands.Context):
    await ctx.send(f"🧠 **Did you know?** {random.choice(FACTS)}")

@bot.hybrid_command(name="ship", description="Checks the 'compatibility' of two members (just for fun).")
@commands.guild_only()
async def ship(ctx: commands.Context, first: discord.Member, second: discord.Member):
    pct = (first.id * second.id) % 101  # same pair always gives the same result
    filled = pct // 10
    bar = "❤️" * filled + "🖤" * (10 - filled)
    await ctx.send(f"💘 **{first.display_name}** + **{second.display_name}** = **{pct}%**\n{bar}",
                   allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="rate", description="Rates anything out of 10.")
async def rate(ctx: commands.Context, *, thing: str):
    await ctx.send(f"⭐ I rate **{thing}** a **{random.randint(0, 10)}/10**.", allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="reverse", description="Reverses your text.")
async def reverse(ctx: commands.Context, *, text: str):
    await ctx.send(text[::-1], allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="hug", description="Gives someone a hug.")
@commands.guild_only()
async def hug(ctx: commands.Context, member: discord.Member):
    await ctx.send(f"🤗 **{ctx.author.display_name}** hugs **{member.display_name}**!", allowed_mentions=NO_MENTIONS)

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