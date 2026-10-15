import os
import threading
import discord
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

# Start web server on a background thread
threading.Thread(target=run_web_server, daemon=True).start()

# ==========================================
# 2. DISCORD BOT SETUP
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    await bot.change_presence(activity=discord.Game(name="!help | Moderation & Links"))

# --- Welcome Message Event ---
@bot.event
async def on_member_join(member):
    channel = discord.utils.get(member.guild.text_channels, name="welcome")
    if channel:
        embed = discord.Embed(
            title=f"Welcome to the server, {member.name}!",
            description="We're glad to have you here. Check out the link panel for quick resources!",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)

# ==========================================
# 3. MODERATION COMMANDS
# ==========================================

@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason: str = "No reason provided."):
    await member.kick(reason=reason)
    await ctx.send(f"✅ Kicked **{member.display_name}** | Reason: {reason}")

@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason: str = "No reason provided."):
    await member.ban(reason=reason)
    await ctx.send(f"⛔ Banned **{member.display_name}** | Reason: {reason}")

@bot.command(name="clear")
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int = 5):
    await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"🧹 Deleted {amount} messages.", delete_after=3)

# ==========================================
# 4. LINK-DISPENSER PANEL
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
            await interaction.user.send(" Here is your requested link: https://example.com/secret")
            await interaction.response.send_message("Check your Direct Messages!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I couldn't DM you! Please enable DMs from server members.", ephemeral=True)

@bot.command(name="panel")
@commands.has_permissions(administrator=True)
async def send_panel(ctx):
    embed = discord.Embed(
        title="🔗 Server Link Dispenser",
        description="Click the buttons below to open our official links or request private resources sent to your DMs!",
        color=discord.Color.green()
    )
    view = LinkDispenserView()
    await ctx.send(embed=embed, view=view)

@kick.error
@ban.error
@clear.error
@send_panel.error
async def permissions_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You do not have the required permissions to use this command.")

# ==========================================
# 5. RUN BOT
# ==========================================
token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("Error: DISCORD_TOKEN environment variable not set.")
