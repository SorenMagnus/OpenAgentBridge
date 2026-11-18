"""Channel adapters for external messaging and chat platform integration."""

from openagent_bridge.channels.base import BaseChannelAdapter
from openagent_bridge.channels.router import ChannelRouter

__all__ = ["BaseChannelAdapter", "ChannelRouter"]
