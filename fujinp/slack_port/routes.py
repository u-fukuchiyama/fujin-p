# SPDX-FileCopyrightText: 2024-2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of FUJIN-P.
# Copyright (C) 2024-2026 Toyoaki Nishida
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

"""
Slack_port の管理画面（admin だけ）

  GET  /                        管理画面
  GET  /api/status              Bot の一覧，チャンネルの一覧と参加状況
  POST /api/channels/save       チャンネルの追加・更新
  POST /api/channels/<id>/delete
  POST /api/try_post            試し投稿（Bot と番号を選んで）
  GET  /api/peek                最近のメッセージを読む（Bot と番号を選んで）
  GET  /channel/<ch_no>         チャンネルの内容を読み取ってダウンロードする画面
  GET  /api/history             履歴を1ページ読む（cursor でページ送り）
  GET  /api/replies             スレッドの返信を1ページ読む
  GET  /text/<ch_no>            Claude 用の目次（月ごとの件数と各月のページの URL）
  GET  /text/<ch_no>/<YYYY-MM>  Claude 用の本文（その月の発言と返信を全部）
  GET  /text/<ch_no>/thread/<ts>  Claude 用のスレッド1本
  GET  /api/apps                登録されたアプリの一覧（2026-10-18）
  POST /api/apps/<app>/log      ログの ON/OFF（JSON {on}）
  GET  /apps/<app>              そのアプリの投稿記録（チャンネルと DM を別の枠で）
  GET  /return_to_fujin
"""
import datetime
import logging
import re

import mysql.connector
from flask import render_template, request, jsonify, abort, url_for
from markupsafe import Markup, escape

from auth import redirect_to_dashboard
from db import DatabaseConfig
from decorators import admin_required

from . import slack_port_bp
from . import api

JST = datetime.timezone(datetime.timedelta(hours=9))


def _db():
    return mysql.connector.connect(**DatabaseConfig.default())


def _now():
    return datetime.datetime.now(JST).replace(tzinfo=None)


def _xhr():
    if request.headers.get('X-Requested-With') != 'fetch':
        abort(400)


@slack_port_bp.route('/return_to_fujin')
def return_to_fujin():
    return redirect_to_dashboard()


@slack_port_bp.route('/')
@admin_required
def index():
    return render_template('slack_port/index.html')


@slack_port_bp.route('/api/status')
@admin_required
def api_status():
    refresh = request.args.get('refresh') == '1'
    bots = api.bots(refresh=refresh)
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM slack_port_channels ORDER BY ch_no")
        chans = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    out = []
    for c in chans:
        row = {k: c[k] for k in ('id', 'ch_no', 'label', 'channel_id', 'note')}
        row['members'] = {}
        if request.args.get('check') == '1':
            for b in bots:
                if b['ok']:
                    m = api.membership(b['key'], c['ch_no'])
                    row['members'][b['key']] = m
                    if m.get('name'):
                        row['slack_name'] = m['name']
        out.append(row)
    return jsonify(ok=True, bots=bots, channels=out)


@slack_port_bp.route('/api/channels/save', methods=['POST'])
@admin_required
def api_channel_save():
    _xhr()
    d = request.get_json(silent=True) or {}
    try:
        ch_no = int(d.get('ch_no'))
        if ch_no < 1:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify(ok=False, error='番号は1以上の整数で入れてください')
    label = (d.get('label') or '').strip()
    channel_id = (d.get('channel_id') or '').strip()
    note = (d.get('note') or '').strip() or None
    if not label:
        return jsonify(ok=False, error='表示名を入れてください')
    if not re.fullmatch(r'[CG][A-Z0-9]{6,}', channel_id):
        return jsonify(ok=False, error='チャンネル ID は C か G で始まる英数字です')
    cid = d.get('id')
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id FROM slack_port_channels WHERE ch_no=%s", (ch_no,))
        dup = cur.fetchone()
        if dup and (not cid or int(cid) != dup['id']):
            return jsonify(ok=False, error=f'{ch_no} 番はすでに使われています')
        now = _now()
        if cid:
            cur.execute("UPDATE slack_port_channels SET ch_no=%s, label=%s, channel_id=%s, "
                        "note=%s, updated_at=%s WHERE id=%s",
                        (ch_no, label, channel_id, note, now, cid))
        else:
            cur.execute("INSERT INTO slack_port_channels (ch_no, label, channel_id, note, "
                        "created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s)",
                        (ch_no, label, channel_id, note, now, now))
        conn.commit()
        cur.close()
        return jsonify(ok=True)
    except mysql.connector.Error as e:
        conn.rollback()
        return jsonify(ok=False, error=f'保存できません: {e.msg}')
    finally:
        conn.close()


@slack_port_bp.route('/api/channels/<int:cid>/delete', methods=['POST'])
@admin_required
def api_channel_delete(cid):
    _xhr()
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM slack_port_channels WHERE id=%s", (cid,))
        conn.commit()
        cur.close()
        return jsonify(ok=True)
    finally:
        conn.close()


@slack_port_bp.route('/api/try_post', methods=['POST'])
@admin_required
def api_try_post():
    _xhr()
    d = request.get_json(silent=True) or {}
    try:
        ch_no = int(d.get('ch_no'))
    except (TypeError, ValueError):
        return jsonify(ok=False, error='チャンネルを選んでください')
    return jsonify(api.post(d.get('bot') or '', ch_no, d.get('text') or ''))


@slack_port_bp.route('/api/peek')
@admin_required
def api_peek():
    try:
        ch_no = int(request.args.get('ch_no'))
    except (TypeError, ValueError):
        return jsonify(ok=False, error='チャンネルを選んでください', messages=[])
    return jsonify(api.read(request.args.get('bot') or '', ch_no,
                            limit=request.args.get('limit', 20)))


# ════════════════════════════════════════════════════════════
# チャンネルの内容の読み取り（ページ送りは画面側が回す）
# ════════════════════════════════════════════════════════════

def _day_to_epoch(s, end=False):
    """'YYYY-MM-DD'（JST）→ UNIX 時刻．end=True ならその日の終わり（翌日0時）"""
    if not s:
        return None
    try:
        d = datetime.datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=JST)
    except ValueError:
        return None
    if end:
        d += datetime.timedelta(days=1)
    return int(d.timestamp())


@slack_port_bp.route('/channel/<int:ch_no>')
@admin_required
def channel_page(ch_no):
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT ch_no, label, channel_id, note FROM slack_port_channels "
                    "WHERE ch_no=%s", (ch_no,))
        ch = cur.fetchone()
        cur.close()
    finally:
        conn.close()
    if not ch:
        abort(404)
    bots = [{'key': b['key'], 'name': b.get('name', '')} for b in api.bots() if b['ok']]
    return render_template('slack_port/channel.html', ch=ch, bots=bots)


@slack_port_bp.route('/api/history')
@admin_required
def api_history():
    try:
        ch_no = int(request.args.get('ch_no'))
    except (TypeError, ValueError):
        return jsonify(ok=False, error='チャンネルを選んでください', messages=[])
    return jsonify(api.history_page(
        request.args.get('bot') or '', ch_no,
        cursor=request.args.get('cursor') or None,
        oldest=_day_to_epoch(request.args.get('from')),
        latest=_day_to_epoch(request.args.get('to'), end=True),
        limit=request.args.get('limit', 200)))


@slack_port_bp.route('/api/replies')
@admin_required
def api_replies():
    try:
        ch_no = int(request.args.get('ch_no'))
    except (TypeError, ValueError):
        return jsonify(ok=False, error='チャンネルを選んでください', messages=[])
    return jsonify(api.replies(
        request.args.get('bot') or '', ch_no, request.args.get('thread_ts') or '',
        cursor=request.args.get('cursor') or None))


# ════════════════════════════════════════════════════════════
# Claude 用のテキストページ（オールに URL 文書として登録し，MCP で読ませる）
#   目次 → 月ごとのページ → （読み残したスレッドだけ）スレッドのページ
#   どれもサーバ側で Slack のページ送りを回し，1枚の HTML で返す
# ════════════════════════════════════════════════════════════

_YM_RE = re.compile(r'(\d{4})-(\d{2})')


def _ext(endpoint, **kw):
    """外から読める完全な URL（Claude が次に読む先として本文に書き出す）"""
    path = url_for(endpoint, **kw)
    proto = request.headers.get('X-Forwarded-Proto', request.scheme)
    return f'{proto}://{request.host}{path}'


def _channel_row(ch_no):
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT ch_no, label, channel_id, note FROM slack_port_channels "
                    "WHERE ch_no=%s", (ch_no,))
        ch = cur.fetchone()
        cur.close()
        return ch
    finally:
        conn.close()


def _body(m):
    """発言の本文（Markdown のまま）を，改行を保った安全な HTML にする"""
    return Markup('<br>'.join(str(escape(line)) for line in (m.get('md') or '').split('\n')))


def _text_common(ch_no):
    ch = _channel_row(ch_no)
    if not ch:
        abort(404)
    bot, err = api.pick_bot(ch_no, request.args.get('bot') or None)
    return ch, bot, err


@slack_port_bp.route('/text/<int:ch_no>')
@admin_required
def text_index(ch_no):
    ch, bot, err = _text_common(ch_no)
    ctx = dict(kind='index', ch=ch, bot=bot, error=err, now=_now().strftime('%Y-%m-%d %H:%M'),
               index_url=_ext('slack_port.text_index', ch_no=ch_no))
    if not err:
        ctx['meta'] = api.channel_meta(bot, ch_no)
        sc = api.scan_months(bot, ch_no)
        for d in sc['months']:
            d['url'] = _ext('slack_port.text_month', ch_no=ch_no, ym=d['ym'])
        ctx['scan'] = sc
        ctx['error'] = sc.get('error')
    return render_template('slack_port/text.html', **ctx)


@slack_port_bp.route('/text/<int:ch_no>/<ym>')
@admin_required
def text_month(ch_no, ym):
    m = _YM_RE.fullmatch(ym)
    if not m or not 1 <= int(m.group(2)) <= 12:
        abort(404)
    ch, bot, err = _text_common(ch_no)
    y, mo = int(m.group(1)), int(m.group(2))
    start = datetime.datetime(y, mo, 1, tzinfo=JST)
    end = datetime.datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=JST)
    prev_ym = (start - datetime.timedelta(days=1)).strftime('%Y-%m')
    ctx = dict(kind='month', ch=ch, bot=bot, error=err, ym=ym, now=_now().strftime('%Y-%m-%d %H:%M'),
               index_url=_ext('slack_port.text_index', ch_no=ch_no),
               prev_url=_ext('slack_port.text_month', ch_no=ch_no, ym=prev_ym),
               next_url=_ext('slack_port.text_month', ch_no=ch_no, ym=end.strftime('%Y-%m')),
               body=_body)
    if not err:
        res = api.period_messages(bot, ch_no, int(start.timestamp()), int(end.timestamp()))
        ctx['res'] = res
        ctx['error'] = res.get('error')
        ctx['pending'] = [_ext('slack_port.text_thread', ch_no=ch_no, ts=t)
                          for t in res.get('pending_threads', [])]
    return render_template('slack_port/text.html', **ctx)


@slack_port_bp.route('/text/<int:ch_no>/thread/<ts>')
@admin_required
def text_thread(ch_no, ts):
    if not re.fullmatch(r'\d+\.\d+', ts):
        abort(404)
    ch, bot, err = _text_common(ch_no)
    ctx = dict(kind='thread', ch=ch, bot=bot, error=err, ts=ts, now=_now().strftime('%Y-%m-%d %H:%M'),
               index_url=_ext('slack_port.text_index', ch_no=ch_no), body=_body)
    if not err:
        res = api.thread_messages(bot, ch_no, ts)
        ctx['res'] = res
        ctx['error'] = res.get('error')
    return render_template('slack_port/text.html', **ctx)


# ════════════════════════════════════════════════════════════
# 登録されたアプリと投稿の記録（2026-10-18）
# ════════════════════════════════════════════════════════════

@slack_port_bp.route('/api/apps')
@admin_required
def api_apps():
    try:
        rows = api.notify_apps()
    except Exception as e:
        return jsonify(ok=False, error=f'アプリの表を読めません: {e}', apps=[])
    out = [{'app_name': r['app_name'], 'display_name': r['display_name'] or r['app_name'],
            'default_bot': r['default_bot'] or '', 'log_on': bool(r['log_on']), 'loaded': r['loaded'],
            'n_log': r['n_log'], 'n_dm': r['n_dm'],
            'registered_at': _fmt(r['registered_at']), 'seen_at': _fmt(r['seen_at']),
            'last_used_at': _fmt(r['last_used_at'])} for r in rows]
    return jsonify(ok=True, apps=out, error=None)


@slack_port_bp.route('/api/apps/<app_name>/log', methods=['POST'])
@admin_required
def api_app_log(app_name):
    _xhr()
    d = request.get_json(silent=True) or {}
    try:
        n = api.notify_set_log(app_name, bool(d.get('on')))
    except Exception as e:
        return jsonify(ok=False, error=f'切り替えられません: {e}')
    if not n:
        return jsonify(ok=False, error='そのアプリは登録されていません')
    return jsonify(ok=True)


@slack_port_bp.route('/apps/<app_name>')
@admin_required
def app_log_page(app_name):
    from . import notify as _n
    row = _n.app(app_name)
    if not row:
        abort(404)
    try:
        page = max(0, int(request.args.get('page', 0)))
    except ValueError:
        page = 0
    per = 100
    ch_rows, ch_total = _n.log_rows(app_name, 'channel', per, page * per)
    dm_rows, dm_total = _n.log_rows(app_name, 'dm', per, page * per)
    return render_template('slack_port/app_log.html', a=row, ch_rows=ch_rows, ch_total=ch_total,
                           dm_rows=dm_rows, dm_total=dm_total, page=page, per=per)


def _fmt(t):
    return t.strftime('%Y-%m-%d %H:%M') if t else ''

