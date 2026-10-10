# SPDX-FileCopyrightText: 2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of FUJIN-P.
# Copyright (C) 2026 Toyoaki Nishida
#
# FUJIN-P is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# FUJIN-P is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with FUJIN-P.  If not, see <https://www.gnu.org/licenses/>.
#
# Source: https://github.com/u-fukuchiyama/fujin-p

"""オール：MCP（Streamable HTTP）の窓口と道具

Claude のカスタムコネクタから POST /all_portal/mcp で呼ばれ，JSON-RPC 2.0 に JSON で答える（SSE は使わない）．
窓口は OAuth のアクセストークンを求め，トークンの持ち主を主体として core.level_of() で段を判定する．
Web 画面と同じ判定なので，AI に見えるのはその人が画面で見られるものと同じである．
アクト（AI からの登録）は，持ち主がアクトの段を持ち，かつそのポータルで「AI のアクトを許す」が
入っているときだけ許す．道具の呼び出しはすべて all_log に via='mcp' で記録する．
"""

import json
import logging

from flask import Response, request

from . import all_portal_bp as bp
from .core import (KINDS, LEVEL_LABEL, ancestors, children_of, descendant_ids, cursor, item_row, item_visible, items_of, level_of, visible_items, mcp_who, portal_row,
                   portals_for, add_request, save_item, user_active, user_info, write_log,
                   subtree_base, in_subtree, subtree_entries)
from .oauth import bearer_grant, cors, json_resp, unauthorized
from . import urldoc

logger = logging.getLogger(__name__)

SERVER_NAME = 'fujinp-all'
SERVER_VERSION = '0.1'
PROTOCOL_VERSIONS = ['2025-06-18', '2025-03-26', '2024-11-05']
MAX_CHARS_DEFAULT = 20000
MAX_CHARS_LIMIT = 50000
TERM_MAX = 100

INSTRUCTIONS = (
    'オール（FUJIN-P）は，機関・グループ・個人・システムごとのポータルに文書の束を置き，'
    '人間と AI に同じ中身を渡す窓口です．見えるもの・できることは，接続を許可した人の FUJIN-P での権限'
    '（閲覧・リクエスト・アクト）で決まります．'
    'まず list_portals で使えるポータルと自分の段を確かめ，list_items で文書の束を見て，read_item で読んでください．'
    'ポータル全体を読むときは read_portal が並んだ URL をまとめて読みます．URL は接続を許可した人の身元でたたくので，その人が画面で見るのと同じ中身が返ります．'
    '話題で探すときは search_items を使います．ポータルは入れ子になっていることがあり，list_items で type が portal の行は child_portal_id でたどれます．PDF の URL も本文を読めます．'
    'URL 文書（source が url）は，read_item のたびにその URL をたたいて返ってきた HTML をテキストにして返します．'
    '中身の追加や修正を頼みたいときは submit_request でリクエストを出します（リクエスト以上の段が要ります）．'
    'add_item はアクトの段があり，ポータルが AI のアクトを許しているときだけ使えます．'
    'list_items で subtree が true の行は「入口」で，その URL の配下にある同じサイトのページ（リンク先の詳細やファイルなど）を，'
    'read_url に URL を渡して読めます．入口の行と同じ公開範囲で読み，入口の配下でない URL は読めません．'
    '回答には，結果に含まれる url（オールの画面へのリンク）を出典として添えてください．'
)


def S(props=None, required=None):
    return {'type': 'object', 'properties': props or {}, 'required': required or [],
            'additionalProperties': False}


PAGING = {
    'offset': {'type': 'integer', 'minimum': 0, 'description': '読み始める文字位置（既定 0）'},
    'max_chars': {'type': 'integer', 'minimum': 1, 'maximum': MAX_CHARS_LIMIT,
                  'description': f'返す最大文字数（既定 {MAX_CHARS_DEFAULT}）'},
}

TOOLS = [
    {'name': 'list_portals', 'title': 'ポータルの一覧',
     'description': '接続している利用者（connected_as）と，使えるポータル（機関・グループ・個人・システム）の id・種類・名前・説明・文書の件数・自分の段を返す．',
     'inputSchema': S({'kind': {'type': 'string', 'enum': list(KINDS), 'description': '種類で絞る'}}),
     'annotations': {'readOnlyHint': True}},
    {'name': 'list_items', 'title': '文書の束',
     'description': ('ポータルの URL（文書）を順序数の順に返す．type が portal の行は子ポータルで，child_portal_id を list_items に渡すと中が見られる．'
                     'search_items は指定したポータルの下の子ポータルまで探す．'),
     'inputSchema': S({'portal_id': {'type': 'integer'}, 'bundle': {'type': 'string'}}, ['portal_id']),
     'annotations': {'readOnlyHint': True}},
    {'name': 'read_item', 'title': '文書を読む',
     'description': ('文書1件の題名・束・URL・備考と本文を返す．URL 文書はその場で URL をたたいて読む（読めなければ最後の写し）．'
                     '長いときは offset と max_chars で区切って読む．'),
     'inputSchema': S(dict({'id': {'type': 'integer'}}, **PAGING), ['id']),
     'annotations': {'readOnlyHint': True}},
    {'name': 'read_portal', 'title': 'ポータルをまとめて読む',
     'description': ('ポータルに並んだ URL を順序数の順にすべて読み，題名・URL・本文を1つのテキストにまとめて返す．'
                     'URL はその場で，接続を許可した人の身元でたたく（その人が画面で見るのと同じ中身になる）．'
                     '子ポータルの行は中まで読まず，題名と child_portal_id だけを示す．長いときは offset と max_chars で区切って読む．'),
     'inputSchema': S(dict({'portal_id': {'type': 'integer'}}, **PAGING), ['portal_id']),
     'annotations': {'readOnlyHint': True}},
    {'name': 'read_url', 'title': '入口の配下を読む',
     'description': ('list_items で subtree が true の行（入口）の URL の配下にある，同じサイトのページを読む．'
                     'url は /app_share/source/finder のようなパスか，同じサイトの https の URL．'
                     '入口の行が見える人だけが読め，接続を許可した人の身元で URL をたたく．長いときは offset と max_chars で区切って読む．'),
     'inputSchema': S(dict({'url': {'type': 'string'}}, **PAGING), ['url']),
     'annotations': {'readOnlyHint': True}},
    {'name': 'search_items', 'title': '文書を探す',
     'description': ('使えるポータルの文書の題名と本文を固定文字列で探す．1行が1つのまとまりで，行の中は空白で区切る．'
                     'mode が and_or（既定）なら行の中はどれか・行どうしは全部，or_and ならその逆．最大100件．'),
     'inputSchema': S({'query': {'type': 'string'},
                       'mode': {'type': 'string', 'enum': ['and_or', 'or_and']},
                       'portal_id': {'type': 'integer', 'description': 'ポータルで絞る'}}, ['query']),
     'annotations': {'readOnlyHint': True}},
    {'name': 'submit_request', 'title': 'リクエストを出す',
     'description': ('ポータルの担当（アクトの段を持つ人）にリクエスト（文書の追加・修正・質問など）を出す．'
                     'リクエスト以上の段が要る．出したリクエストは画面の「リクエスト」に並ぶ．'),
     'inputSchema': S({'portal_id': {'type': 'integer'}, 'text': {'type': 'string'}}, ['portal_id', 'text']),
     'annotations': {'readOnlyHint': False, 'destructiveHint': False}},
    {'name': 'add_item', 'title': '文書を登録する',
     'description': ('ポータルに文書を1件登録する．アクトの段があり，ポータルが AI のアクトを許しているときだけ使える．'
                     '既存の文書は書き換えない．'),
     'inputSchema': S({'portal_id': {'type': 'integer'}, 'title': {'type': 'string'},
                       'body': {'type': 'string'}, 'url': {'type': 'string'},
                       'bundle': {'type': 'string', 'description': '束の名前（無ければ束なし）'}},
                      ['portal_id', 'title']),
     'annotations': {'readOnlyHint': False, 'destructiveHint': False}},
]


class ToolError(Exception):
    pass


def link(path):
    return 'https://' + request.host + path


def as_int(v, name, required=False):
    if v in (None, ''):
        if required:
            raise ToolError(f'{name} を指定してください．')
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ToolError(f'{name} は整数で指定してください．')


def paging(args):
    try:
        offset = max(int(args.get('offset') or 0), 0)
        mc = int(args.get('max_chars') or MAX_CHARS_DEFAULT)
    except (TypeError, ValueError):
        raise ToolError('offset と max_chars は整数で指定してください．')
    return offset, min(max(mc, 1), MAX_CHARS_LIMIT)


def page(text, offset, max_chars, title):
    total = len(text)
    part = text[offset:offset + max_chars]
    end = offset + len(part)
    return (f'# {title}\n（全{total}文字のうち {offset}〜{end} 文字目'
            f"{'．続きは offset=' + str(end) + ' で読める' if end < total else '．ここで終わり'}）\n\n" + part)


def need(who, pid, min_level):
    """ポータルと段を確かめて (portal, level) を返す．足りなければ ToolError"""
    p = portal_row(pid)
    lv = level_of(p, who) if p else 0
    if p and lv < 1 and min_level <= 1 and any(item_visible(r, who, 0) for r in items_of(pid)):
        return p, 0     # ポータルの閲覧は無いが，見える URL がある
    if not p or lv < 1:
        raise ToolError(f'id {pid} のポータルは見つからないか，使う権限がありません．')
    if lv < min_level:
        raise ToolError(f'このポータルでのあなたの段は「{LEVEL_LABEL[lv]}」で，'
                        f'この操作には「{LEVEL_LABEL[min_level]}」以上が要ります．')
    return p, lv


# ── 道具 ──

def t_list_portals(who, args):
    kind = args.get('kind')
    u = user_info(who.uid)
    rows = [{'id': p['id'], 'kind': p['kind'], 'name': p['name'], 'description': p['description'] or '',
             'items': p['n_items'], 'my_level': LEVEL_LABEL.get(p['level'], 'URL ごと'),
             'ai_act': bool(p['ai_act']) and p['level'] >= 3,
             'url': link(f"/all_portal/p/{p['id']}")}
            for p in portals_for(who) if not kind or p['kind'] == kind]
    out = {'connected_as': {'user_id': who.uid, 'name': u['name'], 'email': u['email'],
                            'category': u['category'] or '（不明）'},
           'portals': rows}
    if not rows:
        out['note'] = ('この接続の利用者に見えるポータルはありません．接続を許可したアカウントが意図したもの（admin など）か，'
                       'ポータルと URL の公開範囲を確かめてください．アカウントを替えるときは Claude のコネクタを切断し，'
                       '目的のアカウントで FUJIN-P にログインしてから連携し直します．')
    return out


def t_list_items(who, args):
    pid = as_int(args.get('portal_id'), 'portal_id', True)
    p, lv = need(who, pid, 1)
    b = args.get('bundle')
    rows = [r for r in visible_items(pid, who, lv) if b is None or (r['bundle'] or '') == str(b)]
    kids = children_of(pid)
    return {'portal': p['name'], 'kind': p['kind'],
            'path': ' ／ '.join([a['name'] for a in ancestors(p)] + [p['name']]), 'my_level': LEVEL_LABEL.get(lv, 'URL ごと'),
            'items': [{'id': r['id'], 'bundle': r['bundle'] or '', 'title': r['title'],
                       'source': r.get('source') or 'text', 'source_url': r['url'] or '',
                       'type': 'portal' if kids.get(r['id']) else 'document',
                       'subtree': bool(r.get('subtree')) and subtree_base(r['url']) is not None,
                       'child_portal_id': kids[r['id']]['id'] if kids.get(r['id']) else None,
                       'chars': r['chars'] or 0, 'url': link(f"/all_portal/i/{r['id']}")} for r in rows]}


def t_read_item(who, args):
    iid = as_int(args.get('id'), 'id', True)
    it = item_row(iid)
    if not it:
        raise ToolError(f'id {iid} の文書はありません．')
    p, lv = need(who, it['portal_id'], 1)
    if not item_visible(it, who, lv):
        raise ToolError(f'id {iid} の文書はありません．')
    offset, mc = paging(args)
    head = (f"ポータル：{p['kind']}／{p['name']}\n束：{it['bundle'] or '（束なし）'}\n"
            f"出所：{(link(it['url']) if (it['url'] or '').startswith('/') else it['url'] or '')}\nurl：{link('/all_portal/i/' + str(iid))}\n"
            + (f"備考：{it['note']}\n" if it.get('note') else '') + '\n')
    if it.get('source') == 'url':
        try:
            body = urldoc.read_text(it['url'], who).strip() or '（URL は空のページを返しました）'
            head += '（この本文は今 URL をたたいて得たものです）\n\n'
        except urldoc.FetchError as e:
            body = (it['body'] or '').strip()
            when = it['fetched_at'].strftime('%Y-%m-%d %H:%M') if it.get('fetched_at') else ''
            head += (f'（URL を読めませんでした：{e}．以下は {when} の写しです）\n\n' if body
                     else f'（URL を読めませんでした：{e}．写しもありません）\n\n')
            body = body or '（本文なし）'
    else:
        body = (it['body'] or '').strip() or '（本文なし．出所の URL を利用者に示してください）'
    return page(head + body, offset, mc, it['title'])


def t_read_url(who, args):
    from urllib.parse import urlsplit
    raw = (args.get('url') or '').strip()
    if not raw:
        raise ToolError('url を指定してください．')
    parts = urlsplit(raw)
    if parts.scheme and parts.scheme not in ('http', 'https'):
        raise ToolError('http(s) の URL か，/ で始まるパスを指定してください．')
    if parts.netloc and parts.netloc.lower() != request.host.lower():
        raise ToolError('このサイト（' + request.host + '）のページだけを読めます．')
    path = parts.path or '/'
    if not path.startswith('/'):
        raise ToolError('/ で始まるパスを指定してください．')
    target = path + ('?' + parts.query if parts.query else '')
    # 入口：配下に含み，見えるもの．最も深い入口を使う
    best = None
    levels = {}
    for it in subtree_entries():
        base = subtree_base(it['url'])
        if not base or not in_subtree(base, path):
            continue
        pid = it['portal_id']
        if pid not in levels:
            try:
                levels[pid] = need(who, pid, 1)
            except ToolError:
                levels[pid] = None
        if not levels[pid] or not item_visible(it, who, levels[pid][1]):
            continue
        if best is None or len(base) > len(best[1]):
            best = (it, base, levels[pid][0])
    if not best:
        raise ToolError('この URL は，あなたが読める入口（subtree が true の行）の配下にありません．'
                        'list_items で subtree が true の行を確かめてください．')
    it, base, p = best
    try:
        text, final = urldoc.read_text_final(target, who)
    except urldoc.FetchError as e:
        raise ToolError(f'読めませんでした：{e}')
    fpath = urlsplit(final).path or '/'
    if not in_subtree(base, fpath):
        raise ToolError('この URL は入口の配下の外へ転送されたので，返しません．')
    offset, mc = paging(args)
    head = (f"入口：{p['kind']}／{p['name']} の「{it['title']}」（{link('/all_portal/i/' + str(it['id']))}）\n"
            f"出所：{link(target)}\n\n（この本文は今 URL をたたいて得たものです）\n\n")
    return page(head + (text.strip() or '（URL は空のページを返しました）'), offset, mc, target)


def item_text(it, who):
    """(本文, 注記)．URL 文書はその場でたたき，読めなければ最後の写し"""
    if it.get('source') != 'url':
        return (it.get('body') or '').strip() or '（本文なし）', ''
    try:
        return urldoc.read_text(it['url'], who).strip() or '（URL は空のページを返しました）', '今 URL をたたいて得た本文'
    except urldoc.FetchError as e:
        body = (it.get('body') or '').strip()
        when = it['fetched_at'].strftime('%Y-%m-%d %H:%M') if it.get('fetched_at') else ''
        return (body or '（本文なし）'), (f'URL を読めませんでした：{e}．{when} の写し' if body else f'URL を読めませんでした：{e}')


def t_read_portal(who, args):
    pid = as_int(args.get('portal_id'), 'portal_id', True)
    p, lv = need(who, pid, 1)
    offset, mc = paging(args)
    kids = children_of(pid)
    path = ' ／ '.join([a['name'] for a in ancestors(p)] + [p['name']])
    parts = [f"ポータル：{path}\nurl：{link('/all_portal/p/' + str(pid))}\n"]
    for n, it in enumerate(visible_items(pid, who, lv, with_body=True), 1):
        src = it['url'] or ''
        src = link(src) if src.startswith('/') else src
        head = f"\n==== {n}．{it['title']} ====\n出所：{src}\nurl：{link('/all_portal/i/' + str(it['id']))}\n"
        if it.get('note'):
            head += f"備考：{it['note']}\n"
        kid = kids.get(it['id'])
        if kid:
            parts.append(head + f"（子ポータル．中は list_items か read_portal に portal_id={kid['id']} を渡して読む）\n")
            continue
        body, note = item_text(it, who)
        parts.append(head + (f'（{note}）\n' if note else '') + '\n' + body + '\n')
    return page(''.join(parts), offset, mc, p['name'])


def like_pat(term):
    return '%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def parse_query(text):
    import re
    groups = []
    for line in (text or '').splitlines():
        terms = [t[:TERM_MAX] for t in re.split(r'[\s　]+', line.strip()) if t]
        if terms:
            groups.append(terms)
    return groups[:20]


def t_search_items(who, args):
    groups = parse_query(str(args.get('query') or ''))
    if not groups:
        raise ToolError('query を指定してください．')
    inner, outer = (' AND ', ' OR ') if args.get('mode') == 'or_and' else (' OR ', ' AND ')
    pid = as_int(args.get('portal_id'), 'portal_id')
    if pid is not None:
        roots = [pid]
    else:
        roots = [p['id'] for p in portals_for(who)]
    allowed = {}
    for root in roots:
        for x in [root] + descendant_ids(root):
            if x in allowed:
                continue
            px = portal_row(x)
            if not px:
                continue
            px['level'] = level_of(px, who)
            if px['level'] >= 1 or any(item_visible(r, who, 0) for r in items_of(x)):
                allowed[x] = px
    if pid is not None and pid not in allowed:
        raise ToolError(f'id {pid} のポータルは見つからないか，使う権限がありません．')
    if not allowed:
        return {'total': 0, 'results': []}
    parts, params = [], []
    for g in groups:
        conds = []
        for t in g:
            conds.append('(title LIKE %s OR body LIKE %s)')
            params += [like_pat(t)] * 2
        parts.append('(' + inner.join(conds) + ')')
    ids = list(allowed)
    with cursor() as (cur, conn):
        cur.execute("SELECT id, portal_id, bundle, title, body, access_policy, access_groups FROM all_items WHERE portal_id IN (" +
                    ','.join(['%s'] * len(ids)) + ") AND (" + outer.join(parts) + ") ORDER BY portal_id, id LIMIT 100",
                    ids + params)
        rows = cur.fetchall()
    terms = [t for g in groups for t in g]
    out = []
    rows = [r for r in rows if item_visible(r, who, allowed[r['portal_id']]['level'])]
    for r in rows:
        body = r['body'] or ''
        i = min([body.find(t) for t in terms if body.find(t) >= 0] or [0])
        snip = body[max(i - 60, 0):i + 120]
        out.append({'id': r['id'], 'portal': allowed[r['portal_id']]['name'], 'bundle': r['bundle'] or '',
                    'title': r['title'], 'snippet': snip, 'url': link(f"/all_portal/i/{r['id']}")})
    return {'total': len(out), 'results': out}


def t_submit_request(who, args):
    pid = as_int(args.get('portal_id'), 'portal_id', True)
    text = str(args.get('text') or '').strip()
    if not text:
        raise ToolError('text を書いてください．')
    if len(text) > 5000:
        raise ToolError('text は5000文字までです．')
    p, lv = need(who, pid, 2)
    rid = add_request(who, pid, '［Claude 経由］\n' + text)
    return {'ok': True, 'request_id': rid, 'portal': p['name'],
            'note': 'リクエストを出しました．担当（アクトの段を持つ人）が画面で受け付けます．'}


def t_add_item(who, args):
    pid = as_int(args.get('portal_id'), 'portal_id', True)
    p, lv = need(who, pid, 3)
    if not p['ai_act']:
        raise ToolError('このポータルは AI からのアクト（登録）を許していません．submit_request でリクエストしてください．')
    title = str(args.get('title') or '').strip()[:500]
    if not title:
        raise ToolError('title を書いてください．')
    url = str(args.get('url') or '').strip() or None
    if url and not url.startswith(('http://', 'https://')):
        raise ToolError('url は http:// か https:// で始めてください．')
    vals = {'bundle': (str(args.get('bundle') or '').strip()[:200] or None), 'title': title, 'url': url,
            'body': str(args.get('body') or '') or None, 'note': '［Claude 経由で登録］', 'sort_order': None}
    iid = save_item(who, pid, vals)
    return {'ok': True, 'id': iid, 'url': link(f'/all_portal/i/{iid}')}


HANDLERS = {'list_portals': t_list_portals, 'list_items': t_list_items, 'read_item': t_read_item,
            'read_portal': t_read_portal, 'read_url': t_read_url,
            'search_items': t_search_items, 'submit_request': t_submit_request, 'add_item': t_add_item}


# ── JSON-RPC ──

def rpc_result(msg_id, result):
    return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}


def rpc_error(msg_id, code, message):
    return {'jsonrpc': '2.0', 'id': msg_id, 'error': {'code': code, 'message': message}}


def text_result(text, is_error=False):
    return {'content': [{'type': 'text', 'text': text}], 'isError': is_error}


def handle(msg, grant):
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or 'method' not in msg:
        return rpc_error(msg.get('id') if isinstance(msg, dict) else None, -32600, 'Invalid Request')
    if 'id' not in msg:
        return None
    method, msg_id, params = msg['method'], msg['id'], msg.get('params') or {}
    if method == 'initialize':
        asked = params.get('protocolVersion')
        return rpc_result(msg_id, {
            'protocolVersion': asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVER_NAME, 'title': 'オール（FUJIN-P）', 'version': SERVER_VERSION},
            'instructions': INSTRUCTIONS,
        })
    if method == 'ping':
        return rpc_result(msg_id, {})
    if method == 'tools/list':
        return rpc_result(msg_id, {'tools': TOOLS})
    if method == 'tools/call':
        name = params.get('name')
        fn = HANDLERS.get(name)
        if not fn:
            return rpc_error(msg_id, -32602, f'Unknown tool: {name}')
        args = params.get('arguments') if isinstance(params.get('arguments'), dict) else {}
        who = mcp_who(grant['user_id'])
        try:
            if not user_active(who.uid):
                raise ToolError('この利用者は FUJIN-P で使えない状態です．')
            out = fn(who, args)
            write_log(who, 'MCP:' + name, args.get('portal_id'), args.get('id'),
                      {k: v for k, v in args.items() if k not in ('body', 'text')})
            return rpc_result(msg_id, text_result(
                out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1, default=str)))
        except ToolError as e:
            write_log(who, 'MCP:' + name + '（拒否）', None, None, {'error': str(e)})
            return rpc_result(msg_id, text_result(str(e), True))
        except Exception:
            logger.exception('all_portal tools/call %s failed', name)
            return rpc_result(msg_id, text_result('内部エラーが起きました．', True))
    return rpc_error(msg_id, -32601, f'Method not found: {method}')


@bp.route('/mcp', methods=['GET', 'POST', 'DELETE', 'OPTIONS'])
def mcp():
    if request.method == 'OPTIONS':
        return cors(Response(status=204))
    grant = bearer_grant()
    if not grant:
        return unauthorized()
    if user_info(grant['user_id']).get('category') != 'admin':
        # Claude からの接続は admin だけ（以前に許可した admin 以外の接続もここで止める）
        return json_resp({'error': 'forbidden', 'error_description': 'Claude からの接続は admin だけが使えます'}, 403)
    if request.method != 'POST':
        return cors(Response(status=405, headers={'Allow': 'POST'}))
    try:
        body = json.loads(request.get_data(as_text=True) or 'null')
    except ValueError:
        return json_resp(rpc_error(None, -32700, 'Parse error'), 400)
    if isinstance(body, list):
        replies = [r for r in (handle(m, grant) for m in body) if r is not None]
        return json_resp(replies) if replies else cors(Response(status=202))
    reply = handle(body, grant)
    return json_resp(reply) if reply is not None else cors(Response(status=202))
