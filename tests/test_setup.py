"""Isolated setup/profile contracts: no live Telegram or global host configuration."""
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tomllib

import pytest

from agents_over_telegram import cli
from agents_over_telegram.core import Core
from agents_over_telegram.credentials import CredentialError, read_private_json, write_private_json
from agents_over_telegram.onboarding import PROFILE_NAME, human_status
from agents_over_telegram.transport import FakeTransport, make_transport


def invoke(*argv):
    return cli.run(cli.parser().parse_args([str(x) for x in argv]))


def prepare(tmp_path, label='Alpha', project=None, **kwargs):
    state = kwargs.pop('state', tmp_path / 'state')
    project = project or tmp_path / label.lower()
    args = ['--state-dir', state, '--fake', 'setup', '--label', label, '--project-dir', project, '--prepare-only']
    if kwargs.get('client_file'):
        args += ['--client-file', kwargs['client_file']]
    return invoke(*args)


def append_pair(result, *, sender=123, chat=-100, thread=None):
    path = Path(result['state_dir']) / 'fake-transport.json'
    fake = read_private_json(path)
    index = max([u['update_id'] for u in fake['updates']] + [0]) + 1
    message = {'message_id': index, 'chat': {'id': chat, 'type': 'supergroup', 'title': 'Test group'},
               'from': {'id': sender, 'is_bot': False}, 'text': result['pairing_command']}
    if thread is not None:
        message['message_thread_id'] = thread
    fake['updates'].append({'update_id': index, 'message': message})
    write_private_json(path, fake, overwrite=True)


def connect(result):
    append_pair(result)
    state = result['state_dir']
    invoke('--state-dir', state, '--fake', 'collect')
    return invoke('--state-dir', state, '--fake', 'pair-confirm', '--pairing-id', result['pairing_id'], '--confirm')


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_prepare_only_is_idempotent_does_not_collect_and_writes_no_secrets(tmp_path):
    first = prepare(tmp_path)
    private = read_private_json(Path(first['client_file']))
    before = {p: p.read_bytes() for p in Path(first['project_dir']).rglob('*') if p.is_file()}
    second = prepare(tmp_path)
    assert second['client_id'] == first['client_id']
    assert second['pairing'] == first['pairing']
    assert before == {p: p.read_bytes() for p in Path(first['project_dir']).rglob('*') if p.is_file()}
    assert read_private_json(Path(first['state_dir']) / 'fake-transport.json')['poll_offsets'] == []
    assert read_private_json(Path(first['state_dir']) / 'fake-transport.json')['sent'] == []
    for payload in [json.dumps(first), *(p.read_text() for p in before)]:
        assert private['client_secret'] not in payload
        assert 'client_secret' not in payload
    with sqlite3.connect(Path(first['state_dir']) / 'state.sqlite3') as db:
        assert db.execute('select count(*) from clients').fetchone()[0] == 1
    assert shlex.split(first['next_step'].split('then run: ', 1)[1])[0].startswith('/')


def test_two_projects_share_bot_but_have_distinct_identity_and_fake_mcp_flags(tmp_path):
    alpha, beta = prepare(tmp_path), prepare(tmp_path, 'Beta')
    assert alpha['state_dir'] == beta['state_dir']
    assert alpha['client_id'] != beta['client_id']
    for result in (alpha, beta):
        entry = tomllib.loads(Path(result['config_file']).read_text())['mcp_servers']['telegram']
        assert entry['args'] == ['--state-dir', result['state_dir'], '--fake', 'mcp', '--client-file', result['client_file']]
        assert entry['command'] == str(Path(sys.executable).absolute().parent / 'aot')
    with sqlite3.connect(Path(alpha['state_dir']) / 'state.sqlite3') as db:
        assert db.execute('select count(*) from account').fetchone()[0] == 1


def test_existing_unrelated_toml_is_preserved(tmp_path):
    project = tmp_path / 'project'
    (project / '.codex').mkdir(parents=True)
    original = '# keep this comment\nmodel_reasoning_effort = "low"\n[mcp_servers.other]\ncommand = "other-command"\n'
    (project / '.codex/config.toml').write_text(original)
    result = prepare(tmp_path, project=project)
    assert Path(result['config_file']).read_text().startswith(original)
    assert tomllib.loads(Path(result['config_file']).read_text())['mcp_servers']['other']['command'] == 'other-command'


@pytest.mark.parametrize('contents', ['[mcp_servers.telegram]\ncommand="different"\n', 'not valid toml ['])
def test_config_conflict_precedes_all_account_or_client_mutation(tmp_path, contents):
    project = tmp_path / 'project'
    (project / '.codex').mkdir(parents=True)
    path = project / '.codex/config.toml'
    path.write_text(contents)
    with pytest.raises(CredentialError):
        prepare(tmp_path, project=project)
    assert path.read_text() == contents
    assert not (tmp_path / 'state').exists()
    assert not (project / PROFILE_NAME).exists()


def test_setup_reuses_existing_client_without_rotation(tmp_path):
    state = tmp_path / 'state'
    invoke('--state-dir', state, '--fake', 'account-register')
    path = state / 'clients' / 'old-alpha.json'
    old = invoke('--state-dir', state, '--fake', 'client-create', '--label', 'Alpha', '--client-file', path)
    secret_bytes = path.read_bytes()
    result = prepare(tmp_path, client_file=path)
    assert result['client_id'] == old['client_id']
    assert path.read_bytes() == secret_bytes
    assert prepare(tmp_path)['client_id'] == old['client_id']


def test_connected_rerun_preserves_connection_pause_and_no_intake(tmp_path):
    first = prepare(tmp_path)
    connect(first)
    invoke('--state-dir', first['state_dir'], '--fake', 'pause', '--client-file', first['client_file'])
    fake_path = Path(first['state_dir']) / 'fake-transport.json'
    before = fake_path.read_bytes()
    rerun = prepare(tmp_path)
    assert rerun['connected'] and rerun['connection']['paused']
    assert rerun['client_id'] == first['client_id']
    assert fake_path.read_bytes() == before
    assert 'pairing_command' not in rerun


def test_exact_project_short_commands_and_no_parent_scope_bleed(tmp_path, monkeypatch):
    result = prepare(tmp_path)
    connect(result)
    project = Path(result['project_dir'])
    monkeypatch.chdir(project)
    assert invoke('status')['client_id'] == result['client_id']
    assert invoke('pause')['paused']
    assert not invoke('resume')['paused']
    assert invoke('history')['context_only']
    child = project / 'nested'
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(CredentialError, match='project_profile_missing_run_setup'):
        invoke('status')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CredentialError, match='project_profile_missing_run_setup'):
        invoke('status')


@pytest.mark.parametrize('extra,error', [
    (['--state-dir', '/tmp/wrong-state'], 'project_profile_state_mismatch'),
    (['status', '--client-file', '/tmp/wrong-client'], 'project_profile_client_mismatch'),
])
def test_explicit_scope_mismatch_never_switches_profile(tmp_path, monkeypatch, extra, error):
    result = prepare(tmp_path)
    monkeypatch.chdir(result['project_dir'])
    args = extra if extra[0] == 'status' else extra + ['status']
    with pytest.raises(CredentialError, match=error):
        invoke(*args)


def test_fake_flag_cannot_switch_live_profile(tmp_path, monkeypatch):
    result = prepare(tmp_path)
    path = Path(result['profile_file'])
    profile = json.loads(path.read_text())
    profile['mode'] = 'live'
    path.write_text(json.dumps(profile))
    monkeypatch.chdir(result['project_dir'])
    with pytest.raises(CredentialError, match='project_profile_mode_mismatch'):
        invoke('--fake', 'status')
    with pytest.raises(CredentialError, match='state_transport_mode_mismatch'):
        invoke('status')


def test_setup_explicit_state_client_label_mismatch_rejected(tmp_path):
    result = prepare(tmp_path)
    for flags, code in [(['--label', 'Beta'], 'project_profile_label_mismatch'),
                        (['--client-file', str(tmp_path/'wrong.json')], 'project_profile_client_mismatch')]:
        with pytest.raises(CredentialError, match=code):
            invoke('setup', '--label', 'Alpha', '--project-dir', result['project_dir'], '--prepare-only', *flags)
    with pytest.raises(CredentialError, match='project_profile_state_mismatch'):
        invoke('--state-dir', tmp_path/'different', 'setup', '--label', 'Alpha', '--project-dir', result['project_dir'], '--prepare-only')


def test_copied_profile_cannot_authorize_another_directory(tmp_path, monkeypatch):
    result = prepare(tmp_path)
    other = tmp_path / 'other'
    other.mkdir()
    (other/PROFILE_NAME).write_bytes(Path(result['profile_file']).read_bytes())
    (other/PROFILE_NAME).chmod(0o600)
    monkeypatch.chdir(other)
    with pytest.raises(CredentialError, match='project_profile_scope_mismatch'):
        invoke('status')


def test_profile_client_file_swap_is_rejected(tmp_path, monkeypatch):
    a, b = prepare(tmp_path), prepare(tmp_path, 'Beta')
    write_private_json(Path(a['client_file']), read_private_json(Path(b['client_file'])), overwrite=True)
    monkeypatch.chdir(a['project_dir'])
    with pytest.raises(CredentialError, match='project_profile_client_mismatch'):
        invoke('status')
    with pytest.raises(CredentialError, match='project_profile_client_mismatch'):
        invoke('flush', '--client-file', a['client_file'])


def test_wizard_confirms_only_explicit_local_yes_and_reuses_observation(tmp_path, monkeypatch, capsys):
    result = prepare(tmp_path)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    responses = iter(['', 'no'])
    def answer():
        value = next(responses)
        if value == '':
            append_pair(result, sender=456, chat=-200, thread=7)
        return value
    monkeypatch.setattr('builtins.input', answer)
    declined = invoke('setup', '--label', 'Alpha', '--project-dir', result['project_dir'])
    assert not declined['connected']
    assert 'No destination was confirmed' in declined['next_step']
    captured = capsys.readouterr().err
    assert '"sender_id": 456' in captured and '"chat_id": -200' in captured
    fake_path = Path(result['state_dir']) / 'fake-transport.json'
    polls = read_private_json(fake_path)['poll_offsets']
    monkeypatch.setattr('builtins.input', lambda: 'yes')
    accepted = invoke('setup', '--label', 'Alpha', '--project-dir', result['project_dir'])
    assert accepted['connected']
    assert accepted['connection']['destination']['owner_id'] == 456
    assert accepted['connection']['destination']['thread_id'] == 7
    assert accepted['client_id'] == result['client_id']
    assert read_private_json(fake_path)['poll_offsets'] == polls


def test_cancelling_wizard_keeps_same_client_and_pending_pairing(tmp_path, monkeypatch):
    result = prepare(tmp_path)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda: 'cancel')
    with pytest.raises(KeyboardInterrupt):
        invoke('setup', '--label', 'Alpha', '--project-dir', result['project_dir'])
    assert prepare(tmp_path)['pairing'] == result['pairing']


def test_wizard_missing_update_returns_rerunnable_next_step(tmp_path, monkeypatch):
    result = prepare(tmp_path)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda: '')
    second = invoke('setup', '--label', 'Alpha', '--project-dir', result['project_dir'])
    assert not second['connected']
    assert second['pairing_id'] == result['pairing_id']
    assert 'not arrived yet' in second['next_step']


def test_interactive_setup_refuses_non_tty_before_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    with pytest.raises(CredentialError, match='private_terminal_required'):
        invoke('--state-dir', tmp_path/'state', '--fake', 'setup', '--label', 'Alpha', '--project-dir', tmp_path/'project')
    assert not (tmp_path/'state').exists()


def test_live_registration_keeps_token_private_and_reuses_without_transport(tmp_path, monkeypatch, capsys):
    token = '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_test_only'
    calls = []
    class LocalTransport(FakeTransport):
        def __init__(self, supplied):
            calls.append(supplied)
            super().__init__()
    monkeypatch.setattr(cli, 'TelegramTransport', LocalTransport)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: token)
    args = ['--state-dir', tmp_path/'state', 'setup', '--label', 'Alpha', '--project-dir', tmp_path/'project', '--prepare-only']
    with pytest.raises(CredentialError, match='receiver_handoff_confirmation_required'):
        invoke(*args)
    assert calls == []
    result = invoke(*args, '--receiver-handoff-confirmed')
    assert calls == [token]
    assert read_private_json(Path(result['state_dir'])/'credentials.json')['token'] == token
    assert invoke(*args)['client_id'] == result['client_id']
    assert calls == [token]
    output = capsys.readouterr()
    assert token not in output.out + output.err + json.dumps(result)
    assert all(token not in p.read_text() for p in Path(result['project_dir']).rglob('*') if p.is_file())
    entry = tomllib.loads(Path(result['config_file']).read_text())['mcp_servers']['telegram']
    assert '--fake' not in entry['args']


def test_setup_preserves_existing_webhook_and_never_saves_token(tmp_path, monkeypatch):
    token = '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_test_only'
    monkeypatch.setattr(cli, 'TelegramTransport', lambda _: FakeTransport(webhook='https://example.invalid/existing'))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli.getpass, 'getpass', lambda _: token)
    with pytest.raises(CredentialError, match='existing_webhook_refused'):
        invoke('--state-dir', tmp_path/'state', 'setup', '--label', 'Alpha', '--project-dir', tmp_path/'project', '--prepare-only', '--receiver-handoff-confirmed')
    assert not (tmp_path/'state/credentials.json').exists()
    assert not (tmp_path/'project'/PROFILE_NAME).exists()


def test_status_history_prepare_do_not_recover_or_send_outbox(tmp_path, monkeypatch):
    result = prepare(tmp_path)
    connect(result)
    transport = make_transport(Path(result['state_dir']), fake=True)
    core = Core(result['state_dir'], transport, recover_sends=False)
    fake_path = Path(result['state_dir'])/'fake-transport.json'
    fake = read_private_json(fake_path)
    fake['outcomes'] = ['crash']
    write_private_json(fake_path, fake, overwrite=True)
    with pytest.raises(SystemExit):
        core.send(result['client_id'], 'interrupted', 'key-interrupted')
    before = fake_path.read_bytes()
    monkeypatch.chdir(result['project_dir'])
    assert invoke('status')['outbox']['sending'] == 1
    assert invoke('history')['messages'][0]['status'] == 'sending'
    assert invoke('setup', '--label', 'Alpha', '--project-dir', result['project_dir'], '--prepare-only')['connection']['outbox']['sending'] == 1
    assert fake_path.read_bytes() == before


@pytest.mark.parametrize('kind', ['profile', 'config', 'config_folder'])
def test_symlink_project_files_fail_closed_before_state(tmp_path, kind):
    project, target = tmp_path/'project', tmp_path/'target'
    project.mkdir()
    if kind == 'config_folder':
        target.mkdir()
        (project/'.codex').symlink_to(target, target_is_directory=True)
    elif kind == 'profile':
        target.write_text('{}')
        (project/PROFILE_NAME).symlink_to(target)
    else:
        (project/'.codex').mkdir()
        target.write_text('')
        (project/'.codex/config.toml').symlink_to(target)
    with pytest.raises(CredentialError):
        prepare(tmp_path, project=project)
    assert not (tmp_path/'state').exists()


def test_new_private_state_cannot_be_inside_project(tmp_path):
    project = tmp_path/'project'
    with pytest.raises(CredentialError, match='private_state_must_be_outside_project'):
        prepare(tmp_path, project=project, state=project/'state')
    assert not project.exists()


def test_human_status_has_ids_escapes_title_and_no_transport(tmp_path, monkeypatch, capsys):
    result = prepare(tmp_path)
    connect(result)
    monkeypatch.chdir(result['project_dir'])
    value = invoke('status')
    value['destination']['title'] = 'bad\x1b[2J\nspoof'
    human = human_status(value)
    assert '\x1b' not in human
    assert '\\u001b' in human
    assert 'chat -100' in human and '123' in human
    monkeypatch.setattr(sys, 'argv', ['aot', 'status', '--human'])
    assert cli.main() == 0
    text = capsys.readouterr().out
    assert 'Alpha: connected' in text
    assert 'Delivery: on demand' in text


def test_setup_stdout_is_json_and_cancellation_has_fixed_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['aot','--state-dir',str(tmp_path/'state'),'--fake','setup','--label','Alpha','--project-dir',str(tmp_path/'project'),'--prepare-only'])
    assert cli.main() == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result['pairing']['command']
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda: (_ for _ in ()).throw(EOFError()))
    monkeypatch.setattr(sys, 'argv', ['aot','setup','--label','Alpha','--project-dir',result['project_dir']])
    assert cli.main() == 130
    output = capsys.readouterr()
    assert '"error":"cancelled"' in output.err
    assert output.out == ''


def test_matching_config_timeouts_preserved_and_env_override_rejected(tmp_path):
    result = prepare(tmp_path)
    path = Path(result['config_file'])
    original = path.read_text() + 'startup_timeout_sec = 20\ntool_timeout_sec = 90\n'
    path.write_text(original)
    assert prepare(tmp_path)['client_id'] == result['client_id']
    assert path.read_text() == original
    path.write_text(original + '[mcp_servers.telegram.env]\nAOT_CLIENT_FILE = "different"\n')
    with pytest.raises(CredentialError, match='project_mcp_config_conflict'):
        prepare(tmp_path)


def test_group_writable_profile_reference_rejected(tmp_path, monkeypatch):
    result = prepare(tmp_path)
    Path(result['profile_file']).chmod(0o660)
    monkeypatch.chdir(result['project_dir'])
    with pytest.raises(CredentialError, match='unsafe_project_config'):
        invoke('status')


def test_interactive_handoff_assertion_and_human_completion(tmp_path, monkeypatch, capsys):
    token = '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_test_only'
    calls = []
    class LocalTransport(FakeTransport):
        def __init__(self, supplied):
            calls.append(supplied)
            super().__init__()
    monkeypatch.setattr(cli, 'TelegramTransport', LocalTransport)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli.getpass, 'getpass', lambda _: token)
    monkeypatch.setattr('builtins.input', lambda: 'no')
    argv = ['aot', '--state-dir', str(tmp_path/'state'), 'setup', '--label', 'Alpha', '--project-dir', str(tmp_path/'project')]
    monkeypatch.setattr(sys, 'argv', argv)
    assert cli.main() == 1
    assert calls == []
    captured = capsys.readouterr()
    assert 'only receiver' in captured.err
    assert 'receiver_handoff_confirmation_required' in captured.err
    responses = iter(['yes', 'cancel'])
    monkeypatch.setattr('builtins.input', lambda: next(responses))
    assert cli.main() == 130
    assert calls == [token]
    assert token not in str(capsys.readouterr())
    # Once connected, interactive output is a concise status, not JSON.
    result = prepare(tmp_path/'second')
    connect(result)
    monkeypatch.setattr(sys, 'argv', ['aot','setup','--label','Alpha','--project-dir',result['project_dir']])
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert output.startswith('Alpha: connected\n')
    assert 'Next:' in output and 'profile_file' not in output


def test_project_flush_only_sends_its_client_and_legacy_global_scope_remains(tmp_path, monkeypatch):
    alpha, beta = prepare(tmp_path), prepare(tmp_path, 'Beta')
    connect(alpha)
    append_pair(beta, chat=-200)
    invoke('--state-dir', beta['state_dir'], '--fake', 'collect')
    invoke('--state-dir', beta['state_dir'], '--fake', 'pair-confirm', '--pairing-id', beta['pairing_id'], '--confirm')
    core = Core(alpha['state_dir'], make_transport(alpha['state_dir'], fake=True), recover_sends=False)
    monkeypatch.setattr(core, 'flush', lambda **kwargs: {'status':'queued', 'attempted':0})
    first = core.send(alpha['client_id'], 'alpha queued', 'alpha-key')
    second = core.send(beta['client_id'], 'beta queued', 'beta-key')
    monkeypatch.setattr(Core, 'BOT_INTERVAL', 0)
    monkeypatch.chdir(alpha['project_dir'])
    assert invoke('flush')['attempted'] == 1
    assert core.delivery_status(alpha['client_id'], first['outbox_id'])['status'] == 'sent'
    assert core.delivery_status(beta['client_id'], second['outbox_id'])['status'] == 'queued'
    monkeypatch.chdir(tmp_path)
    assert invoke('--state-dir', alpha['state_dir'], '--fake', 'flush')['attempted'] == 1
    assert core.delivery_status(beta['client_id'], second['outbox_id'])['status'] == 'sent'
