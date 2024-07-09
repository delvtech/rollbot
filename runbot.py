import asyncio
import io
import os
import re
import sys
from datetime import datetime
from typing import Union

import discord
import requests
from discord import CategoryChannel, TextChannel, VoiceChannel
from discord.ext import commands
from dotenv import dotenv_values

GuildChannel = Union[TextChannel, VoiceChannel, CategoryChannel]
ROBOTS_CHANNEL = 1077979151316828270
DATASUSSY_CHANNEL = 1139538470696669215

# pylint: disable=too-many-arguments
# ruff: noqa: D101, D102

# load env and check we have the keys we need
config = dotenv_values(".env")
assert config["DISCORD_BOT_TOKEN"], "DISCORD_BOT_TOKEN is not set in .env"
assert "ROLLBAR_SERVICE_BOTS_SEPOLIA_PAT" in config, "ROLLBAR_SERVICE_BOTS_SEPOLIA_PAT is not set in .env"
assert "ROLLBAR_SERVICE_BOTS_MAINNET_PAT" in config, "ROLLBAR_SERVICE_BOTS_MAINNET_PAT is not set in .env"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class Rollbot(commands.Bot):
    bg_task: dict[str, asyncio.Task] = {}
    rollbar_channels: dict[str, TextChannel] = {}
    rollbar_reported_ids: list[int] = []
    rollbar_reported_ids_testnet: list[int] = []

bot = Rollbot(command_prefix="$", intents=intents)

ROLLBAR_PAGES_TO_CHECK = 5
ROLLBAR_WAIT_SECONDS = 10
ROLLBAR_CHANNEL_IDS = {
    "critical": 1255561995508514916,
    "error": 1255562047433740388,
    "warning": 1256016800450347048,
    "info": 1256016871174443058,
    "testnet": 1256016939940184095,
}
def format_timestamp(timestamp):
    return discord.utils.format_dt(datetime.fromtimestamp(timestamp), style='R')

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

def concatenate_items(entries) -> str:
    return "\n".join([
        f"{entry['data']['level']:<8} - {entry['data']['environment']:<12} - {entry['data']['body']['message']['body']}"
        for entry in entries
    ])

def excluded(entry: dict, rollbar_exclusion_filter: list[str]) -> bool:
    message_body = entry["data"]["body"]["message"]["body"]
    return any(re.search(filter_entry.replace("*", ".*?"), message_body, re.DOTALL | re.IGNORECASE) for filter_entry in rollbar_exclusion_filter)

async def check_rollbar_item_backlog(testnet: bool = False):
    try:
        message = open(f"rollbar_{'testnet' if testnet else 'mainnet'}_message_id.csv", "r", encoding="utf-8").read()
        message = await (bot.rollbar_channels["testnet"] if testnet else bot.rollbar_channels["info"]).fetch_message(int(message))
    except Exception:
        print("Rollbar message not found, creating new one...", end="")
        message = await (bot.rollbar_channels["testnet"] if testnet else bot.rollbar_channels["info"]).send(f"Checking rollbar on {'testnet' if testnet else 'mainnet'}...")
        open(f"rollbar_{'testnet' if testnet else 'mainnet'}_message_id.csv", "w", encoding="utf-8").write(str(message.id))
        print("done.")
    while True:
        # Update exclusion filter once per report. This means we can add to it without having to restart the bot.
        preamble = f"check_rollbar_item_backlog::{'testnet' if testnet else 'mainnet'}:: "
        print(f"{preamble}Reading rollbar_exclusion_filter{'_testnet' if testnet else ''}.csv")
        exclusion_list = f"rollbar_exclusion_filter{'_testnet' if testnet else ''}.csv"
        if not os.path.exists(exclusion_list):
            with open(exclusion_list, "w", encoding="utf-8") as file:
                file.write("")
        with open(exclusion_list, "r", encoding="utf-8") as file:
            rollbar_exclusion_filter = file.read().splitlines()
        # Begin reporting entries
        print(f"{preamble}Starting while loop")
        try:
            entries_to_report = []
            keep_checking = True
            # Identify entries to report
            for page in range(1, ROLLBAR_PAGES_TO_CHECK + 1):
                print(f"{preamble}Checking page {page}")
                entries = check_rollbar_entries(page=page, testnet=testnet)
                for entry in entries:
                    print(f"{preamble}Checking item {entry['id']}")
                    if entry["id"] in (bot.rollbar_reported_ids_testnet if testnet else bot.rollbar_reported_ids):
                        print(f"{preamble}Item {entry['id']} has already been reported, BREAKING")
                        keep_checking = False
                        break
                    print(f"{preamble}Item {entry['id']} has not been reported yet, CONTINUING")
                    entries_to_report.append(entry)
                if not keep_checking:
                    break
            print(f"{preamble}Found {len(entries_to_report)} items to report")
            if not entries_to_report:
                print(f"{preamble}No items to report, BREAKING")
            # Report entries
            print(f"{preamble}Sending {len(entries_to_report)} items to report")
            for entry in entries_to_report[::-1]:  # Chronological order
                print(f"{entry=}")
                level = entry["data"]["level"].lower()
                if excluded(entry, rollbar_exclusion_filter):
                    print(f"EXCLUDED: filtered by rollbar_exclusion_filter{'_testnet' if testnet else ''}.csv")
                elif level not in bot.rollbar_channels:
                    print(f"EXCLUDED: {level} not in rollbar_channels")
                else:
                    channel = bot.rollbar_channels["testnet"] if testnet else bot.rollbar_channels[level]
                    print(f"{channel=}") 
                    await channel.send(embed=prepare_rollbar_entry(entry, testnet))
                (bot.rollbar_reported_ids_testnet if testnet else bot.rollbar_reported_ids).append(entry["id"])
                with open(f"rollbar_reported_ids{'_testnet' if testnet else ''}.csv", "a", encoding="utf-8") as file:
                    file.write(f"{entry['id']}\n")
        except Exception as e:
            print(f"Error checking Rollbar items: {e}")
        print(f"{preamble}Sleeping for {ROLLBAR_WAIT_SECONDS} seconds")
        await message.edit(content=f"{'Testnet' if testnet else 'Mainnet'} rollbar checked at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, next check in {ROLLBAR_WAIT_SECONDS} seconds")
        await asyncio.sleep(ROLLBAR_WAIT_SECONDS)

@bot.command()
async def rollbar(context):
    if context.channel.id not in [ROBOTS_CHANNEL, DATASUSSY_CHANNEL]:
        await context.send("This command can only be used in the 🤪︱data-sussy or 🤖︱ro-bots.")
        return
    try:
        # Get optional page parameter passed in by user
        page = int(context.message.content.split()[1]) if len(context.message.content.split()) > 1 else 1
        # Get optional testnet parameter passed in by user
        testnet = context.message.content.split()[2] == "testnet" if len(context.message.content.split()) > 2 else False
        entries = check_rollbar_entries(page=page, testnet=testnet)
        concatenated_items = concatenate_items(entries)
        bytes_data = concatenated_items.encode('utf-8')
        file = io.BytesIO(bytes_data)
        discord_file = discord.File(fp=file, filename="rollbar_items.txt")
        await context.send(f"Here are the Rollbar items on page {page}:", file=discord_file)
    except Exception:
        await context.send("Failed to rollbar")
        return

def load_reported_ids(filename):
    if not os.path.exists(filename):
        open(filename, "w", encoding="utf-8").close()
        return []
    with open(filename, "r", encoding="utf-8") as file:
        return [int(line.strip()) for line in file]

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

    # Start background tasks
    print("Starting rollbar check loop...", end="")
    if "NETWORK" in config and config["NETWORK"] is not None:
        if config["NETWORK"].lower() in ["mainnet", "both"]:
            bot.bg_task["rollbar"] = bot.loop.create_task(check_rollbar_item_backlog(testnet=False))
        if config["NETWORK"].lower() in ["testnet", "both"]:
            bot.bg_task["rollbar_testnet"] = bot.loop.create_task(check_rollbar_item_backlog(testnet=True))
        if config["NETWORK"].lower() not in ["testnet", "mainnet", "both"]:
            print(f"Unknown network: {config['NETWORK']}. Please use 'testnet', 'mainnet', or 'both'")
    print("done.")

@bot.event
async def on_resume():
    print("Resuming...", end="")
    if "rollbar" in bot.bg_task:
        bot.bg_task["rollbar"].cancel()
        bot.bg_task["rollbar"] = bot.loop.create_task(check_rollbar_item_backlog(testnet=False))
    if "rollbar_testnet" in bot.bg_task:
        bot.bg_task["rollbar_testnet"].cancel()
        bot.bg_task["rollbar_testnet"] = bot.loop.create_task(check_rollbar_item_backlog(testnet=True))
    print("done.")

# Run the bot
config["NETWORK"] = sys.argv[1] if len(sys.argv) > 1 else "testnet"
print(f"{config['NETWORK']=}")
bot.run(config["DISCORD_BOT_TOKEN"])
