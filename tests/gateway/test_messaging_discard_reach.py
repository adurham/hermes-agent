"""Messaging ``/discard`` reaches every lost turn it promises to (review #141357, GW-10).

Slack has no native ``/discard`` (its slash cap), so the pause notice must name ``/hermes discard``;
a conversation brought back by ``/resume`` after a restart is not in the owner's live map, and
``/discard`` must still find its lost turn. Real runner dispatch and SQLite; transport stubbed."""


def test_slack_pause_notice_names_the_registered_umbrella_command():
    from types import SimpleNamespace
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.session_contract import SessionRef
    from gateway.session_ingress import pause_notice
    from hermes_cli.commands_platforms import slack_native_slashes, slack_subcommand_map
    assert 'discard' not in [row[0].lstrip('/') for row in slack_native_slashes()]
    assert slack_subcommand_map().get('discard')
    for platform, expected in ((Platform.SLACK, '/hermes discard'), (Platform.TELEGRAM, 'Send /discard')):
        live = SimpleNamespace(source=SessionSource(platform=platform, chat_id='D42'), pause_notified=False)
        notice = pause_notice(SimpleNamespace(sessions={'s': live}), SessionRef('p', 's'), 'unknown_execution')
        assert expected in notice, notice
