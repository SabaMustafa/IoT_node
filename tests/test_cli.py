"""The CLI's argument parsing.

These exist because of a real bug: an `analyse` positional was declared before
the `command` positional, and argparse — matching consecutive optional
positionals left to right — silently assigned the subcommand name to it. Every
subcommand then fell through to the default, so `iot-node record` started the
full daemon instead of recording. Nothing else in the suite would have caught
it, because each subcommand's own function was fine.
"""

from __future__ import annotations

import pytest

from iot_node.main import build_parser

SUBCOMMANDS = [
    "run", "sample", "record", "analyse", "audio-test",
    "scan", "upload", "config", "status",
]


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_each_subcommand_binds_to_command_not_files(name):
    args = build_parser().parse_args([name])
    assert args.command == name
    assert args.files == []


def test_no_arguments_defaults_to_run():
    args = build_parser().parse_args([])
    assert args.command == "run"


def test_analyse_takes_file_arguments():
    args = build_parser().parse_args(["analyse", "a.wav", "b.wav"])
    assert args.command == "analyse"
    assert [p.name for p in args.files] == ["a.wav", "b.wav"]


def test_record_options_are_parsed():
    args = build_parser().parse_args(["record", "--seconds", "30", "--rate", "96000"])
    assert args.command == "record"
    assert args.seconds == 30
    assert args.rate == 96000


def test_options_may_precede_the_subcommand():
    args = build_parser().parse_args(["--verbose", "status"])
    assert args.command == "status"
    assert args.verbose is True


def test_config_path_option():
    args = build_parser().parse_args(["-c", "/etc/iot.yaml", "config"])
    assert args.command == "config"
    assert str(args.config) == "/etc/iot.yaml"


def test_an_unknown_subcommand_is_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["frobnicate"])
