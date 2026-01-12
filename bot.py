import logging
import os
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("slotbot")

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

class SlotError(Exception):
    """User-facing error for slot operations."""


def parse_duration(duration, time_value):
    duration_lower = duration.lower().strip()
    if "lifetime" in duration_lower:
        return None, "lifetime"

    try:
        amount = int(time_value)
    except (TypeError, ValueError) as exc:
        raise SlotError("The duration time must be a number.") from exc

    if amount <= 0:
        raise SlotError("The duration time must be greater than zero.")

    if "week" in duration_lower or duration_lower == "w":
        return timedelta(weeks=amount), "week"
    if "month" in duration_lower or duration_lower == "mo":
        return timedelta(days=amount * 30), "month"
    if "year" in duration_lower or duration_lower == "y":
        return timedelta(days=amount * 365), "year"

    raise SlotError("Invalid duration format. Use week, month, year, or lifetime.")


async def send_response(interaction: discord.Interaction, message: str, **kwargs):
    if interaction.response.is_done():
        await interaction.followup.send(message, **kwargs)
    else:
        await interaction.response.send_message(message, **kwargs)


async def expire_slot(slot, reason: str):
    channel = slot.get("channel")
    if channel:
        try:
            await channel.set_permissions(
                channel.guild.default_role,
                send_messages=False,
                add_reactions=False,
            )
            await channel.set_permissions(
                slot.get("user"),
                send_messages=False,
                add_reactions=False,
            )
            embed = discord.Embed(
                title="Slot expired",
                description=reason,
                color=discord.Color.red(),
            )
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Missing permissions to update channel %s", channel.id)
        except discord.HTTPException:
            logger.exception("Failed to update channel %s", channel.id)
    slot["expired"] = True

# Task to reset pings every day at midnight
@tasks.loop(hours=24)
async def reset_pings():
    try:
        now = datetime.utcnow()
        for slot in slots.values():
            end_date = slot.get("end_date")
            if end_date is not None and end_date <= now and not slot.get("expired"):
                await expire_slot(
                    slot,
                    "This slot has reached its end date and is now locked.",
                )
                continue
            if end_date is None or end_date > now:  # Lifetime slots or active slots
                slot["pings_used"] = {"here": 0, "everyone": 0}
                slot.setdefault("pings", {"here": 0, "everyone": 0})
                # Reset daily pings (e.g., 1x @here for weekly slots)
                if "week" in slot.get("duration", ""):
                    slot["pings"]["here"] = 1
                elif "month" in slot.get("duration", ""):
                    slot["pings"]["here"] = 2
                elif "year" in slot.get("duration", ""):
                    slot["pings"]["here"] = 3
                elif "lifetime" in slot.get("duration", ""):
                    slot["pings"]["here"] = 3
                    slot["pings"]["everyone"] = 1
        logger.info("Pings have been reset.")
    except Exception:
        logger.exception("Error while resetting pings")

# Start the ping reset task
@bot.event
async def on_ready():
    await bot.tree.sync()  # Sync slash commands
    if not reset_pings.is_running():
        reset_pings.start()  # Start the daily ping reset
    logger.info("Bot is ready as %s", bot.user)

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
            await send_response(
                interaction,
                "You do not have permission to execute this command.",
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            await send_response(
                interaction,
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if here < 0 or everyone < 0:
            raise SlotError("Additional ping values must be zero or greater.")

        # Parse the duration and time to get a timedelta
        delta, normalized_duration = parse_duration(duration, time)
        end_date = datetime.utcnow() + delta if delta else None

        # Default pings based on duration
        if normalized_duration == "week":
            default_here = 1
            default_everyone = 0
        elif normalized_duration == "month":
            default_here = 2
            default_everyone = 0
        elif normalized_duration == "year":
            default_here = 3
            default_everyone = 0
        else:
            default_here = 3
            default_everyone = 1

        # Apply additional pings if specified
        total_here = default_here + here if here else default_here
        total_everyone = default_everyone + everyone if everyone else default_everyone
        if total_here < 0 or total_everyone < 0:
            raise SlotError("Ping totals cannot be negative.")

        # Create a channel (slot) for the user
        guild = interaction.guild
        if user.id in slots:
            existing_slot = slots[user.id]
            existing_end = existing_slot.get("end_date")
            if existing_end is None or existing_end > datetime.utcnow():
                raise SlotError("This user already has an active slot.")

        bot_member = guild.get_member(bot.user.id) if bot.user else None
        if bot_member is None:
            raise SlotError("Bot member data is unavailable in this guild.")
        if not bot_member.guild_permissions.manage_channels:
            raise SlotError("I need the Manage Channels permission to create slot channels.")

        channel_name = f"slot-{user.name}"
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),  # Everyone cannot read messages by default
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, add_reactions=True),  # User can read and send messages, react
            bot.user: discord.PermissionOverwrite(read_messages=True, send_messages=True, add_reactions=True, administrator=True)  # Bot has admin permissions
        }

        # Create the channel
        try:
            channel = await guild.create_text_channel(channel_name, overwrites=overwrites)
        except discord.Forbidden as exc:
            raise SlotError("I do not have permission to create channels.") from exc
        except discord.HTTPException as exc:
            raise SlotError("Failed to create the slot channel. Please try again later.") from exc

        # Create a slot record
        slots[user.id] = {
            "user": user,
            "duration": normalized_duration,
            "end_date": end_date,
            "pings": {"here": total_here, "everyone": total_everyone},
            "pings_used": {"here": 0, "everyone": 0},
            "hold": False,
            "expired": False,
            "channel": channel  # Store the channel associated with the slot
        }

        duration_label = "Lifetime" if normalized_duration == "lifetime" else f"{time} {normalized_duration}"

        # Send embed with slot information
        embed = discord.Embed(
            title=f"Slot Created for {user.name}",
            description=(
                f"Duration: {duration_label}\n"
                f"End Date: {end_date if end_date else 'Lifetime'}\n\n"
                f"**Ping Allowed:**\n@everyone: {total_everyone}\n@here: {total_here}"
            ),
            color=discord.Color.purple()
        )
        embed.add_field(name="Lock Time", value=str(end_date) if end_date else "Lifetime", inline=True)
        embed.add_field(name="Rules", value="• MUST follow the slot rules strictly\n• Always accept MM", inline=False)

        await interaction.response.send_message(embed=embed)
        await channel.send(f"Welcome to your slot, {user.mention}! This is your personal space to manage your pings.")

    except SlotError as exc:
        await send_response(interaction, str(exc), ephemeral=True)
    except Exception:
        logger.exception("Error while creating slot")
        await send_response(
            interaction,
            "An error occurred while creating the slot. Please try again later.",
            ephemeral=True,
        )


# Command to ping
@bot.tree.command(name="ping")
async def ping(interaction: discord.Interaction, ping_type: str):
    try:
        user = interaction.user
        if interaction.guild is None:
            await send_response(
                interaction,
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        normalized_type = ping_type.lower().strip()
        if normalized_type not in {"here", "everyone"}:
            await send_response(
                interaction,
                "Invalid ping type. Use `here` or `everyone`.",
                ephemeral=True,
            )
            return

        # Check if the user has a slot
        if user.id not in slots:
            await send_response(interaction, "You do not have a slot.", ephemeral=True)
            return

        slot = slots[user.id]
        end_date = slot.get("end_date")
        if end_date is not None and end_date <= datetime.utcnow():
            if not slot.get("expired"):
                await expire_slot(
                    slot,
                    "This slot has reached its end date and is now locked.",
                )
            await send_response(
                interaction,
                "Your slot has expired. Please contact an owner to renew it.",
                ephemeral=True,
            )
            return

        if slot.get("hold"):
            await send_response(
                interaction,
                "Your slot is currently on hold.",
                ephemeral=True,
            )
            return

        # Check if the user has any pings left
        if slot["pings_used"]["here"] >= slot["pings"]["here"] and slot["pings_used"]["everyone"] >= slot["pings"]["everyone"]:
            await send_response(
                interaction,
                f"{user.name}, you don't have enough pings in stock.",
                ephemeral=True,
            )
            return

        if normalized_type == "here" and slot["pings_used"]["here"] < slot["pings"]["here"]:
            slot["pings_used"]["here"] += 1
            await interaction.response.send_message(f"{user.mention} you used {slot['pings_used']['here']}/{slot['pings']['here']} @here. Use MM")
        elif normalized_type == "everyone" and slot["pings_used"]["everyone"] < slot["pings"]["everyone"]:
            # Ensure the user has @everyone pings available
            if slot["pings_used"]["here"] >= slot["pings"]["here"]:
                await send_response(
                    interaction,
                    "You must use your @here ping before using @everyone.",
                    ephemeral=True,
                )
                return
            slot["pings_used"]["everyone"] += 1
            await interaction.response.send_message(f"{user.mention} you used {slot['pings_used']['everyone']}/{slot['pings']['everyone']} @everyone. Use MM")
        else:
            await send_response(
                interaction,
                f"{user.name}, you have used all your pings of type {normalized_type}.",
                ephemeral=True,
            )
    
    except SlotError as exc:
        await send_response(interaction, str(exc), ephemeral=True)
    except Exception:
        logger.exception("Error while processing ping")
        await send_response(
            interaction,
            "An error occurred while processing your ping. Please try again later.",
            ephemeral=True,
        )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandInvokeError):
        original = error.original
        if isinstance(original, SlotError):
            await send_response(interaction, str(original), ephemeral=True)
            return
    logger.exception("Unhandled command error: %s", error)
    await send_response(
        interaction,
        "An unexpected error occurred while running the command.",
        ephemeral=True,
    )

# Run the bot
token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set.")

bot.run(token)
