"""Kill real processes at transaction/network boundaries, then reopen durable state."""
import subprocess
import sys

from agents_over_telegram.core import Core
from agents_over_telegram.credentials import read_private_json, write_private_json
from agents_over_telegram.transport import FileFakeTransport


def setup_state(tmp_path):
    state = tmp_path/"state"
    transport = FileFakeTransport(state/"fake-transport.json")
    core = Core(state, transport)
    core.register_account(900001,"aot_test_bot")
    client = core.create_client("Alpha")["client_id"]
    pairing = core.connection_begin(client)
    transport.get_me()
    update = {"update_id":1,"message":{"message_id":1,"chat":{"id":-100,"type":"supergroup"},
        "from":{"id":101,"is_bot":False},"text":pairing["command"]}}
    data=read_private_json(state/"fake-transport.json")
    data["updates"]=[update]
    write_private_json(state/"fake-transport.json",data,overwrite=True)
    core.collect()
    core.confirm_pairing(pairing["pairing_id"])
    update2={"update_id":2,"message":{"message_id":2,"chat":{"id":-100,"type":"supergroup"},
        "from":{"id":101,"is_bot":False},"text":"durable work"}}
    data=read_private_json(state/"fake-transport.json")
    data["updates"].append(update2)
    write_private_json(state/"fake-transport.json",data,overwrite=True)
    return state,client


def child(state, client, code):
    prologue = '''
import os,sys
from pathlib import Path
from agents_over_telegram.core import Core
from agents_over_telegram.transport import FileFakeTransport
state=Path(sys.argv[1]); client=sys.argv[2]
transport=FileFakeTransport(state/'fake-transport.json')
core=Core(state,transport)
'''
    return subprocess.run([sys.executable,"-c",prologue+code,str(state),client],capture_output=True,text=True,timeout=10)


def test_process_death_before_intake_commit_replays_entire_update(tmp_path):
    state,client=setup_state(tmp_path)
    result=child(state,client, '''
original=core._route_update
def crash(*args):
    original(*args)
    os._exit(71)
core._route_update=crash
core.collect()
''')
    assert result.returncode==71
    restarted=Core(state,FileFakeTransport(state/"fake-transport.json"))
    assert restarted.poll(client)["deliveries"]==[]
    assert restarted.collect()["updates"]==1
    assert [m["text"] for m in restarted.poll(client)["deliveries"]]==["durable work"]
    offsets=read_private_json(state/"fake-transport.json")["poll_offsets"]
    assert offsets[-2:]==[2,2]


def test_process_death_after_intake_commit_keeps_delivery_and_cursor(tmp_path):
    state,client=setup_state(tmp_path)
    result=child(state,client,"core.collect()\nos._exit(72)\n")
    assert result.returncode==72
    restarted=Core(state,FileFakeTransport(state/"fake-transport.json"))
    assert restarted.collect()["updates"]==0
    assert [m["text"] for m in restarted.poll(client)["deliveries"]]==["durable work"]
    assert read_private_json(state/"fake-transport.json")["poll_offsets"][-1]==3


def test_process_death_after_remote_acceptance_is_unknown_and_never_retried(tmp_path):
    state,client=setup_state(tmp_path)
    result=child(state,client, '''
original=transport.send_message
def crash(*args,**kwargs):
    original(*args,**kwargs)
    os._exit(73)
transport.send_message=crash
core.send(client,'accepted before process death','crash-intent')
''')
    assert result.returncode==73
    restarted=Core(state,FileFakeTransport(state/"fake-transport.json"))
    status=restarted.send(client,"accepted before process death","crash-intent")
    assert status["status"]=="unknown"
    restarted.flush(client)
    assert len(read_private_json(state/"fake-transport.json")["sent"])==1
