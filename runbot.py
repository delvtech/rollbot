import io
import os
import re
from datetime import datetime
from typing import Any, Dict, List

import discord
import requests
from discord import TextChannel
from discord.ext import commands, tasks
from dotenv import dotenv_values

# pylint: disable=too-many-arguments
# ruff: noqa: D101, D102

# constants
ROBOTS_CHANNEL = 1077979151316828270
DATASUSSY_CHANNEL = 1139538470696669215
ROLLBAR_PAGES_TO_CHECK = 5
ROLLBAR_WAIT_SECONDS = 10
ROLLBAR_CHANNEL_IDS = {
    "critical": 1255561995508514916,
    "error": 1255562047433740388,
    "warning": 1256016800450347048,
    "info": 1256016871174443058,
    "testnet": 1256016939940184095,
}

# configuration: load env and check we have the keys we need
config = dotenv_values(".env")
assert config["DISCORD_BOT_TOKEN"], "DISCORD_BOT_TOKEN is not set in .env"
assert "ROLLBAR_SERVICE_BOTS_SEPOLIA_PAT" in config, "ROLLBAR_SERVICE_BOTS_SEPOLIA_PAT is not set in .env"
assert "ROLLBAR_SERVICE_BOTS_MAINNET_PAT" in config, "ROLLBAR_SERVICE_BOTS_MAINNET_PAT is not set in .env"

# utility functions
def format_timestamp(timestamp):
    return discord.utils.format_dt(datetime.fromtimestamp(timestamp), style='R')

def concatenate_items(entries) -> str:
    return "\n".join([
        f"{entry['data']['level']:<8} - {entry['data']['environment']:<12} - {entry['data']['body']['message']['body']}"
        for entry in entries
    ])

# entry={'id': 409806024567, 'project_id': 697774, 'timestamp': 1719596094, 'version': 2, 'data': data, 'billable': 1, 'item_id': 1554003164}
# data = {'timestamp': 1719596094, 'environment': 'checkpoint_bot', 'level': 'INFO', 'language': 'python 3.10.14', 'notifier': {'name': 'pyrollbar', 'version': '1.0.0'}, 'uuid': '91579ef2-655a-43e2-afa8-f798beec0c78', 'code_version': '1.0', 'body': body, 'server': server, 'metadata': metadata, 'framework': 'unknown', 'retentionDays': 180}
# body = {'message': {'body': 'This is a test info message #3'}}
# server = {'host': 'fedora', 'pid': 122207, 'argv': ['/code/elfpy/.venv/lib/python3.10/site-packages/ipykernel_launcher.py', '--f=/home/mihai/.local/share/jupyter/runtime/kernel-v2-3425277VxtCqD29GjQE.json']}
# metadata = {'customer_timestamp': 1719596094, 'timestamp_ms': 1719596094925, 'access_token': 'b188b787595449e4a24cb55db12c0d8c', 'api_server_hostname': 'k8s', 'debug': {'routes': {'start_time': 1719412468468, 'counters': {'post_item': 1444235}}}}
def prepare_rollbar_entry(entry, testnet: bool = False):
    preamble = f"check_rollbar_item_backlog::prepare_rollbar_entry::{'testnet' if testnet else 'mainnet'}:: "
    print(f"{preamble}preparing entry")
    data = entry["data"]
    print(f"{preamble}{data=}")
    content = data["body"]["message"]["body"]
    print(f"{preamble}{content=}")
    uuid = data["uuid"]
    embed = discord.Embed(title=f"{uuid}", url=f"https://rollbar.com/item/uuid/?uuid={uuid}")
    print(f"{preamble}{embed=}")
    embed.add_field(name="Level", value=data['level'], inline=True)
    print(f"{preamble}added level")
    embed.add_field(name="Environment", value=data['environment'], inline=True)
    print(f"{preamble}added environment")
    embed.add_field(name="Timestamp", value=format_timestamp(data['timestamp']), inline=True)
    print(f"{preamble}added timestamp")
    embed.add_field(name="Content", value=content, inline=False)
    print(f"{preamble}added content, returning embed")
    return embed

def check_rollbar_entries(page: int | None = None, testnet: bool = False) -> list:
    if page is None:
        page = 1
    # Fetch recent items from Rollbar
    key = config["ROLLBAR_SERVICE_BOTS_SEPOLIA_PAT"] if testnet else config["ROLLBAR_SERVICE_BOTS_MAINNET_PAT"]
    headers = {"X-Rollbar-Access-Token": key}
    # response = requests.get("https://api.rollbar.com/api/1/items/", params={"status": "active", "page": page}, headers=headers, timeout=10)
    response = requests.get("https://api.rollbar.com/api/1/instances/", params={"page": page}, headers=headers, timeout=10)
    response.raise_for_status()  # Raise an exception for bad status codes
    result = response.json().get("result", {})
    return result.get("instances", [])

def is_excluded(entry: dict, rollbar_exclusion_filter: list[str]) -> bool:
    # Check if a specific entry is in the exclusion filter
    message_body = entry["data"]["body"]["message"]["body"]
    return any(re.search(filter_entry.replace("*", ".*?"), message_body, re.DOTALL | re.IGNORECASE) for filter_entry in rollbar_exclusion_filter)

def load_exclusion_filter(testnet: bool) -> List[str]:
    exclusion_list = f"rollbar_exclusion_filter{'_testnet' if testnet else ''}.csv"
    if not os.path.exists(exclusion_list):
        with open(exclusion_list, "w", encoding="utf-8") as file:
            file.write("")
    with open(exclusion_list, "r", encoding="utf-8") as file:
        return file.read().splitlines()

def load_reported_ids(filename):
    if not os.path.exists(filename):
        open(filename, "w", encoding="utf-8").close()
        return []
    with open(filename, "r", encoding="utf-8") as file:
        return [int(line.strip()) for line in file]

class Rollbot(commands.Bot):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rollbar_channels: dict[str, TextChannel] = {}
        self.rollbar_reported_ids: list[int] = []
        self.rollbar_reported_ids_testnet: list[int] = []

    async def setup_hook(self):
        self.rollbar_check.start()  # type: ignore

    @tasks.loop(seconds=ROLLBAR_WAIT_SECONDS)
    async def rollbar_check(self):
        await self.perform_rollbar_check(testnet=False)
        await self.perform_rollbar_check(testnet=True)

    @rollbar_check.before_loop  # type: ignore
    async def before_rollbar_check(self):
        await self.wait_until_ready()
        print("Rollbar check is ready to start.")

    async def report_entry(self, entry: Dict[str, Any], testnet: bool) -> None:
        level = entry["data"]["level"].lower()
        if level not in self.rollbar_channels:
            print(f"EXCLUDED: {level} not in rollbar_channels")
            return

        channel = self.rollbar_channels["testnet" if testnet else level]
        await channel.send(embed=prepare_rollbar_entry(entry, testnet))

    def save_reported_id(self, entry_id: int, testnet: bool) -> None:
        filename = f"rollbar_reported_ids{'_testnet' if testnet else ''}.csv"
        with open(filename, "a", encoding="utf-8") as file:
            file.write(f"{entry_id}\n")

    async def perform_rollbar_check(self, testnet: bool = False) -> None:
        preamble = f"check_rollbar_item_backlog::perform_rollbar_check::{'testnet' if testnet else 'mainnet'}:: "
        exclusion_filter = load_exclusion_filter(testnet)
        reported_ids = bot.rollbar_reported_ids_testnet if testnet else bot.rollbar_reported_ids
        print(f"{preamble}Identifying entries to report")
        entries_to_report = []
        try:
            for page in range(1, ROLLBAR_PAGES_TO_CHECK + 1):
                entries = check_rollbar_entries(page=page, testnet=testnet)
                if new_entries := [ entry for entry in entries if entry["id"] not in reported_ids and not is_excluded(entry, exclusion_filter)]:
                    entries_to_report.extend(new_entries)
                else:
                    break
            print(f"{preamble}Found {len(entries_to_report)} items to report")
            if not entries_to_report:
                print(f"{preamble}No items to report, BREAKING")
            else:
                print(f"{preamble}Sending {len(entries_to_report)} items to report")
                for entry in entries_to_report[::-1]:  # Chronological order
                    await self.report_entry(entry=entry, testnet=testnet)
                    reported_ids.append(entry["id"])
                    self.save_reported_id(entry_id=entry["id"], testnet=testnet)
        except Exception as exc:
            print(f"{preamble}Failed to check rollbar: {exc}")

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = Rollbot(command_prefix="$", intents=intents)

@bot.event
async def on_ready():
    print(f"We have logged in as {bot.user}")

    # Instantiate channels
    for channel_name, channel_id in ROLLBAR_CHANNEL_IDS.items():
        channel = bot.get_channel(channel_id)
        assert channel, f"{channel_name} channel not found"
        assert isinstance(channel, TextChannel), f"{channel_name} channel is not a TextChannel"
        bot.rollbar_channels[channel_name] = channel

    # Load reported IDs
    bot.rollbar_reported_ids = load_reported_ids("rollbar_reported_ids.csv")
    bot.rollbar_reported_ids_testnet = load_reported_ids("rollbar_reported_ids_testnet.csv")

@bot.command()
async def rollbar(context):
    if context.channel.id not in [ROBOTS_CHANNEL, DATASUSSY_CHANNEL]:
        await context.send("This command can only be used in the 🤪︱data-sussy or 🤖︱ro-bots.")
        return

    try:
        # Parse optional parameters
        page = int(context.message.content.split()[1]) if len(context.message.content.split()) > 1 else 1
        testnet = context.message.content.split()[2] == "testnet" if len(context.message.content.split()) > 2 else False

        # Get entries
        entries = check_rollbar_entries(page=page, testnet=testnet)
        concatenated_items = concatenate_items(entries)
        file = discord.File(io.BytesIO(concatenated_items.encode('utf-8')), filename="rollbar_items.txt")
        await context.send(f"Here are the Rollbar items on page {page}:", file=file)
    except Exception as e:
        await context.send(f"Failed to rollbar: {str(e)}")

# Run the bot
bot.run(config["DISCORD_BOT_TOKEN"])
