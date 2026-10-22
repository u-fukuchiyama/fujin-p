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
まいあし (MaiAshi) ルート（2026-10-20 第1段）

  図書館      … 文書アーカイブなどに置いた文書への目次．行は題名・説明・URL・並び順だけ
  配布ファイル … 導入スクリプト（data_for_distribution/ の .py）と組み立て道具 build_installer.py

閲覧はログインした人全員．目次の編集は admin と「まいあしインストラクター」グループの所属者．
"""
import datetime
import logging
import os

import mysql.connector
from flask import abort, jsonify, render_template, request, send_from_directory, session

from auth import redirect_to_dashboard
from db import DatabaseConfig

from . import migration_assistant

# まいあしが所有するテーブル（SQL では必ずこの定数を使う．アプシャは *_TABLE 定数から見つける）
LIBRARY_TABLE = 'migration_assistant_library'

# 教材を作り，図書館の目次を編集できる人のグループ（「ユーザとグループ」で登録する）
INSTRUCTOR_GROUP = 'まいあしインストラクター'

HERE = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(HERE, 'data_for_distribution')
BUILDER_NAME = 'build_installer.py'

JST = datetime.timezone(datetime.timedelta(hours=9), 'JST')


def get_jst_now():
    """JST の naive datetime（DBがUTCのため，NOW() に頼らず明示的に書く）"""
    return datetime.datetime.now(JST).replace(tzinfo=None)


def get_db_connection():
    return mysql.connector.connect(**DatabaseConfig.default())


# =============================================================================
# 権限
# =============================================================================

def _ug(name):
    """「ユーザとグループ」の公開API を遅延で取り出す（無ければ None）"""
    try:
        from fujinp.user_groups import utils
        return getattr(utils, name, None)
    except Exception:
        return None


def _in_group_by_sql(user_id, group_name):
    """公開API が無いサイト向けの代替：直接メンバーを有効期間つきで見る"""
    now = get_jst_now()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT 1 FROM user_group_memberships m
            JOIN user_groups g ON g.id = m.group_id
            WHERE m.user_id = %s AND g.name = %s
              AND (m.valid_from IS NULL OR m.valid_from <= %s)
              AND (m.valid_until IS NULL OR m.valid_until >= %s)
            LIMIT 1
        """, (user_id, group_name, now, now))
        hit = cur.fetchone() is not None
        cur.close()
        return hit
    except Exception as e:
        logging.error("migration_assistant _in_group_by_sql: %s", e)
        return False
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


def is_instructor(user_id=None):
    """admin，または「まいあしインストラクター」の所属者か"""
    user_id = user_id or session.get('user_id')
    if not user_id:
        return False
    if session.get('user_category') == 'admin':
        return True
    names_fn = _ug('get_user_group_names')
    if names_fn is not None:
        try:
            return INSTRUCTOR_GROUP in (names_fn(user_id) or [])
        except Exception as e:
            logging.warning("migration_assistant get_user_group_names: %s", e)
    return _in_group_by_sql(user_id, INSTRUCTOR_GROUP)


def _need_login_json():
    return jsonify({'success': False, 'error': 'ログインが切れています．再ログインしてください'}), 401


def _need_instructor_json():
    return jsonify({'success': False,
                    'error': 'この操作はまいあしインストラクターだけが行えます'}), 403


# =============================================================================
# 配布ファイル
# =============================================================================

def _dist_files():
    """配布するファイルの一覧（名前 → 実パス）．.md は文書アーカイブ側に移したので配らない"""
    out = {}
    if os.path.isdir(DIST_DIR):
        for name in sorted(os.listdir(DIST_DIR)):
            path = os.path.join(DIST_DIR, name)
            if os.path.isfile(path) and name.endswith('.py'):
                out[name] = path
    builder = os.path.join(HERE, BUILDER_NAME)
    if os.path.isfile(builder):
        out[BUILDER_NAME] = builder
    return out


DIST_NOTES = {
    'fujinp_kernel_install.py': '導入スクリプト．新しいサイトのホーム直下（/home/<アカウント名>/）に置いて使います',
    BUILDER_NAME: '組み立て道具．配布元のサイトで導入スクリプトを作り直すときに使います',
}


def _dist_info():
    rows = []
    for name, path in _dist_files().items():
        st = os.stat(path)
        rows.append({
            'name': name,
            'size': st.st_size,
            'mtime': datetime.datetime.fromtimestamp(st.st_mtime, JST).strftime('%Y-%m-%d %H:%M'),
            'note': DIST_NOTES.get(name, ''),
        })
    return rows


# =============================================================================
# 画面
# =============================================================================

@migration_assistant.route('/')
@migration_assistant.route('/index')
def index():
    if not session.get('user_id'):
        return render_template('migration_assistant/error.html', error='ログインが必要です'), 401
    return render_template('migration_assistant/migration_assistant_index.html',
                           can_edit=is_instructor(),
                           dist_files=_dist_info(),
                           instructor_group=INSTRUCTOR_GROUP)


@migration_assistant.route('/dist/<path:name>')
def dist_download(name):
    """配布ファイルを受け取る（ガイドの文書からもここへリンクする）"""
    if not session.get('user_id'):
        return render_template('migration_assistant/error.html',
                               error='配布ファイルの受け取りにはログインが必要です'), 401
    files = _dist_files()
    if name not in files:
        abort(404)
    path = files[name]
    return send_from_directory(os.path.dirname(path), os.path.basename(path),
                               as_attachment=True, download_name=name)


@migration_assistant.route('/return_to_fujin')
def return_to_fujin():
    return redirect_to_dashboard()


# =============================================================================
# 図書館 API
# =============================================================================

def _row_out(r):
    return {
        'id': r['id'],
        'title': r['title'],
        'description': r.get('description') or '',
        'url': r['url'],
        'sort_order': r['sort_order'],
        'updated_at': r['updated_at'].strftime('%Y-%m-%d %H:%M') if r.get('updated_at') else '',
    }


def _clean_entry(data):
    """入力を検証して (title, description, url) を返す．不正なら ValueError"""
    title = str(data.get('title') or '').strip()
    url = str(data.get('url') or '').strip()
    desc = str(data.get('description') or '').strip()
    if not title:
        raise ValueError('題名は必須です')
    if len(title) > 255:
        raise ValueError('題名は255文字までです')
    if not url:
        raise ValueError('URLは必須です')
    if len(url) > 1000:
        raise ValueError('URLは1000文字までです')
    if not (url.startswith('https://') or url.startswith('http://') or url.startswith('/')):
        raise ValueError('URLは https:// または / で始めてください')
    if len(desc) > 2000:
        raise ValueError('説明は2000文字までです')
    return title, desc, url


@migration_assistant.route('/api/library', methods=['GET'])
def api_library_list():
    if not session.get('user_id'):
        return _need_login_json()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute(f"""
            SELECT id, title, description, url, sort_order, updated_at
            FROM {LIBRARY_TABLE} ORDER BY sort_order, id
        """)
        rows = [_row_out(r) for r in cur.fetchall()]
        cur.close()
        return jsonify({'success': True, 'entries': rows, 'can_edit': is_instructor()})
    except Exception as e:
        logging.error("migration_assistant api_library_list: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


@migration_assistant.route('/api/library', methods=['POST'])
def api_library_add():
    if not session.get('user_id'):
        return _need_login_json()
    if not is_instructor():
        return _need_instructor_json()
    try:
        title, desc, url = _clean_entry(request.get_json(silent=True) or {})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT COALESCE(MAX(sort_order), 0) FROM {LIBRARY_TABLE}")
        nxt = (cur.fetchone()[0] or 0) + 10
        now = get_jst_now()
        cur.execute(f"""
            INSERT INTO {LIBRARY_TABLE}
                (title, description, url, sort_order, created_by, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (title, desc, url, nxt, session.get('user_id'), now, now))
        new_id = cur.lastrowid
        conn.commit()
        cur.close()
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        logging.error("migration_assistant api_library_add: %s", e)
        if conn is not None:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


@migration_assistant.route('/api/library/<int:entry_id>', methods=['PUT'])
def api_library_update(entry_id):
    if not session.get('user_id'):
        return _need_login_json()
    if not is_instructor():
        return _need_instructor_json()
    try:
        title, desc, url = _clean_entry(request.get_json(silent=True) or {})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {LIBRARY_TABLE} WHERE id = %s", (entry_id,))
        if cur.fetchone() is None:
            cur.close()
            return jsonify({'success': False, 'error': '見つかりません'}), 404
        cur.execute(f"""
            UPDATE {LIBRARY_TABLE}
            SET title = %s, description = %s, url = %s, updated_at = %s
            WHERE id = %s
        """, (title, desc, url, get_jst_now(), entry_id))
        conn.commit()
        cur.close()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("migration_assistant api_library_update: %s", e)
        if conn is not None:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


@migration_assistant.route('/api/library/<int:entry_id>', methods=['DELETE'])
def api_library_delete(entry_id):
    if not session.get('user_id'):
        return _need_login_json()
    if not is_instructor():
        return _need_instructor_json()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {LIBRARY_TABLE} WHERE id = %s", (entry_id,))
        n = cur.rowcount
        conn.commit()
        cur.close()
        if not n:
            return jsonify({'success': False, 'error': '見つかりません'}), 404
        return jsonify({'success': True})
    except Exception as e:
        logging.error("migration_assistant api_library_delete: %s", e)
        if conn is not None:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


@migration_assistant.route('/api/library/order', methods=['POST'])
def api_library_order():
    """並び順を ids の順に振り直す（10, 20, 30…）"""
    if not session.get('user_id'):
        return _need_login_json()
    if not is_instructor():
        return _need_instructor_json()
    ids = (request.get_json(silent=True) or {}).get('ids')
    if not isinstance(ids, list):
        return jsonify({'success': False, 'error': 'ids は配列で渡してください'}), 400
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'ids が不正です'}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        now = get_jst_now()
        for i, eid in enumerate(ids, 1):
            cur.execute(f"UPDATE {LIBRARY_TABLE} SET sort_order = %s, updated_at = %s WHERE id = %s",
                        (i * 10, now, eid))
        conn.commit()
        cur.close()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("migration_assistant api_library_order: %s", e)
        if conn is not None:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn is not None and conn.is_connected():
            conn.close()
