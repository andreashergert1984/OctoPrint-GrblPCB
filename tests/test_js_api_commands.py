"""Every API command the frontend sends must be known to the plugin."""
import pathlib
import re

from octoprint_grblpcb import GrblPcbPlugin

JS = pathlib.Path(__file__).resolve().parents[1] / "octoprint_grblpcb" / "static" / "js" / "grblpcb.js"


def _commands_sent_by_js():
    text = JS.read_text(encoding="utf-8")
    sent = set(re.findall(r'simpleApiCommand\(\s*"grblpcb"\s*,\s*"(\w+)"', text))
    sent |= set(re.findall(r'_sendCommand\(\s*"(\w+)"', text))
    return sent


def test_javascript_only_sends_commands_the_plugin_accepts():
    plugin = GrblPcbPlugin()
    known = set(plugin.get_api_commands())
    sent = _commands_sent_by_js()
    assert "machine_check" in sent  # the regex really finds the calls
    assert sent - known == set()


def test_every_command_has_a_dispatch_handler():
    plugin = GrblPcbPlugin()
    for command in plugin.get_api_commands():
        assert hasattr(plugin, f"_handle_{command}"), command
