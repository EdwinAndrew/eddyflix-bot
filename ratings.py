"""Movie night ratings (#50): 1-5 🍿 buttons on the recap, open for a day, and the
lines /movienight-history shows.

Who may rate: anyone who was in the Theatre while the night was live, the people
on the recap (attended or dropped by). A night with nobody counted is open to
anyone. Rating again replaces the earlier one.

The buttons are DynamicItems with the event id in their custom_id, registered in
the bot's setup_hook, so they keep working after a restart. The day is counted
from the recap message itself, so nothing else needs storing.
"""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord

import watch_data

TZ = ZoneInfo("America/Toronto")
WINDOW = timedelta(hours=24)
FIELD = "🍿 Rate it"
STARS = range(1, 6)


# --- the rules -----------------------------------------------------------------

def is_open(posted_at: datetime, now: datetime) -> bool:
    return now < posted_at + WINDOW


def can_rate(discord_id: int, minutes: dict[int, int]) -> bool:
    """minutes is Theatre minutes per member for the night (watch_data.theatre_minutes)."""
    if not any(m > 0 for m in minutes.values()):
        return True
    return minutes.get(discord_id, 0) > 0


def average(scores) -> float | None:
    scores = list(scores)
    return sum(scores) / len(scores) if scores else None


def score(avg: float | None, votes: int) -> str:
    """"🍿 4.3 (3)", or "not rated"."""
    return f"🍿 {avg:.1f} ({votes})" if avg is not None and votes else "not rated"


def field_text(scores, closes: datetime) -> str:
    """The recap's rating field, updated as people rate."""
    scores = list(scores)
    until = f"<t:{int(closes.timestamp())}:f>"
    if not scores:
        return f"How was it? Everyone who was in the Theatre can rate it, until {until}."
    votes = "1 rating" if len(scores) == 1 else f"{len(scores)} ratings"
    return f"**{average(scores):.1f}** / 5 from {votes}. Open until {until}; press again to change yours."


# --- /movienight-history -------------------------------------------------------

def night_date(start: datetime) -> str:
    local = start.astimezone(TZ)
    return f"{local:%a} {local:%b} {local.day}, {local.year}"


def history_line(night: dict, link: str | None) -> str:
    """"**Fri Nov 13, 2026** · [1917 (2019)](…) · hosted by Eddy · 3 came · 🍿 4.3 (3)"."""
    film = f"[{night['title']}]({link})" if link else night["title"]
    bits = [f"**{night_date(night['start_time'])}**", film]
    if night.get("host"):
        bits.append(f"hosted by {night['host']}")
    bits.append(f"{night['attended']} came" if night["attended"] else "nobody in the Theatre")
    bits.append(score(night["average"], night["votes"]))
    return " · ".join(bits)


def top_lines(films: list[tuple[str, float, int]]) -> str:
    """The club's best-rated films, from watch_data.top_rated_films."""
    medals = ["🥇", "🥈", "🥉"]
    return "\n".join(f"{medals[i] if i < 3 else f'{i + 1}.'} **{title}** · {score(avg, votes)}"
                     for i, (title, avg, votes) in enumerate(films))


# --- the buttons ---------------------------------------------------------------

class RateButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"movienight:rate:(?P<event>[0-9]+):(?P<stars>[1-5])"):
    def __init__(self, event_id: int, stars: int):
        super().__init__(discord.ui.Button(label=str(stars), emoji="🍿", style=discord.ButtonStyle.secondary,
                                           custom_id=f"movienight:rate:{event_id}:{stars}"))
        self.event_id, self.stars = event_id, stars

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(int(match["event"]), int(match["stars"]))

    async def callback(self, interaction: discord.Interaction):
        night = await watch_data.get_movie_night(self.event_id)
        title = night["title"] if night else "this movie night"
        message = interaction.message
        if night is None or message is None or not is_open(message.created_at, discord.utils.utcnow()):
            await interaction.response.send_message(f"Ratings for **{title}** have closed.", ephemeral=True)
            return
        if not can_rate(interaction.user.id, await watch_data.theatre_minutes(self.event_id)):
            await interaction.response.send_message(
                f"Only people who were in the Theatre for **{title}** can rate it.", ephemeral=True)
            return
        await watch_data.set_rating(self.event_id, interaction.user.id, self.stars)
        await interaction.response.send_message(
            f"You gave **{title}** {'🍿' * self.stars} ({self.stars}/5). Press another to change it.", ephemeral=True)
        # Show the running average on the recap
        if message.embeds:
            embed = message.embeds[0]
            text = field_text((await watch_data.night_ratings(self.event_id)).values(), message.created_at + WINDOW)
            for i, f in enumerate(embed.fields):
                if f.name == FIELD:
                    embed.set_field_at(i, name=FIELD, value=text, inline=False)
                    break
            else:
                embed.add_field(name=FIELD, value=text, inline=False)
            try:
                await message.edit(embed=embed)
            except discord.HTTPException as e:
                print(f"Could not update the rating on {title}: {e}")


def rating_view(event_id) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for stars in STARS:
        view.add_item(RateButton(int(event_id), stars))
    return view
