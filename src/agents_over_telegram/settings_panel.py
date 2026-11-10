"""The owner's settings panel: a Telegram HTML widget in the private chat with the shared bot.

One panel per owner covers every connection that owner has on the bot, with a
picker at the top. The widget draws from its chat state and asks the bot with
requests of a few bytes; the bot checks every request
(core.Core._route_widget_query): only the owner, in their private chat, on the
latest panel AOT sent them, for a connection they still own, can change a
setting, and only to one of the values listed here. Settings that warn need a
second tap, with the warning the terminal shows.

Widgets currently work only in the private chat with the bot (Bot API 10.4).
"""
from __future__ import annotations

import json
import re

# (setting, label, values, value labels). The order is part of the request
# format, so only append to it; SECTIONS groups them for drawing.
SETTINGS = (
    ('delivery_filter', 'Deliver', ('all', 'addressed'), ('All', 'Addressed')),
    ('capture_context', 'Others as context', (False, True), ('Off', 'On')),
    ('accept_media', 'Media', (False, True), ('Off', 'On')),
    ('reaction_wake', 'Reactions wake', ('own_messages', 'all', 'none'), ('Own', 'All', 'None')),
    ('wake_interval', 'Batch notices', (0, 60, 300, 600, 1800), ('Off', '1m', '5m', '10m', '30m')),
    ('wake_exempt', 'Skip batching', ('addressed,owner', 'addressed', 'owner', 'none'),
     ('Both', 'Addr', 'Owner', 'None')),
    ('owner_notify', 'Notices to you', (False, True), ('Off', 'On')),
    ('model_judge', 'Model judge', (False, True), ('Off', 'On')),
    ('auto_allow', 'Auto-allow', (False, True), ('Off', 'On')),
    ('keep_alive', 'Keep-alive', (False, True), ('Off', 'On')),
    ('approvals_in', 'Ask approvals in', ('private', 'group'), ('Private', 'Group')),
)
SECTIONS = (('Messages', (0, 1, 2)), ('Wake-ups', (3, 4, 5, 9)), ('Access', (8, 7, 6, 10)))

# Turning these on needs a second tap, with the warning the terminal shows.
WARNINGS = {
    'model_judge': 'With the addressed filter, recent messages from this group are sent to the configured '
                   'decision model to decide whether a message is meant for this agent.',
    'auto_allow': 'Everyone who posts in this group, people and bots, becomes an allowed sender and can give '
                  'this agent work, including anyone who joins later through an invite link.',
    'keep_alive': 'An idle Claude Code session is woken for one short turn per idle day, which reads the '
                  'whole conversation without the prompt cache.',
}

MAX_CONNECTIONS = 8


def key(client_id):
    """The short form of a client id used in requests: its 24 hex digits."""
    return client_id.removeprefix('agent_')


def _line(text, limit):
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def values(settings):
    """The index of each current value, -1 when it is not one of the presets."""
    return [options.index(settings.get(name)) if settings.get(name) in options else -1
            for name, _, options, _ in SETTINGS]


def view(client_id, settings):
    """What the widget draws for one connection."""
    return {'k': key(client_id), 'v': values(settings), 'wi': settings.get('wake_interval')}


def state(connections, client_id, settings):
    """The panel's chat state: the owner's connections and the selected one's values.

    ``connections`` are rows with client_id, label and title; at most
    MAX_CONNECTIONS are listed, which keeps the state far below 1 KB.
    """
    listed = [[_line(c['label'], 24), _line(c['title'], 40), key(c['client_id'])]
              for c in connections[:MAX_CONNECTIONS]]
    return json.dumps({'c': listed, **view(client_id, settings)}, separators=(',', ':'), ensure_ascii=False)


def parse(data):
    """(kind, client key, setting name, value) for a request, or None when it is not valid.

    ``v:<key>`` asks for one connection's values (when the panel appears or a
    connection is picked). ``s:<key>:<i>:<j>`` sets setting i to value j, and
    ``c:<key>:<i>:<j>`` is the second tap that confirms a setting that warns.
    """
    if not isinstance(data, str):
        return None
    match = re.fullmatch(r'v:([0-9a-f]{24})', data)
    if match:
        return 'v', match.group(1), None, None
    match = re.fullmatch(r'([sc]):([0-9a-f]{24}):([0-9]{1,2}):([0-9]{1,2})', data)
    if match is None:
        return None
    i, j = int(match.group(3)), int(match.group(4))
    if i >= len(SETTINGS) or j >= len(SETTINGS[i][2]):
        return None
    name, _, options, _ = SETTINGS[i]
    return match.group(1), match.group(2), name, options[j]


# The widget only draws its state and sends requests; the bot decides.
HTML = '''<style>
body{margin:0;padding:8px 10px 10px;font-size:13px}
.hd{font-weight:600;font-size:14px}.sub{color:var(--tg-theme-hint-color);font-size:12px;margin:0 0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pk{display:flex;gap:4px;flex-wrap:wrap;margin:0 0 2px}
.pk button{border:0;border-radius:12px;padding:3px 9px;font-size:12px;background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-text-color)}
.pk button.on{background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color)}
.sec{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--tg-theme-section-header-text-color,var(--tg-theme-hint-color));margin:8px 0 2px}
.row{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:2px 8px;padding:3px 0}
.lb{flex:1 1 90px;min-width:0}.lb i{color:var(--tg-theme-hint-color);font-style:normal}
.seg{display:flex;flex:0 0 auto;gap:1px;padding:2px;border-radius:8px;background:var(--tg-theme-secondary-bg-color)}
.seg button{border:0;border-radius:6px;padding:3px 7px;font-size:12px;background:transparent;color:var(--tg-theme-text-color)}
.seg button.on{background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color);font-weight:600}
#w{display:none;border-radius:10px;padding:8px 10px;margin:2px 0 6px;background:var(--tg-theme-secondary-bg-color)}
#w p{margin:0 0 6px;font-size:12px;color:var(--tg-theme-destructive-text-color)}#w .seg{background:none;gap:6px;padding:0}
#w .seg button{background:var(--tg-theme-bg-color);padding:5px 12px}#w .seg button.on{background:var(--tg-theme-button-color)}
#m{font-size:11px;color:var(--tg-theme-hint-color);min-height:14px;margin-top:2px}
</style><div class="hd" id="t">Settings</div><div class="sub" id="g"></div><div class="pk" id="p"></div>
<div id="w"><p id="wt"></p><div class="seg"><button class="on" id="wy">Turn on</button><button id="wn">Cancel</button></div></div>
<div id="s"></div><div id="m"></div>
<script>
const W=window.TelegramWidget,S=__SETTINGS__,G=__SECTIONS__;let st=null,busy=0;
const $=i=>document.getElementById(i),el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
function msg(t){$('m').textContent=t||''}
function load(){try{st=JSON.parse(W.chatData)}catch(e){st=null}draw()}
function cur(){return st&&st.c&&st.c.find(c=>c[2]==st.k)}
function draw(){const s=$('s'),p=$('p'),w=$('w');w.style.display='none';document.body.appendChild(w);s.innerHTML='';p.innerHTML='';const c=cur();
if(!c){$('t').textContent='Settings';$('g').textContent='No connected agents.';return}
$('t').textContent=c[0];$('g').textContent=c[1];
if(st.c.length>1)st.c.forEach(x=>{const b=el('button',x[2]==st.k?'on':'',x[0]);b.onclick=()=>ask('v:'+x[2]);p.appendChild(b)});
G.forEach(([h,ix])=>{s.appendChild(el('div','sec',h));ix.forEach(i=>{const r=el('div','row'),l=el('div','lb',S[i][0]);
if(st.v[i]<0&&i==4&&st.wi!=null)l.appendChild(el('i','',' ('+st.wi+'s)'));r.appendChild(l);const g=el('div','seg');
S[i][1].forEach((n,j)=>{const b=el('button',j==st.v[i]?'on':'',n);b.onclick=()=>ask('s:'+st.k+':'+i+':'+j,r);g.appendChild(b)});
r.appendChild(g);s.appendChild(r)})})}
function ask(d,row){if(busy)return;busy=1;msg('Saving…');W.request(d).then(a=>{busy=0;let r={};
try{r=JSON.parse(a.result||'{}')}catch(e){}
if(r.w){const w=$('w');$('wt').textContent=r.w;if(row)row.parentNode.insertBefore(w,row.nextSibling);w.style.display='block';
$('wy').onclick=()=>{w.style.display='none';ask('c'+d.slice(1),row)};$('wn').onclick=()=>{w.style.display='none';msg('')};msg('');return}
if(r.s){st=Object.assign(st||{},r.s);draw()}msg(r.e?'Not changed: '+r.e:d[0]=='v'?'':'Saved.')},e=>{busy=0;msg('Not changed: '+e.code)})}
W.onEvent('chatDataChanged',load);load();W.ready();
if(st&&st.k)W.request('v:'+st.k).then(a=>{try{const r=JSON.parse(a.result||'{}');if(r.s&&!busy){Object.assign(st,r.s);draw()}}catch(e){}},()=>{});
</script>'''


def html():
    """The widget's HTML, with the setting labels and sections for drawing."""
    labels = [[label, list(names)] for _, label, _, names in SETTINGS]
    sections = [[title, list(indexes)] for title, indexes in SECTIONS]
    return (HTML.replace('__SETTINGS__', json.dumps(labels, separators=(',', ':'), ensure_ascii=False))
                .replace('__SECTIONS__', json.dumps(sections, separators=(',', ':'), ensure_ascii=False)))
