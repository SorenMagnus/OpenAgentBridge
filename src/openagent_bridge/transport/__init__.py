"""Transport adapters for external controller interaction."""

from openagent_bridge.transport.manual import ManualFileTransport
from openagent_bridge.transport.ssh_cli import SSHCommandWrapper
from openagent_bridge.transport.http import LocalHTTPTransport

__all__ = ["ManualFileTransport", "SSHCommandWrapper", "LocalHTTPTransport"]
