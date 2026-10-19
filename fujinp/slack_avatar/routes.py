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
Slack_avatar の画面（admin だけ）

  GET  /                          アバターの一覧と登録
  GET  /a/<id>                    アバターの端末
  GET  /api/avatars               アバターの一覧と，Slack_port の Bot・チャンネル
  POST /api/avatars/save          アバターの追加・更新
  POST /api/avatars/<id>/delete
  GET  /api/a/<id>/messages       チャンネルの最近のメッセージ（Slack_port.read）
  POST /api/a/<id>/post           チャンネルへの投稿（Slack_port.notify．2026-10-18）
  POST /api/a/<id>/dm             DM（Slack_port.notify_dm．JSON {email, text}．2026-10-18）
  GET  /return_to_fujin
"""
import datetime

import mysql.connector
from flask import render_template, request, jsonify, abort, session

from auth import redirect_to_dashboard
from db import DatabaseConfig
from decorators import admin_required

from . import slack_avatar_bp

JST = datetime.timezone(datetime.timedelta(hours=9))


def _port():
    """Slack_port の api．無ければ None"""
    try:
        from ..slack_port import api
        return api
    except Exception:
        try:
            from fujinp.slack_port import api
            return api
        except Exception:
            return None


def _db():
    return mysql.connector.connect(**DatabaseConfig.default())


def _now():
    return datetime.datetime.now(JST).replace(tzinfo=None)


def _xhr():
    if request.headers.get('X-Requested-With') != 'fetch':
        abort(400)


def _avatar(aid):
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM slack_avatar_avatars WHERE id=%s", (aid,))
        a = cur.fetchone()
        cur.close()
        return a
    finally:
        conn.close()


APP_NAME = 'slack_avatar'   # Slack_port に登録する名前（__init__.py と同じ）
NO_PORT = 'Slack_port が導入されていません．先に Slack_port を取り込んでください'


@slack_avatar_bp.route('/return_to_fujin')
def return_to_fujin():
    return redirect_to_dashboard()


@slack_avatar_bp.route('/')
@admin_required
def index():
    return render_template('slack_avatar/index.html', has_port=_port() is not None)


@slack_avatar_bp.route('/a/<int:aid>')
@admin_required
def terminal(aid):
    a = _avatar(aid)
    if not a:
        abort(404)
    port = _port()
    ch_label = ''
    bot_name = ''
    if port:
        ch_label = next((c['label'] for c in port.channels() if c['no'] == a['ch_no']), '')
        bot_name = port.bot_info(a['bot_key']).get('name', '')
    return render_template('slack_avatar/terminal.html', a=a, ch_label=ch_label,
                           bot_name=bot_name, has_port=port is not None)


@slack_avatar_bp.route('/api/avatars')
@admin_required
def api_avatars():
    port = _port()
    conn = _db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, name, bot_key, ch_no, note, sort_order "
                    "FROM slack_avatar_avatars ORDER BY sort_order, id")
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    return jsonify(ok=True, avatars=rows,
                   bots=port.bots() if port else [],
                   channels=port.channels() if port else [],
                   error=None if port else NO_PORT)


@slack_avatar_bp.route('/api/avatars/save', methods=['POST'])
@admin_required
def api_avatar_save():
    _xhr()
    d = request.get_json(silent=True) or {}
    name = (d.get('name') or '').strip()
    bot = (d.get('bot_key') or '').strip()
    try:
        ch_no = int(d.get('ch_no'))
    except (TypeError, ValueError):
        return jsonify(ok=False, error='チャンネルを選んでください')
    if not name or not bot:
        return jsonify(ok=False, error='名前と Bot を入れてください')
    try:
        order = int(d.get('sort_order') or 0)
    except (TypeError, ValueError):
        order = 0
    note = (d.get('note') or '').strip() or None
    conn = _db()
    try:
        cur = conn.cursor()
        now = _now()
        if d.get('id'):
            cur.execute("UPDATE slack_avatar_avatars SET name=%s, bot_key=%s, ch_no=%s, note=%s, "
                        "sort_order=%s, updated_at=%s WHERE id=%s",
                        (name, bot, ch_no, note, order, now, d['id']))
        else:
            cur.execute("INSERT INTO slack_avatar_avatars (name, bot_key, ch_no, note, sort_order, "
                        "created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (name, bot, ch_no, note, order, now, now))
        conn.commit()
        cur.close()
        return jsonify(ok=True)
    except mysql.connector.Error as e:
        conn.rollback()
        return jsonify(ok=False, error=f'保存できません: {e.msg}')
    finally:
        conn.close()


@slack_avatar_bp.route('/api/avatars/<int:aid>/delete', methods=['POST'])
@admin_required
def api_avatar_delete(aid):
    _xhr()
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM slack_avatar_avatars WHERE id=%s", (aid,))
        conn.commit()
        cur.close()
        return jsonify(ok=True)
    finally:
        conn.close()


@slack_avatar_bp.route('/api/a/<int:aid>/messages')
@admin_required
def api_messages(aid):
    a = _avatar(aid)
    port = _port()
    if not a:
        return jsonify(ok=False, error='アバターが見つかりません', messages=[])
    if not port:
        return jsonify(ok=False, error=NO_PORT, messages=[])
    return jsonify(port.read(a['bot_key'], a['ch_no'], limit=request.args.get('limit', 40)))


@slack_avatar_bp.route('/api/a/<int:aid>/post', methods=['POST'])
@admin_required
def api_post(aid):
    _xhr()
    a = _avatar(aid)
    port = _port()
    if not a:
        return jsonify(ok=False, error='アバターが見つかりません')
    if not port:
        return jsonify(ok=False, error=NO_PORT)
    d = request.get_json(silent=True) or {}
    return jsonify(port.notify(APP_NAME, a['ch_no'], d.get('text') or '', bot=a['bot_key'],
                               sender=_sender()))


@slack_avatar_bp.route('/api/a/<int:aid>/dm', methods=['POST'])
@admin_required
def api_dm(aid):
    _xhr()
    a = _avatar(aid)
    port = _port()
    if not a:
        return jsonify(ok=False, error='アバターが見つかりません')
    if not port:
        return jsonify(ok=False, error=NO_PORT)
    d = request.get_json(silent=True) or {}
    return jsonify(port.notify_dm(APP_NAME, d.get('email') or '', d.get('text') or '', bot=a['bot_key'],
                                  sender=_sender()))


def _sender():
    return session.get('email') or session.get('user_name') or None
