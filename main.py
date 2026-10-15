import os
import threading
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View
from flask import Flask

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
# 2. DISCORD BOT SETUP
# ==========================================
class CustomBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Sync globally as well
        try:
            synced = await self.tree.sync()
            print(f"✅ Synced {len(synced)} global commands.")
        except Exception as e:
            print(f"Sync error: {e}")

bot = CustomBot()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    await bot.change_presence(activity=discord.Game(name="!cmds | Carl-Style"))

@bot.event
async def on_member_join(member):
    channel = discord.utils.get(member.guild.text_channels, name="welcome")
    if channel:
        embed = discord.Embed(
            title=f"Welcome to the server, {member.name}!",
            description="Type `!cmds` or `/cmds` to see all available commands.",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)

# ==========================================
# 3. INSTANT SYNC COMMAND (Prefix command)
# ==========================================
@bot.command(name="sync")
@commands.has_permissions(administrator=True)
async def sync_commands(ctx: commands.Context):
    """Forces instant command syncing to the current server."""
    bot.tree.copy_global_to(guild=ctx.guild)
    synced = await bot.tree.sync(guild=ctx.guild)
    await ctx.send(f"✅ Successfully synced **{len(synced)}** commands instantly to this server!")

# ==========================================
# 4. HELP & UTILITY COMMANDS
# ==========================================

@bot.hybrid_command(name="cmds", description="Lists all available bot commands.")
async def list_commands(ctx: commands.Context):
    embed = discord.Embed(
        title="📜 Carl-Style Bot Commands",
        description="Here are all the active commands loaded on your bot:",
        color=discord.Color.gold()
    )
    embed.add_field(
        name="🛡️ Moderation",
        value="`/kick`, `/ban`, `/mute` (timeout), `/unmute`, `/clear`, `/lock`, `/unlock`, `/slowmode`",
        inline=False
    )
    embed.add_field(
        name="🛠️ Utilities & UI",
        value="`/panel` (link dispenser), `/ping`, `/whois`, `/serverinfo`, `/cmds`, `!sync`",
        inline=False
    )
    await ctx.send(embed=embed)

@bot.hybrid_command(name="ping", description="Checks the bot's latency.")
async def ping(ctx: commands.Context):
    latency = round(bot.latency * 1000)
    await ctx.send(f"Pong! 🏓 Latency: **{latency}ms**", ephemeral=True)

@bot.hybrid_command(name="whois", description="Displays info about a user.")
async def whois(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"User Info - {member.name}", color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=member.id, inline=True)
    embed.add_field(name="Joined Server", value=member.joined_at.strftime("%Y-%m-%d"), inline=True)
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
# 5. ADVANCED MODERATION COMMANDS
# ==========================================

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

@bot.hybrid_command(name="mute", description="Timeouts/mutes a member for a specified number of minutes.")
@commands.has_permissions(moderate_members=True)
async def mute(ctx: commands.Context, member: discord.Member, minutes: int, *, reason: str = "No reason provided."):
    from datetime import timedelta
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
# 6. LINK-DISPENSER PANEL
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

# Error handling
@sync_commands.error
@kick.error
@ban.error
@mute.error
@unmute.error
@clear.error
@lock.error
@unlock.error
@slowmode.error
@send_panel.error
async def permissions_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You do not have permission to execute this command.", ephemeral=True)

# ==========================================
# 7. RUN BOT
# ==========================================
token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("Error: DISCORD_TOKEN environment variable not set.")
