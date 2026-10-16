import asyncio
import hmac
import json
import os
import random
import re
import secrets
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
from dotenv import load_dotenv
from flask import Flask

# Reads DISCORD_TOKEN, LINK_PASSWORD, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CF_PAGES_PROJECT,
# SURGE_TOKEN, SURGE_LOGIN, VERCEL_TOKEN, VERCEL_ORG_ID, VERCEL_PROJECT_ID
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

# ==========================================
# 2. IN-MEMORY STORAGE (resets on restart)
# ==========================================
join_channels = {}
leave_channels = {}
auto_roles = {}
sniped = {}
edited = {}
afk_users = {}
warnings = {}  # {guild_id: {user_id: [ {reason, moderator, time} ]}}

# ==========================================
# 3. DISCORD BOT SETUP
# ==========================================
class CustomBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(LinkDispenserView())
        try:
            synced = await self.tree.sync()
            print(f"✅ Synced {len(synced)} global commands.")
        except Exception as e:
            print(f"Sync error: {e}")

bot = CustomBot()

@bot.event
async def on_ready():
    if not hasattr(bot, "start_time"):
        bot.start_time = time.time()
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
# 4. SETUP COMMANDS (Join/Leave/AutoRole)
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
# 5. WELCOME & LEAVE EVENTS
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
            description=f"**{member.display_name}** has left the server. We hope to see you again!",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Remaining members: {member.guild.member_count}")
        await channel.send(embed=embed)

# ==========================================
# 6. MESSAGE EVENTS (AFK, snipe, editsnipe)
# ==========================================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return await bot.process_commands(message)

    # Remove AFK if the user talks
    if message.author.id in afk_users:
        del afk_users[message.author.id]
        try:
            await message.channel.send(f"👋 Welcome back, {message.author.mention}!",
                                       delete_after=5, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            pass

    # Notify if a mentioned user is AFK
    for mention in message.mentions:
        if mention.id in afk_users:
            try:
                await message.channel.send(f"💤 {mention.display_name} is AFK: {afk_users[mention.id]}",
                                           delete_after=8, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                pass

    await bot.process_commands(message)

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    sniped[message.channel.id] = {
        "content": message.content,
        "author": message.author,
        "time": datetime.now(timezone.utc),
    }

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild or before.content == after.content:
        return
    edited[before.channel.id] = {
        "before": before.content,
        "after": after.content,
        "author": before.author,
    }

# ==========================================
# 7. INSTANT SYNC COMMAND
# ==========================================
@bot.command(name="sync")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def sync_commands(ctx: commands.Context):
    synced = await bot.tree.sync(guild=ctx.guild)
    await ctx.send(f"✅ Successfully synced **{len(synced)}** commands instantly to this server!")

# ==========================================
# 8. HELP & UTILITY COMMANDS
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
                    value="`/kick`, `/ban`, `/softban`, `/unban`, `/banlist`, `/warn`, `/warnings`, `/clear`, `/purgeuser`, `/nuke`, `/mute`, `/unmute`, `/lock`, `/unlock`, `/slowmode`, `/nick`, `/role`",
                    inline=False)
    embed.add_field(name="🛠️ Utilities",
                    value="`/panel`, `/makelink`, `/autolink`, `/poll`, `/avatar`, `/banner`, `/whois`, `/serverinfo`, `/botinfo`, `/roleinfo`, `/channelinfo`, `/icon`, `/usercount`, `/membercount`, `/invite`, `/emoji`, `/ping`, `/uptime`, `/say`, `/remind`, `/snipe`, `/editsnipe`, `/afk`",
                    inline=False)
    embed.add_field(name="🎲 Fun",
                    value="`/8ball`, `/coinflip`, `/roll`, `/dice`, `/choose`, `/reverse`, `/password`, `/rate`, `/ship`, `/rps`, `/truthordare`, `/joke`, `/quote`",
                    inline=False)
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

@bot.hybrid_command(name="banner", description="Shows a user's banner.")
async def banner(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    user = await bot.fetch_user(member.id)
    if not user.banner:
        await ctx.send(f"❌ {member.display_name} has no banner.")
        return
    embed = discord.Embed(title=f"{member.display_name}'s Banner", color=discord.Color.purple())
    embed.set_image(url=user.banner.url)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="usercount", description="Shows total server members.")
@commands.guild_only()
async def usercount(ctx: commands.Context):
    await ctx.send(f"👥 **{ctx.guild.name}** currently has **{ctx.guild.member_count}** members!")

@bot.hybrid_command(name="membercount", description="Shows a breakdown of members.")
@commands.guild_only()
async def membercount(ctx: commands.Context):
    guild = ctx.guild
    humans = sum(1 for m in guild.members if not m.bot)
    bots = guild.member_count - humans
    online = sum(1 for m in guild.members if m.status != discord.Status.offline)
    embed = discord.Embed(title=f"👥 {guild.name} Members", color=discord.Color.green())
    embed.add_field(name="Total", value=guild.member_count, inline=True)
    embed.add_field(name="Humans", value=humans, inline=True)
    embed.add_field(name="Bots", value=bots, inline=True)
    embed.add_field(name="Online", value=online, inline=True)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="say", description="Makes the bot broadcast a message.")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def say(ctx: commands.Context, *, message: str):
    await ctx.send(message, allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="ping", description="Checks the bot's latency.")
async def ping(ctx: commands.Context):
    latency = round(bot.latency * 1000)
    await ctx.send(f"Pong! 🏓 Latency: **{latency}ms**", ephemeral=True)

@bot.hybrid_command(name="uptime", description="Shows how long the bot has been online.")
async def uptime(ctx: commands.Context):
    if not hasattr(bot, "start_time"):
        bot.start_time = time.time()
    delta = int(time.time() - bot.start_time)
    d, rem = divmod(delta, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    await ctx.send(f"⏱️ Uptime: **{d}d {h}h {m}m {s}s**")

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
    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    if roles:
        embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles)[:1024], inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="serverinfo", description="Displays info about this server.")
@commands.guild_only()
async def serverinfo(ctx: commands.Context):
    guild = ctx.guild
    embed = discord.Embed(title=f"Server Info - {guild.name}", color=discord.Color.blurple())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=str(guild.owner), inline=True)
    embed.add_field(name="Members", value=guild.member_count, inline=True)
    embed.add_field(name="Channels", value=len(guild.channels), inline=True)
    embed.add_field(name="Roles", value=len(guild.roles), inline=True)
    embed.add_field(name="Boosts", value=guild.premium_subscription_count, inline=True)
    embed.add_field(name="Created", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="botinfo", description="Shows information about the bot.")
async def botinfo(ctx: commands.Context):
    embed = discord.Embed(title="🤖 Bot Info", color=discord.Color.blurple())
    embed.add_field(name="Name", value=str(bot.user), inline=True)
    embed.add_field(name="ID", value=bot.user.id, inline=True)
    embed.add_field(name="Servers", value=len(bot.guilds), inline=True)
    embed.add_field(name="Users", value=sum(g.member_count or 0 for g in bot.guilds), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="discord.py", value=discord.__version__, inline=True)
    if bot.user.avatar:
        embed.set_thumbnail(url=bot.user.avatar.url)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="roleinfo", description="Shows information about a role.")
@commands.guild_only()
async def roleinfo(ctx: commands.Context, role: discord.Role):
    embed = discord.Embed(title=f"Role Info - {role.name}", color=role.color or discord.Color.default())
    embed.add_field(name="ID", value=role.id, inline=True)
    embed.add_field(name="Color", value=str(role.color), inline=True)
    embed.add_field(name="Members", value=len(role.members), inline=True)
    embed.add_field(name="Mentionable", value=role.mentionable, inline=True)
    embed.add_field(name="Hoisted", value=role.hoist, inline=True)
    embed.add_field(name="Position", value=role.position, inline=True)
    embed.add_field(name="Created", value=role.created_at.strftime("%Y-%m-%d"), inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="channelinfo", description="Shows information about a channel.")
@commands.guild_only()
async def channelinfo(ctx: commands.Context, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    embed = discord.Embed(title=f"Channel Info - #{channel.name}", color=discord.Color.blurple())
    embed.add_field(name="ID", value=channel.id, inline=True)
    embed.add_field(name="Type", value=str(channel.type), inline=True)
    embed.add_field(name="Topic", value=(channel.topic or "None")[:1024], inline=False)
    embed.add_field(name="Slowmode", value=f"{channel.slowmode_delay}s", inline=True)
    embed.add_field(name="NSFW", value=channel.is_nsfw(), inline=True)
    embed.add_field(name="Created", value=channel.created_at.strftime("%Y-%m-%d"), inline=True)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="icon", description="Shows the server icon.")
@commands.guild_only()
async def icon(ctx: commands.Context):
    if not ctx.guild.icon:
        await ctx.send("❌ This server has no icon.")
        return
    embed = discord.Embed(title=f"{ctx.guild.name}'s Icon", color=discord.Color.blurple())
    embed.set_image(url=ctx.guild.icon.url)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="invite", description="Get the bot's invite link.")
async def invite(ctx: commands.Context):
    perms = discord.Permissions(administrator=True)
    url = discord.utils.oauth_url(bot.user.id, permissions=perms)
    await ctx.send(f"🔗 Invite me: {url}")

@bot.hybrid_command(name="emoji", description="Shows an emoji enlarged.")
async def emoji(ctx: commands.Context, emoji: str):
    try:
        e = await commands.EmojiConverter().convert(ctx, emoji)
        embed = discord.Embed(title=f"Emoji: {e.name}", color=discord.Color.gold())
        embed.set_image(url=e.url)
        await ctx.send(embed=embed)
    except commands.BadArgument:
        await ctx.send("❌ That's not a valid custom emoji.")

@bot.hybrid_command(name="remind", description="Set a reminder in N minutes.")
async def remind(ctx: commands.Context, minutes: int, *, text: str):
    if not 1 <= minutes <= 1440:
        await ctx.send("❌ Minutes must be 1–1440.", ephemeral=True)
        return
    await ctx.send(f"⏰ Reminder set for **{minutes} min** from now.", ephemeral=True)
    await asyncio.sleep(minutes * 60)
    try:
        await ctx.author.send(f"⏰ Reminder: **{text}**")
    except discord.Forbidden:
        await ctx.send(f"{ctx.author.mention} ⏰ Reminder: **{text}**", allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="snipe", description="Show the last deleted message in this channel.")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def snipe(ctx: commands.Context):
    data = sniped.get(ctx.channel.id)
    if not data:
        await ctx.send("❌ Nothing to snipe.", ephemeral=True)
        return
    embed = discord.Embed(description=data["content"] or "*[no text]*", color=discord.Color.orange(),
                          timestamp=data["time"])
    embed.set_author(name=str(data["author"]), icon_url=data["author"].display_avatar.url)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="editsnipe", description="Show the last edited message.")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def editsnipe(ctx: commands.Context):
    data = edited.get(ctx.channel.id)
    if not data:
        await ctx.send("❌ Nothing to show.", ephemeral=True)
        return
    embed = discord.Embed(color=discord.Color.blue())
    embed.add_field(name="Before", value=data["before"] or "*[empty]*", inline=False)
    embed.add_field(name="After", value=data["after"] or "*[empty]*", inline=False)
    embed.set_author(name=str(data["author"]), icon_url=data["author"].display_avatar.url)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="afk", description="Set an AFK status.")
@commands.guild_only()
async def afk(ctx: commands.Context, *, reason: str = "AFK"):
    afk_users[ctx.author.id] = reason
    await ctx.send(f"💤 {ctx.author.display_name} is now AFK: {reason}", allowed_mentions=NO_MENTIONS)

# ==========================================
# 9. FUN COMMANDS
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

@bot.hybrid_command(name="dice", description="Roll multiple dice (e.g. 3d6).")
async def dice(ctx: commands.Context, notation: str = "1d6"):
    m = re.match(r"^(\d{1,2})d(\d{1,4})$", notation.lower())
    if not m:
        await ctx.send("❌ Use format like `2d6` or `1d20`.", ephemeral=True)
        return
    count, sides = int(m.group(1)), int(m.group(2))
    if count < 1 or count > 20 or sides < 2 or sides > 1000:
        await ctx.send("❌ Max 20 dice, sides 2–1000.", ephemeral=True)
        return
    rolls = [random.randint(1, sides) for _ in range(count)]
    await ctx.send(f"🎲 `{notation}` → {rolls} (total: **{sum(rolls)}**)")

@bot.hybrid_command(name="choose", description="Let the bot pick between options.")
async def choose(ctx: commands.Context, *, options: str):
    choices = [o.strip() for o in options.split("|") if o.strip()]
    if len(choices) < 2:
        await ctx.send("❌ Separate options with `|` (e.g. `pizza | sushi`).", ephemeral=True)
        return
    await ctx.send(f"🤔 I choose: **{random.choice(choices)}**")

@bot.hybrid_command(name="reverse", description="Reverses your text.")
async def reverse(ctx: commands.Context, *, text: str):
    await ctx.send(text[::-1], allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="password", description="Generate a random password.")
async def password(ctx: commands.Context, length: int = 16):
    length = max(8, min(length, 64))
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*"
    pwd = "".join(secrets.choice(alphabet) for _ in range(length))
    try:
        await ctx.author.send(f"🔐 Your password ({length} chars): `{pwd}`")
        await ctx.send("📩 Sent to your DMs!", ephemeral=True)
    except discord.Forbidden:
        await ctx.send("❌ Enable DMs so I can send it.", ephemeral=True)

@bot.hybrid_command(name="rate", description="Rates something 0–10.")
async def rate(ctx: commands.Context, *, thing: str):
    await ctx.send(f"⭐ I rate **{thing}** a **{random.randint(0, 10)}/10**!")

@bot.hybrid_command(name="ship", description="Ship two users.")
async def ship(ctx: commands.Context, user1: discord.Member, user2: discord.Member = None):
    user2 = user2 or ctx.author
    score = random.randint(0, 100)
    bar = "█" * (score // 10) + "░" * (10 - score // 10)
    await ctx.send(f"💘 **{user1.display_name}** + **{user2.display_name}**\n`{bar}` **{score}%**",
                   allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="rps", description="Rock, paper, scissors.")
async def rps(ctx: commands.Context, choice: str):
    choice = choice.lower()
    if choice not in ("rock", "paper", "scissors"):
        await ctx.send("❌ Choose rock, paper, or scissors.", ephemeral=True)
        return
    bot_choice = random.choice(["rock", "paper", "scissors"])
    wins = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    if choice == bot_choice:
        result = "🤝 Tie!"
    elif wins[choice] == bot_choice:
        result = "🎉 You win!"
    else:
        result = "🤖 I win!"
    await ctx.send(f"You: **{choice}** | Me: **{bot_choice}**\n{result}")

@bot.hybrid_command(name="truthordare", description="Random truth or dare prompt.")
async def truthordare(ctx: commands.Context):
    truths = ["What's your most embarrassing moment?", "What's a secret you've never told anyone?",
              "Who was your first crush?", "What's the biggest lie you've told?"]
    dares = ["Send the last photo in your camera roll.", "Text your crush 'hi'.",
             "Do 20 push-ups.", "Speak only in rhymes for 5 minutes."]
    kind = random.choice(["truth", "dare"])
    prompt = random.choice(truths if kind == "truth" else dares)
    await ctx.send(f"🎭 **{kind.upper()}**: {prompt}", allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="joke", description="Tells a random joke.")
async def joke(ctx: commands.Context):
    jokes = [
        "Why don't scientists trust atoms? Because they make up everything.",
        "I told my computer I needed a break, and it said 'no problem — I'll go to sleep.'",
        "Why did the scarecrow win an award? He was outstanding in his field.",
        "Parallel lines have so much in common. It's a shame they'll never meet.",
        "I'm reading a book about anti-gravity. It's impossible to put down.",
    ]
    await ctx.send(f"😂 {random.choice(jokes)}")

@bot.hybrid_command(name="quote", description="Sends a random inspirational quote.")
async def quote(ctx: commands.Context):
    quotes = [
        "The only way to do great work is to love what you do. — Steve Jobs",
        "In the middle of difficulty lies opportunity. — Albert Einstein",
        "It always seems impossible until it's done. — Nelson Mandela",
        "The best time to plant a tree was 20 years ago. The second best time is now.",
        "Don't watch the clock; do what it does. Keep going. — Sam Levenson",
    ]
    await ctx.send(f"💭 {random.choice(quotes)}")

# ==========================================
# 10. MODERATION COMMANDS
# ==========================================
@bot.hybrid_command(name="warn", description="Issues a warning to a member.")
@commands.guild_only()
@commands.has_permissions(kick_members=True)
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided."):
    warnings.setdefault(ctx.guild.id, {}).setdefault(member.id, []).append({
        "reason": reason,
        "moderator": str(ctx.author),
        "time": datetime.now(timezone.utc).isoformat(),
    })
    count = len(warnings[ctx.guild.id][member.id])
    embed = discord.Embed(
        title="⚠️ Warning Issued",
        description=f"**{member.display_name}** has been warned. (Total: {count})",
        color=discord.Color.orange()
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)
    try:
        await member.send(f"⚠️ You were warned in **{ctx.guild.name}**: {reason}")
    except discord.Forbidden:
        pass

@bot.hybrid_command(name="warnings", description="Lists a member's warnings.")
@commands.guild_only()
@commands.has_permissions(kick_members=True)
async def warnings_cmd(ctx: commands.Context, member: discord.Member):
    entries = warnings.get(ctx.guild.id, {}).get(member.id, [])
    if not entries:
        await ctx.send(f"✅ **{member.display_name}** has no warnings.")
        return
    lines = [f"**{i+1}.** {w['reason']} — *by {w['moderator']}*" for i, w in enumerate(entries)]
    embed = discord.Embed(title=f"⚠️ Warnings for {member.display_name}",
                          description="\n".join(lines)[:4000], color=discord.Color.orange())
    await ctx.send(embed=embed)

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

@bot.hybrid_command(name="softban", description="Bans then immediately unbans a member (clears messages).")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
async def softban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided."):
    if not can_act_on(ctx, member):
        await ctx.send(HIERARCHY_MSG, ephemeral=True)
        return
    await member.ban(reason=reason, delete_message_days=7)
    await ctx.guild.unban(member, reason="Softban")
    await ctx.send(f"🔨 Softbanned **{member.display_name}** | Reason: {reason}", allowed_mentions=NO_MENTIONS)

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

@bot.hybrid_command(name="banlist", description="Lists all banned users.")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
async def banlist(ctx: commands.Context):
    bans = [entry async for entry in ctx.guild.bans(limit=50)]
    if not bans:
        await ctx.send("✅ No banned users.")
        return
    lines = [f"**{e.user}** (`{e.user.id}`) — {e.reason or 'No reason'}" for e in bans]
    embed = discord.Embed(title=f"🔨 Bans ({len(bans)})", description="\n".join(lines)[:4000],
                          color=discord.Color.red())
    await ctx.send(embed=embed)

@bot.hybrid_command(name="mute", description="Timeouts/mutes a member for a specified number of minutes.")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
async def mute(ctx: commands.Context, member: discord.Member, minutes: int, *, reason: str = "No reason provided."):
    if not 1 <= minutes <= 40320:
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
    if not can_act_on(ctx, member):
        await ctx.send(HIERARCHY_MSG, ephemeral=True)
        return
    await member.timeout(None)
    await ctx.send(f"🔊 Unmuted **{member.display_name}**.", allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="clear", description="Deletes a specified number of messages.")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def clear(ctx: commands.Context, amount: int = 5):
    amount = max(1, min(amount, 100))
    if ctx.interaction is None:
        await ctx.channel.purge(limit=amount + 1)
        await ctx.send(f"🧹 Deleted {amount} messages.", delete_after=3)
    else:
        await ctx.defer(ephemeral=True)
        await ctx.channel.purge(limit=amount)
        await ctx.send(f"🧹 Deleted {amount} messages.", ephemeral=True, delete_after=3)

@bot.hybrid_command(name="purgeuser", description="Delete recent messages from a specific user.")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def purgeuser(ctx: commands.Context, member: discord.Member, amount: int = 20):
    amount = max(1, min(amount, 100))
    deleted = await ctx.channel.purge(limit=amount * 3,
                                      check=lambda m: m.author.id == member.id, bulk=True)
    await ctx.send(f"🧹 Deleted **{len(deleted)}** messages from {member.mention}.",
                   delete_after=3, allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="nuke", description="Clone and delete the current channel (full reset).")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def nuke(ctx: commands.Context):
    channel = ctx.channel
    new = await channel.clone(reason=f"Nuked by {ctx.author}")
    await new.edit(position=channel.position)
    await channel.delete(reason=f"Nuked by {ctx.author}")
    await new.send(f"💥 Channel nuked by {ctx.author.mention}.", allowed_mentions=NO_MENTIONS)

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
    seconds = max(0, min(seconds, 21600))
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(f"⏱️ Slowmode set to {seconds} seconds.")

@bot.hybrid_command(name="nick", description="Change a member's nickname.")
@commands.guild_only()
@commands.has_permissions(manage_nicknames=True)
async def nick(ctx: commands.Context, member: discord.Member, *, nickname: str = None):
    if not can_act_on(ctx, member):
        await ctx.send(HIERARCHY_MSG, ephemeral=True)
        return
    await member.edit(nick=nickname)
    await ctx.send(f"✅ Nickname for **{member.display_name}** set to `{nickname or 'reset'}`.",
                   allowed_mentions=NO_MENTIONS)

@bot.hybrid_command(name="role", description="Add or remove a role from a member.")
@commands.guild_only()
@commands.has_permissions(manage_roles=True)
async def role(ctx: commands.Context, member: discord.Member, role: discord.Role, action: str = "add"):
    if action.lower() not in ("add", "remove"):
        await ctx.send("❌ Action must be `add` or `remove`.", ephemeral=True)
        return
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ That role is above mine.", ephemeral=True)
        return
    if action.lower() == "add":
        await member.add_roles(role)
        await ctx.send(f"✅ Added **{role.name}** to **{member.display_name}**.", allowed_mentions=NO_MENTIONS)
    else:
        await member.remove_roles(role)
        await ctx.send(f"✅ Removed **{role.name}** from **{member.display_name}**.", allowed_mentions=NO_MENTIONS)

# ==========================================
# 11. LINK-DISPENSER PANEL
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
# 12. CLOUDFLARE PAGES LINK GENERATOR (/makelink)
# ==========================================
BASE_DIR = Path(__file__).resolve().parent
SITE_DIR = BASE_DIR / "site-src"
WRANGLER_BIN = BASE_DIR / "node_modules" / ".bin" / "wrangler"
SITE_SKIP = shutil.ignore_patterns(
    ".git", ".gitignore", "CNAME", "README.md", "build-links.js", "supabase-*.sql"
)
PAGES_URL_RE = re.compile(r"https://[a-z0-9-]+\.[a-z0-9-]+\.pages\.dev")
KEEP_DEPLOYMENTS = 20
_background_tasks = set()

async def cleanup_old_deployments():
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
            for d in deployments[KEEP_DEPLOYMENTS:]:
                async with session.delete(f"{base}/{d['id']}", params={"force": "true"}) as r:
                    if r.status >= 400:
                        print(f"cleanup: could not delete {d['id']}: {r.status}")
    except Exception as e:
        print(f"cleanup failed: {e!r}")

async def deploy_site() -> str:
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    project = os.environ.get("CF_PAGES_PROJECT")
    if not (token and account and project):
        raise RuntimeError("CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID / CF_PAGES_PROJECT not set")
    if not (SITE_DIR / "index.html").exists():
        raise RuntimeError("site-src is empty (run: git submodule update --init --recursive)")

    wrangler = str(WRANGLER_BIN) if WRANGLER_BIN.exists() else "wrangler"
    tmp = tempfile.mkdtemp(prefix="ubg-")
    url = None
    try:
        site_copy = Path(tmp) / "site"
        await asyncio.to_thread(shutil.copytree, SITE_DIR, site_copy, ignore=SITE_SKIP)

        proc = await asyncio.create_subprocess_exec(
            wrangler, "pages", "deploy", str(site_copy),
            "--project-name", project, "--branch", "main", "--commit-dirty=true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "WRANGLER_SEND_METRICS": "false"},
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
        url = match.group(0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    task = asyncio.create_task(cleanup_old_deployments())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return url

SURGE_BIN = BASE_DIR / "node_modules" / ".bin" / "surge"

async def deploy_surge_site() -> str:
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
            surge, str(site_copy), domain,
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
    token = os.environ.get("VERCEL_TOKEN")
    org_id = os.environ.get("VERCEL_ORG_ID")
    project_id = os.environ.get("VERCEL_PROJECT_ID")
    if not (token and org_id and project_id):
        raise RuntimeError("VERCEL_TOKEN / VERCEL_ORG_ID / VERCEL_PROJECT_ID not set")
    if not (SITE_DIR / "index.html").exists():
        raise RuntimeError("site-src is empty (run: git submodule update --init --recursive)")

    vercel = str(VERCEL_BIN) if VERCEL_BIN.exists() else "vercel"
    tmp = tempfile.mkdtemp(prefix="ubg-")
    url = None
    try:
        site_copy = Path(tmp) / "site"
        await asyncio.to_thread(shutil.copytree, SITE_DIR, site_copy, ignore=SITE_SKIP)

        proc = await asyncio.create_subprocess_exec(
            vercel, "deploy", str(site_copy), "--yes",
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
        match = VERCEL_URL_RE.search(out_text) or VERCEL_URL_RE.search(err_text)
        if not match:
            raise RuntimeError(f"no URL found in vercel output: {(out_text + err_text)[-300:]}")
        url = match.group(0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return url

HOSTS = {
    "cloudflare": ("☁️ Cloudflare", deploy_site),
    "surge": ("⚡ Surge", deploy_surge_site),
    "vercel": ("🔺 Vercel", deploy_vercel_site),
}

async def generate_links(kinds: list[str], show_errors: bool = False) -> list[str]:
    results = await asyncio.gather(*(HOSTS[k][1]() for k in kinds), return_exceptions=True)
    lines = []
    for k, res in zip(kinds, results):
        label = HOSTS[k][0]
        if isinstance(res, Exception):
            print(f"{k} deploy failed: {res!r}", flush=True)
            detail = ""
            if show_errors:
                reason = (str(res) or repr(res))[-600:].replace("`", "'")
                detail = f"\n```{type(res).__name__}: {reason}```"
            lines.append(f"{label}: ❌ failed{detail}")
        else:
            lines.append(f"{label}: {res}")
    return lines

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
    def __init__(self):
        super().__init__(timeout=120)

    async def _run(self, interaction: discord.Interaction, kinds: list[str]):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="⏳ Generating your link...", view=self)
        lines = await generate_links(kinds, show_errors=True)
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
@commands.cooldown(1, 30, commands.BucketType.user)
async def makelink(ctx: commands.Context):
    if not LINK_PASSWORD:
        await ctx.send("❌ No password is set on the bot (LINK_PASSWORD).", ephemeral=True)
        return
    if ctx.interaction is None:
        await ctx.send(SLASH_ONLY_MSG)
        return
    await ctx.interaction.response.send_modal(PasswordModal(makelink_after_password))

# ==========================================
# 13. AUTO LINK POSTER (/autolink)
# ==========================================
AUTOLINK_INTERVAL_MIN = 60
autolink_channel_id = int(os.environ.get("AUTOLINK_CHANNEL_ID", "0") or 0)
AUTOLINK_HOSTS = [h.strip() for h in os.environ.get("AUTOLINK_HOSTS", "cloudflare,surge,vercel").split(",") if h.strip() in HOSTS] or list(HOSTS)

@tasks.loop(minutes=AUTOLINK_INTERVAL_MIN)
async def autolink_loop():
    try:
        channel = bot.get_channel(autolink_channel_id)
        if channel is None:
            return
        lines = await generate_links(AUTOLINK_HOSTS)
        if all("❌ failed" in line for line in lines):
            print("autolink: all deploys failed, nothing posted")
            return
        await channel.send("🔗 **New links:**\n" + "\n".join(lines))
    except Exception as e:
        print(f"autolink_loop error: {e!r}")

@autolink_loop.before_loop
async def before_autolink_loop():
    await bot.wait_until_ready()

@bot.hybrid_group(name="autolink", description="Auto-post fresh links on a timer.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def autolink(ctx: commands.Context):
    if ctx.invoked_subcommand is None:
        await ctx.send("Use `/autolink start #channel` or `/autolink stop`.")

@autolink.command(name="start", description="Start posting a new link every 60 minutes in a channel.")
@commands.has_permissions(administrator=True)
async def autolink_start(ctx: commands.Context, channel: discord.TextChannel):
    if ctx.interaction is None:
        await ctx.send(SLASH_ONLY_MSG)
        return
    if not LINK_PASSWORD:
        await ctx.send("❌ No password is set on the bot (LINK_PASSWORD).", ephemeral=True)
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
# 14. ERROR HANDLING
# ==========================================
@bot.event
async def on_command_error(ctx: commands.Context, error):
    while getattr(error, "original", None) is not None:
        error = error.original

    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        msg = "❌ You do not have permission to execute this command."
    elif isinstance(error, commands.BotMissingPermissions):
        msg = "❌ I'm missing the permissions required for that."
    elif isinstance(error, commands.NoPrivateMessage):
        msg = "❌ This command only works inside a server."
    elif isinstance(error, commands.CommandOnCooldown):
        msg = f"⏳ Try again in {error.retry_after:.0f}s."
    elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        msg = "❌ Invalid or missing arguments. Check the command usage."
    elif isinstance(error, commands.CheckFailure):
        msg = "❌ You can't use that command here."
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
# 15. RUN BOT
# ==========================================
token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("Error: DISCORD_TOKEN environment variable not set.")