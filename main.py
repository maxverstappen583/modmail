# modmail_bot.py
import os
import json
import discord
from discord.ext import commands, tasks
from discord import ui
from flask import Flask
from threading import Thread
from datetime import datetime, timezone
import asyncio

# Optional OpenAI usage (only works if OPENAI_API_KEY is set)
try:
    import openai
except Exception:
    openai = None

# --------- ENV ----------
from dotenv import load_dotenv
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY") or None
PRIMARY_GUILD_ID = os.getenv("PRIMARY_GUILD_ID")  # string -> cast later
OWNER_ID = os.getenv("OWNER_ID")  # string

# quick validation
if not DISCORD_TOKEN:
    raise SystemExit("❌ DISCORD_BOT_TOKEN is not set in environment. Exiting.")
if not PRIMARY_GUILD_ID:
    raise SystemExit("❌ PRIMARY_GUILD_ID is not set. Exiting.")
if not OWNER_ID:
    raise SystemExit("❌ OWNER_ID is not set. Exiting.")
try:
    PRIMARY_GUILD_ID = int(PRIMARY_GUILD_ID)
    OWNER_ID = int(OWNER_ID)
except Exception:
    raise SystemExit("❌ PRIMARY_GUILD_ID and OWNER_ID must be integers (IDs). Exiting.")

if OPENAI_KEY and openai:
    openai.api_key = OPENAI_KEY

# --------- FLASK KEEP ALIVE ----------
app = Flask("modmail")

@app.route("/")
def home():
    return "Modmail bot running."

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

Thread(target=run_flask, daemon=True).start()

# --------- DATA PERSISTENCE ----------
DATA_FILE = "modmail_data.json"

DEFAULT_DATA = {
    "category_id": None,
    "staff_role_id": None,
    "solve_keyword": "solved",
    "close_keyword": "close",
    "tickets": {},  # maps user_id (str) -> channel_id (int)
    "allow_other_guilds": False
}

def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w") as f:
            json.dump(DEFAULT_DATA, f, indent=4)
        return DEFAULT_DATA.copy()
    with open(DATA_FILE, "r") as f:
        d = json.load(f)
    # ensure missing keys exist
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v
    return d

def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=4)

data = load_data()

# --------- BOT SETUP ----------
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# helper: human timestamp with seconds
def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# role check helper
def is_staff(member: discord.Member):
    role_id = data.get("staff_role_id")
    if not role_id:
        return False
    return any(r.id == role_id for r in member.roles)

# ---------- Ticket Buttons ----------
class TicketView(ui.View):
    def __init__(self, ticket_user_id: int, timeout=None):
        super().__init__(timeout=timeout)
        self.ticket_user_id = ticket_user_id

    # Mark solved button
    @ui.button(label="Mark Solved", style=discord.ButtonStyle.success, emoji="✅")
    async def solved_button(self, interaction: discord.Interaction, button: ui.Button):
        # only staff allowed
        if not interaction.guild:
            return await interaction.response.send_message("This can only be used in the server.", ephemeral=True)
        member = interaction.user
        if not is_staff(member) and member.guild_permissions.administrator is False:
            return await interaction.response.send_message("❎ You are not staff.", ephemeral=True)

        solved_embed = discord.Embed(
            title="✅ Problem Marked Solved",
            description=f"Marked solved by {member.mention}",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        solved_embed.add_field(name="Time", value=now_ts())
        await interaction.channel.send(embed=solved_embed)
        await interaction.response.send_message("✅ Marked solved.", ephemeral=True)

    # AI reply button (optional)
    @ui.button(label="AI Reply (draft)", style=discord.ButtonStyle.secondary, emoji="🤖")
    async def ai_button(self, interaction: discord.Interaction, button: ui.Button):
        # only staff allowed
        if not interaction.guild:
            return await interaction.response.send_message("This can only be used in the server.", ephemeral=True)
        member = interaction.user
        if not is_staff(member) and member.guild_permissions.administrator is False:
            return await interaction.response.send_message("❎ You are not staff.", ephemeral=True)

        if not OPENAI_KEY or not openai:
            return await interaction.response.send_message("❎ OpenAI not configured on this bot.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)

        # gather last ~10 messages from the ticket channel to summarize / generate a reply
        msgs = []
        async for m in interaction.channel.history(limit=20, oldest_first=False):
            # include only user/staff messages (skip bot messages)
            if m.author.bot:
                continue
            msgs.append(f"{m.author.name}: {m.content}")
        prompt = ("You are a support assistant. Given the recent conversation, draft a short, professional reply "
                  "that staff can paste to the user to resolve the ticket. Conversation:\n\n" +
                  "\n".join(reversed(msgs[:20])) +
                  "\n\nDraft reply:")

        # call OpenAI (ChatCompletion)
        try:
            completion = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role":"user","content":prompt}],
                max_tokens=300,
                temperature=0.2
            )
            reply = completion.choices[0].message.content.strip()
            # send ephemeral suggestion
            await interaction.followup.send(f"🤖 **AI draft:**\n\n{reply}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❎ OpenAI error: {e}", ephemeral=True)

    # Close ticket button
    @ui.button(label="Close Ticket (delete)", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def close_button(self, interaction: discord.Interaction, button: ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("This can only be used in the server.", ephemeral=True)
        member = interaction.user
        if not is_staff(member) and member.guild_permissions.administrator is False:
            return await interaction.response.send_message("❎ You are not staff.", ephemeral=True)

        await interaction.response.send_message("✅ Closing ticket. Channel will be deleted in 5s.", ephemeral=True)
        # send final message to channel (public) before deleting so staff know
        final = discord.Embed(title="🗑️ Ticket closed", description=f"Closed by {member.mention}", color=discord.Color.dark_gray(), timestamp=datetime.now(timezone.utc))
        await interaction.channel.send(embed=final)
        # remove mapping
        # find user mapping by channel id
        ch_id = interaction.channel.id
        to_remove = None
        for uid, cid in list(data.get("tickets", {}).items()):
            if cid == ch_id:
                to_remove = uid
                break
        if to_remove:
            data["tickets"].pop(to_remove, None)
            save_data(data)
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {member} via button")
        except Exception:
            pass

# ---------- Helper: create ticket channel ----------
async def create_ticket_channel(guild: discord.Guild, user: discord.User):
    # returns channel object
    cat_id = data.get("category_id")
    staff_role_id = data.get("staff_role_id")
    category = guild.get_channel(cat_id) if cat_id else None
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None

    # channel name
    cleanname = f"ticket-{user.id}"
    # permissions
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=False)
    # create in category if provided
    try:
        if category and isinstance(category, discord.CategoryChannel):
            ch = await category.create_text_channel(cleanname, overwrites=overwrites, reason="New modmail ticket")
        else:
            ch = await guild.create_text_channel(cleanname, overwrites=overwrites, reason="New modmail ticket")
    except Exception:
        # fallback: create at guild root
        ch = await guild.create_text_channel(cleanname, overwrites=overwrites)
    # save mapping
    data["tickets"][str(user.id)] = ch.id
    save_data(data)
    return ch

# ---------- On DM received: create/forward ticket ----------
@bot.event
async def on_message(message: discord.Message):
    # allow normal bot commands to function
    await bot.process_commands(message)

    # ignore bots
    if message.author.bot:
        return

    # If it's a DM to the bot -> create/forward ticket
    if isinstance(message.channel, discord.DMChannel):
        # refuse if user account under 5 weeks
        account_age_days = (datetime.now(timezone.utc) - message.author.created_at).days
        if account_age_days < 35:
            try:
                await message.author.send("❌ You cannot open a support ticket. Your Discord account is under 5 weeks old.")
            except Exception:
                pass
            return

        guild = bot.get_guild(PRIMARY_GUILD_ID)
        if not guild:
            # if toggled to allow other guilds, optionally respond
            if data.get("allow_other_guilds"):
                await message.author.send("⚠️ Primary guild not available to create ticket now.")
            else:
                await message.author.send("⚠️ Support is currently only available via our server.")
            return

        # check existing
        user_id_str = str(message.author.id)
        existing_channel_id = data.get("tickets", {}).get(user_id_str)
        channel = guild.get_channel(existing_channel_id) if existing_channel_id else None
        if not channel:
            # create channel
            channel = await create_ticket_channel(guild, message.author)
            # send initial embed with buttons
            embed = discord.Embed(
                title="📩 New Ticket",
                description=f"Ticket created by {message.author.mention} ({message.author.id})",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Account Age", value=f"{account_age_days} days", inline=False)
            embed.add_field(name="Time", value=now_ts(), inline=False)
            try:
                await channel.send(embed=embed, view=TicketView(ticket_user_id=message.author.id))
            except Exception:
                await channel.send(embed=embed)

            # inform user
            try:
                await message.author.send(f"✅ Your ticket has been created. Staff will reply in {guild.name}.")
            except Exception:
                pass

        # forward the message content to the channel
        forward_embed = discord.Embed(
            title=f"📨 Message from user ({message.author})",
            description=message.content or "[attachment]",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        forward_embed.set_footer(text=f"User ID: {message.author.id} • {now_ts()}")
        # send attachments if present
        files = []
        for att in message.attachments:
            try:
                fp = await att.to_file()
                files.append(fp)
            except Exception:
                pass
        try:
            if files:
                await channel.send(embed=forward_embed, files=files)
            else:
                await channel.send(embed=forward_embed)
        except Exception:
            pass

        return  # end DM handling

    # If message is in one of the ticket channels and author is staff, check keywords
    if message.guild and str(message.guild.id) != "":
        ch_id = message.channel.id
        # only proceed for channels that exist in tickets mapping
        if ch_id in (list(data.get("tickets", {}).values())):
            # check if author is staff
            member = message.author
            if not isinstance(member, discord.Member):
                try:
                    member = await message.guild.fetch_member(message.author.id)
                except Exception:
                    return
            if not is_staff(member) and member.guild_permissions.administrator is False:
                return  # not staff, ignore keyword triggers
            content = (message.content or "").strip().lower()
            solve_kw = (data.get("solve_keyword") or "solved").lower().strip()
            close_kw = (data.get("close_keyword") or "close").lower().strip()
            # match exact or startswith
            matched_solve = (content == solve_kw) or content.startswith(solve_kw)
            matched_close = (content == close_kw) or content.startswith(close_kw)
            if matched_solve:
                solved_embed = discord.Embed(title="✅ Problem Marked Solved", description=f"Marked solved by {member.mention}", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
                solved_embed.add_field(name="Time", value=now_ts())
                await message.channel.send(embed=solved_embed)
                try:
                    await message.delete()  # remove the keyword message to keep channel clean
                except Exception:
                    pass
            elif matched_close:
                final_embed = discord.Embed(title="🗑️ Ticket Closed", description=f"Closed by {member.mention}. Deleting channel...", color=discord.Color.dark_gray(), timestamp=datetime.now(timezone.utc))
                await message.channel.send(embed=final_embed)
                # remove mapping and delete after short delay
                to_remove = None
                for uid, cid in list(data.get("tickets", {}).items()):
                    if cid == ch_id:
                        to_remove = uid
                        break
                if to_remove:
                    data["tickets"].pop(to_remove, None)
                    save_data(data)
                await asyncio.sleep(3)
                try:
                    await message.channel.delete(reason=f"Closed by keyword by {member}")
                except Exception:
                    pass

# ---------- Slash / text commands for settings ----------
@guild := discord.Object(id=PRIMARY_GUILD_ID)  # for registration hint (not used directly)

# register guild-specific commands on ready
@bot.event
async def on_ready():
    # ensure guild-only slash commands are registered to your guild (fast)
    try:
        await bot.tree.sync(guild=discord.Object(id=PRIMARY_GUILD_ID))
        print(f"✅ Synced commands to guild {PRIMARY_GUILD_ID}")
    except Exception as e:
        print(f"⚠️ Could not sync to guild only: {e}; syncing globally")
        try:
            await bot.tree.sync()
        except Exception as e2:
            print(f"❌ Global sync failed: {e2}")

    # DM owner
    try:
        owner = await bot.fetch_user(OWNER_ID)
        host = os.getenv("HOSTNAME", "unknown-host")
        await owner.send(f"✅ Modmail bot logged in as {bot.user} on {host} — {now_ts()}")
    except Exception as e:
        print(f"⚠️ Could not DM owner: {e}")

# ---------- slash commands ----------
@bot.tree.command(name="set_category", description="Set category where ticket channels are created (admins only).")
async def set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ You must be an administrator.", ephemeral=True)
    data["category_id"] = category.id
    save_data(data)
    await interaction.response.send_message(f"✅ Ticket category set to {category.name} ({category.id}) — {now_ts()}", ephemeral=True)

@bot.tree.command(name="set_staff_role", description="Set role used as staff for tickets (admins only).")
async def set_staff_role(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ You must be an administrator.", ephemeral=True)
    data["staff_role_id"] = role.id
    save_data(data)
    await interaction.response.send_message(f"✅ Staff role set to {role.name} ({role.id}) — {now_ts()}", ephemeral=True)

@bot.tree.command(name="set_solve_keyword", description="Set the keyword staff can type to mark solved")
async def set_solve_keyword(interaction: discord.Interaction, keyword: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    data["solve_keyword"] = keyword.strip()
    save_data(data)
    await interaction.response.send_message(f"✅ Solve keyword set to `{keyword}` — {now_ts()}", ephemeral=True)

@bot.tree.command(name="set_close_keyword", description="Set the keyword staff can type to close a ticket (deletes channel)")
async def set_close_keyword(interaction: discord.Interaction, keyword: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    data["close_keyword"] = keyword.strip()
    save_data(data)
    await interaction.response.send_message(f"✅ Close keyword set to `{keyword}` — {now_ts()}", ephemeral=True)

@bot.tree.command(name="settings", description="Show current modmail settings (admins only).")
async def settings(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    category = bot.get_guild(PRIMARY_GUILD_ID).get_channel(data.get("category_id")) if data.get("category_id") else None
    staff_role = bot.get_guild(PRIMARY_GUILD_ID).get_role(data.get("staff_role_id")) if data.get("staff_role_id") else None
    embed = discord.Embed(title="⚙️ Modmail Settings", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Category", value=(f"{category.name} ({category.id})" if category else "❎ Not set"), inline=False)
    embed.add_field(name="Staff Role", value=(f"{staff_role.name} ({staff_role.id})" if staff_role else "❎ Not set"), inline=False)
    embed.add_field(name="Solve Keyword", value=f"`{data.get('solve_keyword')}`", inline=True)
    embed.add_field(name="Close Keyword", value=f"`{data.get('close_keyword')}`", inline=True)
    embed.add_field(name="Primary Guild ID", value=str(PRIMARY_GUILD_ID), inline=False)
    embed.add_field(name="OpenAI configured", value=("✅ Yes" if OPENAI_KEY else "❎ No"), inline=False)
    embed.add_field(name="Allow other guild DMs -> ticket", value=("✅ Yes" if data.get("allow_other_guilds") else "❎ No"), inline=False)
    embed.set_footer(text=f"Requested by {interaction.user} • {now_ts()}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="toggle_other_guilds", description="Toggle whether the bot will create tickets when DM'd even if primary guild not present (admin only).")
async def toggle_other_guilds(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    data["allow_other_guilds"] = not data.get("allow_other_guilds", False)
    save_data(data)
    await interaction.response.send_message(f"✅ allow_other_guilds set to {data['allow_other_guilds']} — {now_ts()}", ephemeral=True)

@bot.tree.command(name="force_close", description="Force close this ticket (staff only; deletes channel).")
async def force_close(interaction: discord.Interaction):
    # this command should be run in a ticket channel
    ch = interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message("❎ This must be run in the ticket channel.", ephemeral=True)
    member = interaction.user
    if not is_staff(member) and member.guild_permissions.administrator is False:
        return await interaction.response.send_message("❎ Staff only.", ephemeral=True)
    await interaction.response.send_message("✅ Closing and deleting channel in 4s...", ephemeral=True)
    # remove mapping
    ch_id = ch.id
    to_remove = None
    for uid, cid in list(data.get("tickets", {}).items()):
        if cid == ch_id:
            to_remove = uid
            break
    if to_remove:
        data["tickets"].pop(to_remove, None)
        save_data(data)
    await asyncio.sleep(4)
    try:
        await ch.delete(reason=f"Force-closed by {member}")
    except Exception:
        pass

# ---------- Small helper: sync command for admin (text) ----------
@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    await ctx.send("🔁 Syncing slash commands for primary guild...")
    try:
        await bot.tree.sync(guild=discord.Object(id=PRIMARY_GUILD_ID))
        await ctx.send("✅ Synced to primary guild.")
    except Exception as e:
        await ctx.send(f"❎ Sync error: {e}")

# ---------- run ----------
if __name__ == "__main__":
    print("Starting modmail bot...")
    bot.run(DISCORD_TOKEN)
