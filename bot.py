import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta
import re

intents = discord.Intents.default()
intents.message_content = True  # Allows access to message content

bot = commands.Bot(command_prefix="/", intents=intents)

# Owners' IDs
owners = [578619592403058698, 1276823690742337577]

# Function to check if the user is an owner
def is_owner(interaction: discord.Interaction):
    return interaction.user.id in owners

# Dictionary to store slots
slots = {}

# Function to parse flexible duration input for weeks, months, years, and lifetime
def parse_duration(duration, time):
    try:
        if "week" in duration.lower() or "w" in duration.lower():
            return timedelta(weeks=int(time))
        elif "month" in duration.lower() or "mo" in duration.lower():
            return timedelta(days=int(time) * 30)  # Approx 30 days per month
        elif "year" in duration.lower() or "y" in duration.lower():
            return timedelta(days=int(time) * 365)  # Approx 365 days per year
        elif "lifetime" in duration.lower():
            return None  # Lifetime has no end date
    except Exception as e:
        print(f"Error while parsing duration: {e}")
        return None

# Task to reset pings every day at midnight
@tasks.loop(hours=24)
async def reset_pings():
    try:
        now = datetime.utcnow()
        for slot in slots.values():
            if slot["end_date"] is None or slot["end_date"] > now:  # Lifetime slots or active slots
                slot["pings_used"] = {"here": 0, "everyone": 0}
                # Reset daily pings (e.g., 1x @here for weekly slots)
                if "week" in slot["duration"]:
                    slot["pings"]["here"] = 1
                elif "month" in slot["duration"]:
                    slot["pings"]["here"] = 2
                elif "year" in slot["duration"]:
                    slot["pings"]["here"] = 3
                elif "lifetime" in slot["duration"]:
                    slot["pings"]["here"] = 3
                    slot["pings"]["everyone"] = 1
        print("Pings have been reset.")
    except Exception as e:
        print(f"Error while resetting pings: {e}")

# Start the ping reset task
@bot.event
async def on_ready():
    await bot.tree.sync()  # Sync slash commands
    reset_pings.start()  # Start the daily ping reset
    print(f"Bot is ready as {bot.user}")

# Define the options for the duration dropdown (week, month, year, lifetime)
duration_options = [
    discord.SelectOption(label="Week", value="week"),
    discord.SelectOption(label="Month", value="month"),
    discord.SelectOption(label="Year", value="year"),
    discord.SelectOption(label="Lifetime", value="lifetime"),
]

# Define the options for additional pings (1-9)
ping_options = [discord.SelectOption(label=str(i), value=str(i)) for i in range(1, 10)]

# Command to create a slot and channel
@bot.tree.command(name="slot")
async def slot(
    interaction: discord.Interaction,
    user: discord.User,
    duration: str,
    time: int,
    here: int = 0,
    everyone: int = 0
):
    try:
        if not is_owner(interaction):
            await interaction.response.send_message("You do not have permission to execute this command.")
            return

        # Parse the duration and time to get a timedelta
        delta = parse_duration(duration, time)
        if not delta:
            await interaction.response.send_message("Invalid duration format. Use formats like '2 weeks', '3 months', '1 year', or 'lifetime'.")
            return

        end_date = datetime.utcnow() + delta if delta else None

        # Default pings based on duration
        if "week" in duration.lower():
            default_here = 1
            default_everyone = 0
        elif "month" in duration.lower():
            default_here = 2
            default_everyone = 0
        elif "year" in duration.lower():
            default_here = 3
            default_everyone = 0
        elif "lifetime" in duration.lower():
            default_here = 3
            default_everyone = 1

        # Apply additional pings if specified
        total_here = default_here + here if here else default_here
        total_everyone = default_everyone + everyone if everyone else default_everyone

        # Create a channel (slot) for the user
        guild = interaction.guild
        channel_name = f"slot-{user.name}"
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),  # Everyone cannot read messages by default
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, add_reactions=True),  # User can read and send messages, react
            bot.user: discord.PermissionOverwrite(read_messages=True, send_messages=True, add_reactions=True, administrator=True)  # Bot has admin permissions
        }

        # Create the channel
        channel = await guild.create_text_channel(channel_name, overwrites=overwrites)

        # Create a slot record
        slots[user.id] = {
            "user": user,
            "duration": duration,
            "end_date": end_date,
            "pings": {"here": total_here, "everyone": total_everyone},
            "pings_used": {"here": 0, "everyone": 0},
            "hold": False,
            "channel": channel  # Store the channel associated with the slot
        }

        # Send embed with slot information
        embed = discord.Embed(
            title=f"Slot Created for {user.name}",
            description=f"Duration: {duration} ({time} {duration})\nEnd Date: {end_date if end_date else 'Lifetime'}\n\n**Ping Allowed:**\n@everyone: {total_everyone}\n@here: {total_here}",
            color=discord.Color.purple()
        )
        embed.add_field(name="Lock Time", value=str(end_date) if end_date else "Lifetime", inline=True)
        embed.add_field(name="Rules", value="• MUST follow the slot rules strictly\n• Always accept MM", inline=False)

        await interaction.response.send_message(embed=embed)
        await channel.send(f"Welcome to your slot, {user.mention}! This is your personal space to manage your pings.")

    except Exception as e:
        print(f"Error while creating slot: {e}")
        await interaction.response.send_message("An error occurred while creating the slot. Please try again later.")


# Command to ping
@bot.tree.command(name="ping")
async def ping(interaction: discord.Interaction, ping_type: str):
    try:
        user = interaction.user

        # Check if the user has a slot
        if user.id not in slots:
            await interaction.response.send_message("You do not have a slot.")
            return

        slot = slots[user.id]

        # Check if the user has any pings left
        if slot["pings_used"]["here"] >= slot["pings"]["here"] and slot["pings_used"]["everyone"] >= slot["pings"]["everyone"]:
            await interaction.response.send_message(f"{user.name}, you don't have enough pings in stock.")
            return

        if ping_type == "here" and slot["pings_used"]["here"] < slot["pings"]["here"]:
            slot["pings_used"]["here"] += 1
            await interaction.response.send_message(f"{user.mention} you used {slot['pings_used']['here']}/{slot['pings']['here']} @here. Use MM")
        elif ping_type == "everyone" and slot["pings_used"]["everyone"] < slot["pings"]["everyone"]:
            # Ensure the user has @everyone pings available
            if slot["pings_used"]["here"] >= slot["pings"]["here"]:
                await interaction.response.send_message("You must use your @here ping before using @everyone.")
                return
            slot["pings_used"]["everyone"] += 1
            await interaction.response.send_message(f"{user.mention} you used {slot['pings_used']['everyone']}/{slot['pings']['everyone']} @everyone. Use MM")
        else:
            await interaction.response.send_message(f"{user.name}, you have used all your pings of type {ping_type}.")
    
    except Exception as e:
        print(f"Error while processing ping: {e}")
        await interaction.response.send_message("An error occurred while processing your ping. Please try again later.")

# Run the bot
bot.run("MTQ1OTIwMTc1OTgxMjY0OTE2MA.GBmeO_.ctvw0vFfkjzeLDQ8Kt4Q_aJGZUMhR9PknVnTrU")
