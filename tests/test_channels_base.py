"""Unit tests for Channel Router and Adapter skeletons."""

from __future__ import annotations

from openagent_bridge.channels.cli_channel import CLIChannelAdapter
from openagent_bridge.channels.discord_bot import DiscordBotAdapter
from openagent_bridge.channels.feishu_bot import FeishuBotAdapter
from openagent_bridge.channels.router import ChannelRouter
from openagent_bridge.channels.slack_bot import SlackBotAdapter


def test_channel_router_and_skeletons():
    router = ChannelRouter()

    cli = CLIChannelAdapter()
    discord = DiscordBotAdapter(bot_token="test_token")
    feishu = FeishuBotAdapter(app_id="app_1", app_secret="secret_1")
    slack = SlackBotAdapter(bot_token="xoxb-test")

    router.register(cli)
    router.register(discord)
    router.register(feishu)
    router.register(slack)

    assert router.get("cli") == cli
    assert router.get("discord") == discord
    assert router.get("feishu") == feishu
    assert router.get("slack") == slack
    assert router.get("unknown") is None

    # Lifecycle start and stop
    router.start_all()
    assert cli.is_running is True
    assert discord.is_running is True
    assert feishu.is_running is True
    assert slack.is_running is True

    # Dispatch messages
    assert router.dispatch_message("discord", {"channel_id": "123"}, "Hello Discord") is True
    assert router.dispatch_message("feishu", {"chat_id": "456"}, "Hello Feishu") is True
    assert router.dispatch_message("slack", {"channel_id": "789"}, "Hello Slack") is True
    assert router.dispatch_message("nonexistent", {}, "test") is False

    # Dispatch task update
    task = {"id": "t_multi", "source_channel": "cli", "reply_target": {}}
    assert router.dispatch_task_update(task, "Running step 2") is True
    assert len(cli.sent_messages) == 1

    # Dispatch approval
    approval = {"id": "app_multi", "action": "MERGE"}
    assert router.dispatch_approval_request(approval, task) is True
    assert len(cli.sent_messages) == 2

    router.stop_all()
    assert cli.is_running is False
    assert discord.is_running is False
