# main.py
import os, json, asyncio
import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask
from threading import Thread

# ---------- EDIT THESE ----------
OWNER_ID = 1319292111325106296          # you
GUILD_ID = 123456789012345678           # your server ID
TOKEN    = os.getenv("DISCORD_BOT_TOKEN")
# ---------------------------------

# ---------- Flask keep-alive (Render) ----------
app = Flask(__name__)
@app.route("/")
def home(): return "✅ ModMail bot running"
def _run(): app.run(host="0.0.0.0", port=10000)
Thread(target=_run, daemon=True).start()
# ----------------------------------------------

# ---------- Discord setup ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.dm_messages = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

CONFIG_PATH = "config.json"
DEFAULT_CFG = {"category_id": None, "staff_role_id": None, "log_channel_id": None, "tickets": {}}  # tickets: user_id -> channel_id

def load_cfg():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f: json.dump(DEFAULT_CFG, f, indent=4)
    with open(CONFIG_PATH, "r") as f: return json.load(f)

def save_cfg(cfg):
    with open(CONFIG_PATH, "w") as f: json.dump(cfg, f, indent=4)

cfg = load_cfg()

# ---------- helpers ----------
async def log(guild: discord.Guild, text: str):
    if not cfg.get("log_channel_id"): return
    ch = guild.get_channel(cfg["log_channel_id"])
    if ch: await ch.send(f"📋 {text}")

def staff_can(user: discord.Member) -> bool:
    if user.id == OWNER_ID: return True
    role_id = cfg.get("staff_role_id")
    return bool(role_id and user.get_role(role_id))

def ticket_embed(author: discord.abc.User, content: str, *, is_staff: bool) -> discord.Embed:
    color = discord.Color.blurple() if not is_staff else discord.Color.orange()
    e = discord.Embed(description=content or "‎", color=color)
    e.set_author(name=str(author), icon_url=author.display_avatar.url)
    return e

async def create_ticket(guild: discord.Guild, user: discord.User) -> discord.TextChannel | None:
    """Create a private ticket channel and store mapping."""
    category = guild.get_channel(cfg.get("category_id") or 0)
    staff_role = guild.get_role(cfg.get("staff_role_id") or 0)
    if not category or not isinstance(category, discord.CategoryChannel) or not staff_role:
        return None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        # user gets access too (so they can type in-channel if you want)
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    ch = await guild.create_text_channel(
        name=f"ticket-{user.name}".replace(" ", "-")[:90],
        category=category,
        overwrites=overwrites,
        topic=str(user.id)  # store user id in topic
    )
    cfg["tickets"][str(user.id)] = ch.id
    save_cfg(cfg)
    await ch.send(f"{staff_role.mention} 📬 **New ticket opened by {user.mention}**")
    await log(guild, f"Ticket opened for {user} → {ch.mention}")
    return ch

# ---------- views (buttons) ----------
class ConfirmDMView(discord.ui.View):
    def __init__(self, user_id: int, initial_msg: discord.Message, guild: discord.Guild):
        super().__init__(timeout=15)
        self.user_id = user_id
        self.initial_msg = initial_msg
        self.guild = guild
        self.confirmed = asyncio.Event()

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This confirmation isn't for you.", ephemeral=True)
        # create ticket
        ch = await create_ticket(self.guild, interaction.user)
        if not ch:
            return await interaction.response.send_message("⚠️ ModMail not set up. Please try later.", ephemeral=True)
        await interaction.response.send_message("✅ Ticket created. Your message will be forwarded.", ephemeral=True)

        # forward the original DM as first message
        emb = ticket_embed(interaction.user, self.initial_msg.content, is_staff=False)
        files = [await a.to_file() for a in self.initial_msg.attachments]
        await ch.send(embed=emb, files=files)
        try:
            await interaction.user.send("📨 Your message was sent to staff.")
        except: pass
        self.confirmed.set()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This cancellation isn't for you.", ephemeral=True)
        await interaction.response.send_message("❎ Cancelled.", ephemeral=True)
        self.confirmed.set()
        self.stop()

    async def on_timeout(self):
        if not self.confirmed.is_set():
            try:
                user = self.initial_msg.author
                await user.send("⌛ Ticket creation timed out. Send another message to try again.")
            except: pass

class TicketButtonView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="🎟 Create Ticket", style=discord.ButtonStyle.green, custom_id="ticket:create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        # if they already have a ticket, just tell them
        existing_id = cfg["tickets"].get(str(interaction.user.id))
        if existing_id:
            ch = interaction.guild.get_channel(existing_id)
            if ch: return await interaction.response.send_message(f"You already have a ticket: {ch.mention}", ephemeral=True)
        ch = await create_ticket(interaction.guild, interaction.user)
        if not ch:
            return await interaction.response.send_message("⚠️ ModMail not set up yet.", ephemeral=True)
        await interaction.response.send_message(f"✅ Ticket created: {ch.mention}", ephemeral=True)
        try:
            await interaction.user.send("📨 Ticket created. You can DM me and staff will see it.")
        except: pass

# ---------- slash commands ----------
@bot.event
async def on_ready():
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    except Exception as e:
        print("Sync error:", e)
    print(f"✅ Logged in as {bot.user}")

# owner-only guards
def owner_only():
    async def predicate(inter: discord.Interaction):
        if inter.user.id != OWNER_ID:
            await inter.response.send_message("❌ Owner only.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

@bot.tree.command(name="set_category", description="Owner: set the ticket category", guild=discord.Object(id=GUILD_ID))
@owner_only()
async def set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    cfg["category_id"] = category.id; save_cfg(cfg)
    await interaction.response.send_message(f"✅ Category set to **{category.name}**", ephemeral=True)

@bot.tree.command(name="set_staff_role", description="Owner: set the staff role", guild=discord.Object(id=GUILD_ID))
@owner_only()
async def set_staff_role(interaction: discord.Interaction, role: discord.Role):
    cfg["staff_role_id"] = role.id; save_cfg(cfg)
    await interaction.response.send_message(f"✅ Staff role set to {role.mention}", ephemeral=True)

@bot.tree.command(name="set_logchannel", description="Owner: set the log channel", guild=discord.Object(id=GUILD_ID))
@owner_only()
async def set_logchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg["log_channel_id"] = channel.id; save_cfg(cfg)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="send_ticket_button", description="Owner: send a 'Create Ticket' button here", guild=discord.Object(id=GUILD_ID))
@owner_only()
async def send_ticket_button(interaction: discord.Interaction):
    await interaction.channel.send("Need help? Click to open a ticket:", view=TicketButtonView())
    await interaction.response.send_message("✅ Button sent.", ephemeral=True)

@bot.tree.command(name="modmail", description="Open a ModMail ticket", guild=discord.Object(id=GUILD_ID))
async def modmail_cmd(interaction: discord.Interaction):
    # if exists, return link
    existing_id = cfg["tickets"].get(str(interaction.user.id))
    if existing_id:
        ch = interaction.guild.get_channel(existing_id)
        if ch:
            return await interaction.response.send_message(f"You already have a ticket: {ch.mention}", ephemeral=True)
    ch = await create_ticket(interaction.guild, interaction.user)
    if not ch:
        return await interaction.response.send_message("⚠️ ModMail not set up yet.", ephemeral=True)
    await interaction.response.send_message(f"✅ Ticket created: {ch.mention}", ephemeral=True)
    try:
        await interaction.user.send("📨 Ticket created. DM me your message and staff will see it.")
    except: pass

@bot.tree.command(name="close", description="Close this ticket", guild=discord.Object(id=GUILD_ID))
async def close_ticket(interaction: discord.Interaction):
    if not staff_can(interaction.user):
        return await interaction.response.send_message("❌ Only staff can close tickets.", ephemeral=True)
    # validate ticket
    topic_uid = interaction.channel.topic
    if not topic_uid or interaction.channel.category_id != cfg.get("category_id"):
        return await interaction.response.send_message("⚠️ This is not a ticket channel.", ephemeral=True)

    user = await bot.fetch_user(int(topic_uid))
    try: await user.send("✅ Your ticket was closed. Thanks!")
    except: pass

    await log(interaction.guild, f"Ticket closed for {user} by {interaction.user}")
    # remove mapping
    cfg["tickets"].pop(str(user.id), None); save_cfg(cfg)
    await interaction.response.send_message("🗂️ Closing…", ephemeral=True)
    await asyncio.sleep(1)
    await interaction.channel.delete()

# ---------- message routing ----------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # 1) DMs → (confirm) → ticket
    if isinstance(message.channel, discord.DMChannel):
        guild = bot.get_guild(GUILD_ID)
        if not guild: return

        # existing ticket: forward directly
        existing_id = cfg["tickets"].get(str(message.author.id))
        if existing_id:
            ch = guild.get_channel(existing_id)
            if ch:
                emb = ticket_embed(message.author, message.content, is_staff=False)
                files = [await a.to_file() for a in message.attachments]
                await ch.send(embed=emb, files=files)
                await log(guild, f"DM → Ticket from {message.author}")
            return

        # no ticket yet → ask to confirm (15s)
        view = ConfirmDMView(message.author.id, message, guild)
        try:
            await message.author.send("Do you want to open a support ticket with staff? (15s)",
                                      view=view)
        except: return
        # Let the view manage timeout/creation. Nothing else to do here.
        return

    # 2) Ticket channel → DM
    if message.guild and message.channel.category_id == cfg.get("category_id"):
        # ensure it's a ticket: topic holds user id
        if not message.channel.topic: return
        user_id = int(message.channel.topic)
        user = await bot.fetch_user(user_id)

        # forward only if author is staff/owner (prevent echo loops from user's own channel messages)
        if isinstance(message.author, discord.Member) and not staff_can(message.author):
            return

        emb = ticket_embed(message.author, message.content, is_staff=True)
        files = [await a.to_file() for a in message.attachments]
        try:
            await user.send(embed=emb, files=files)
            await log(message.guild, f"Ticket → DM from {message.author} to {user}")
        except:
            await message.channel.send("⚠️ Could not DM the user.")

    await bot.process_commands(message)

# ---------- run ----------
bot.run(TOKEN)
