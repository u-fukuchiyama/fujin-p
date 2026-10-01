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

# table_share/transfer.py
# テーシャ：テーブルのエクスポート／インポート（ファイル経由のマイグレーション）
#
# ・エクスポート：チェックしたテーブルを CREATE TABLE 文と全行ごと1本のJSONに書き出す
# ・インポート　：JSONを読み，取り込み先のスキーマと照合して
#     取り込み先に無い → ファイルのCREATE TABLEで作成
#     一致           → バックアップ後にデータを取り込む
#     相違           → バックアップ後にファイルのスキーマで作り直し，データを取り込む（ファイル側優先）
#   データの扱いは「置換」（既存行を捨てる）と「追記」（既存行を残し，同じキーの行はファイル側で上書き）の2通り
#
# インポートはブラウザがJSONを読み，テーブルごと・行の塊ごとに送る（サーバに一時ファイルを置かない）
# IMPORT_ENABLED = False のサイトではインポートの画面とAPIを出さない（エクスポート専用）
# 拠点サイト（nishida など）は True：エクスポートとインポートの両方を使う

import re
import json
import base64
import decimal
import datetime
import logging

import mysql.connector
from flask import request, jsonify, session, Response

from decorators import login_required
from config import Config
from db import DatabaseConfig
from . import table_share_bp
from .routes import check_admin_permission, is_safe_identifier, now_jst

# このサイトでインポートを使うか（拠点サイトは True，fujinpshowcase は False）
IMPORT_ENABLED = True

EXPORT_TYPE = 'fujinp_table_export'
EXPORT_FORMAT_VERSION = 1
MAX_ROWS_PER_CHUNK = 5000
MYSQL_NAME_MAX = 64


@table_share_bp.context_processor
def _transfer_context():
    return {'ts_import_enabled': IMPORT_ENABLED,
            'ts_is_admin': check_admin_permission(session.get('user_id'))}


# ============================================
# 共通ヘルパ
# ============================================

def _admin_or_403():
    if not check_admin_permission(session.get('user_id')):
        return jsonify({'success': False, 'error': 'この操作は管理者のみ実行できます'}), 403
    return None


def _account_databases():
    """このアカウント配下のDB名一覧"""
    conn = mysql.connector.connect(**DatabaseConfig.base())
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
            "WHERE SCHEMA_NAME LIKE %s ORDER BY SCHEMA_NAME",
            (Config.DB_ACCOUNT + '$%',))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _check_database(database):
    return bool(database) and is_safe_identifier(database) and database in _account_databases()


def _q(name):
    return '`' + name.replace('`', '``') + '`'


def _jsonable(v):
    """MySQLの値をJSONに載せられる形へ（取り込み時にそのまま戻せる表現）"""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float('inf'), float('-inf')) else None
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, datetime.datetime):
        return v.strftime('%Y-%m-%d %H:%M:%S.%f' if v.microsecond else '%Y-%m-%d %H:%M:%S')
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, datetime.time):
        return v.isoformat()
    if isinstance(v, datetime.timedelta):
        total = int(v.total_seconds())
        sign = '-' if total < 0 else ''
        total = abs(total)
        return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
    if isinstance(v, (bytes, bytearray)):
        try:
            return bytes(v).decode('utf-8')
        except UnicodeDecodeError:
            return {'$b64': base64.b64encode(bytes(v)).decode('ascii')}
    if isinstance(v, set):
        return ','.join(sorted(v))
    return str(v)


def _from_json(v):
    if isinstance(v, dict) and '$b64' in v:
        return base64.b64decode(v['$b64'])
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def _show_create(cur, table):
    cur.execute(f"SHOW CREATE TABLE {_q(table)}")
    row = cur.fetchone()
    return row[1] if row else None


def _rename_ddl(ddl, table):
    return re.sub(r'^\s*CREATE TABLE\s+`(?:[^`]|``)+`', f'CREATE TABLE {_q(table)}', ddl, count=1)


# ---------- スキーマ照合 ----------

_INT_WIDTH = re.compile(r'\b(tinyint|smallint|mediumint|int|bigint)\(\d+\)', re.I)


def _parse_ddl(ddl):
    """CREATE TABLE文を 列{名前:定義}・キー集合・表オプション に分ける（比較用に正規化）"""
    cols, keys, opts = {}, set(), ''
    lines = ddl.strip().splitlines()
    for ln in lines[1:]:
        s = ln.strip().rstrip(',')
        if s.startswith(')'):
            opts = s[1:].strip()
            continue
        if s.startswith('`'):
            m = re.match(r'`((?:[^`]|``)+)`\s+(.*)$', s)
            if m:
                cols[m.group(1)] = _INT_WIDTH.sub(r'\1', m.group(2)).strip()
        elif s:
            keys.add(_INT_WIDTH.sub(r'\1', s))
    opts = re.sub(r'\s*AUTO_INCREMENT=\d+', '', opts)
    opts = re.sub(r'\s+', ' ', opts).strip()
    return cols, keys, opts


def _compare(current_ddl, file_ddl):
    """(一致か, 相違点の説明リスト)"""
    c_cols, c_keys, c_opts = _parse_ddl(current_ddl)
    f_cols, f_keys, f_opts = _parse_ddl(file_ddl)
    notes = []
    for name, d in f_cols.items():
        if name not in c_cols:
            notes.append(f"列 {name} が取り込み先にない")
        elif c_cols[name] != d:
            notes.append(f"列 {name}：{c_cols[name]} → {d}")
    for name in c_cols:
        if name not in f_cols:
            notes.append(f"列 {name} はファイルにない（作り直すと消える）")
    if c_keys != f_keys:
        notes.append("キー・索引が異なる")
    if c_opts != f_opts:
        notes.append(f"表オプション：{c_opts} → {f_opts}")
    return (not notes), notes


def _session_setup(cur):
    cur.execute("SET FOREIGN_KEY_CHECKS = 0")
    # 0 を自動採番に置き換えない／日付などの厳格チェックを緩める（元データをそのまま入れるため）
    cur.execute("SET SESSION sql_mode = 'NO_AUTO_VALUE_ON_ZERO'")


def _backup_name(cur, table):
    """控えの表名（同じ秒に重なったら _2, _3 … を付ける）"""
    base = '_bk_' + now_jst().strftime('%Y%m%d_%H%M%S')
    for n in range(1, 100):
        suffix = base if n == 1 else f"{base}_{n}"
        name = table[:MYSQL_NAME_MAX - len(suffix)] + suffix
        cur.execute("SHOW TABLES LIKE %s", (name,))
        if not cur.fetchone():
            return name
    raise RuntimeError('控えの表名を決められません')


def _table_exists(cur, table):
    cur.execute("SHOW FULL TABLES LIKE %s", (table,))
    row = cur.fetchone()
    return bool(row) and row[1] == 'BASE TABLE'


# ============================================
# エクスポート
# ============================================

@table_share_bp.route('/transfer/list_local', methods=['GET'])
@login_required
def transfer_list_local():
    """エクスポート候補（このアカウント配下の全DBの実テーブル，行数つき）"""
    denied = _admin_or_403()
    if denied:
        return denied
    result = []
    try:
        for db in _account_databases():
            conn = mysql.connector.connect(**DatabaseConfig.get_config(db))
            try:
                cur = conn.cursor()
                cur.execute("SHOW FULL TABLES")
                names = [r[0] for r in cur.fetchall() if r[1] == 'BASE TABLE']
                for t in sorted(names):
                    cur.execute(f"SELECT COUNT(*) FROM {_q(t)}")
                    result.append({'database': db, 'table_name': t, 'row_count': cur.fetchone()[0]})
            finally:
                conn.close()
        return jsonify({'success': True, 'tables': result,
                        'import_enabled': IMPORT_ENABLED})
    except Exception as e:
        logging.error("transfer_list_local error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@table_share_bp.route('/transfer/export', methods=['POST'])
@login_required
def transfer_export():
    """チェックしたテーブルをスキーマと全行ごと1本のJSONで返す"""
    denied = _admin_or_403()
    if denied:
        return denied
    targets = (request.json or {}).get('tables') or []
    if not targets:
        return jsonify({'success': False, 'error': 'テーブルが選ばれていません'}), 400

    valid_dbs = set(_account_databases())
    out_tables = []
    total_rows = 0
    try:
        by_db = {}
        for t in targets:
            db, name = t.get('database', ''), t.get('table_name', '')
            if db not in valid_dbs or not is_safe_identifier(name):
                return jsonify({'success': False, 'error': f'不正な指定: {db}.{name}'}), 400
            by_db.setdefault(db, []).append(name)

        for db, names in by_db.items():
            conn = mysql.connector.connect(**DatabaseConfig.get_config(db))
            try:
                cur = conn.cursor()
                # DDLと行を同じ時点のものにそろえる
                cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
                for name in names:
                    if not _table_exists(cur, name):
                        return jsonify({'success': False, 'error': f'テーブルがありません: {db}.{name}'}), 404
                    ddl = _show_create(cur, name)
                    cur.execute(f"SELECT * FROM {_q(name)}")
                    columns = list(cur.column_names)
                    rows = [[_jsonable(v) for v in r] for r in cur.fetchall()]
                    total_rows += len(rows)
                    out_tables.append({
                        'database': db,
                        'database_suffix': db.split('$', 1)[1] if '$' in db else db,
                        'table_name': name,
                        'create_table': ddl,
                        'columns': columns,
                        'row_count': len(rows),
                        'rows': rows,
                    })
                conn.rollback()
            finally:
                conn.close()

        now = now_jst()
        payload = {
            'export_type': EXPORT_TYPE,
            'format_version': EXPORT_FORMAT_VERSION,
            'source_account': Config.DB_ACCOUNT,
            'source_site': getattr(Config, 'SITE_DISPLAY_NAME', Config.DB_ACCOUNT),
            'generated_at': now.strftime('%Y-%m-%d %H:%M:%S'),
            'generated_by': session.get('user_id'),
            'table_count': len(out_tables),
            'total_rows': total_rows,
            'note': 'テーシャのテーブルエクスポート。tables[]＝database（元DB名）／database_suffix（$以降）'
                    '／table_name／create_table（SHOW CREATE TABLE）／columns／rows（columns順の配列。'
                    'バイナリは {"$b64": …}）。取り込みはテーシャのインポートで行う。',
            'tables': out_tables,
        }
        body = json.dumps(payload, ensure_ascii=False)
        fname = f"tables_{Config.DB_ACCOUNT}_{now.strftime('%Y%m%d_%H%M%S')}.json"
        return Response(body, mimetype='application/json; charset=utf-8', headers={
            'Content-Disposition': f'attachment; filename="{fname}"',
            'X-Export-Filename': fname,
        })
    except Exception as e:
        logging.error("transfer_export error: %s", e)
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


# ============================================
# インポート
# ============================================

def _import_guard():
    if not IMPORT_ENABLED:
        return jsonify({'success': False, 'error': 'このサイトではインポートを使いません（エクスポート専用）'}), 404
    return _admin_or_403()


@table_share_bp.route('/transfer/check', methods=['POST'])
@login_required
def transfer_check():
    """ファイル内のテーブルを取り込み先のスキーマと照合する（データは送らない）
    入力: {tables:[{target_database, table_name, create_table}]}
    出力: 各テーブルの status = new（取り込み先に無い）／same（一致）／differ（相違）"""
    denied = _import_guard()
    if denied:
        return denied
    items = (request.json or {}).get('tables') or []
    valid_dbs = set(_account_databases())
    results = []
    conns = {}
    try:
        for it in items:
            db, name, ddl = it.get('target_database', ''), it.get('table_name', ''), it.get('create_table', '')
            r = {'target_database': db, 'table_name': name}
            if db not in valid_dbs:
                r.update(status='error', notes=[f'取り込み先DBがありません: {db}'])
            elif not is_safe_identifier(name) or len(name) > MYSQL_NAME_MAX:
                r.update(status='error', notes=['テーブル名が不正です'])
            elif not ddl.strip().upper().startswith('CREATE TABLE'):
                r.update(status='error', notes=['CREATE TABLE文がありません'])
            else:
                if db not in conns:
                    conns[db] = mysql.connector.connect(**DatabaseConfig.get_config(db))
                cur = conns[db].cursor()
                if not _table_exists(cur, name):
                    r.update(status='new', notes=['取り込み先に無いので作成します'])
                else:
                    cur.execute(f"SELECT COUNT(*) FROM {_q(name)}")
                    r['current_rows'] = cur.fetchone()[0]
                    same, notes = _compare(_show_create(cur, name), ddl)
                    r.update(status='same' if same else 'differ',
                             notes=notes or ['スキーマは一致しています'])
                cur.close()
            results.append(r)
        default_db = DatabaseConfig.default().get('database', '')
        return jsonify({'success': True, 'tables': results, 'databases': sorted(valid_dbs),
                        'default_database': default_db if default_db in valid_dbs else ''})
    except Exception as e:
        logging.error("transfer_check error: %s", e)
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        for c in conns.values():
            c.close()


@table_share_bp.route('/transfer/prepare', methods=['POST'])
@login_required
def transfer_prepare():
    """取り込みの準備（テーブル1本）
    入力: {target_database, table_name, create_table, mode: replace|merge}
    ・無い → ファイルのCREATE TABLEで作成
    ・一致 → バックアップ．replace なら中身を空にする
    ・相違 → バックアップ → DROP → ファイルのスキーマで作成．merge なら共通列の既存行を戻す"""
    denied = _import_guard()
    if denied:
        return denied
    d = request.json or {}
    db, name, ddl = d.get('target_database', ''), d.get('table_name', ''), d.get('create_table', '')
    mode = d.get('mode', 'replace')
    if mode not in ('replace', 'merge'):
        return jsonify({'success': False, 'error': 'mode が不正です'}), 400
    if not _check_database(db) or not is_safe_identifier(name) or len(name) > MYSQL_NAME_MAX:
        return jsonify({'success': False, 'error': '取り込み先の指定が不正です'}), 400
    if not ddl.strip().upper().startswith('CREATE TABLE'):
        return jsonify({'success': False, 'error': 'CREATE TABLE文がありません'}), 400

    new_ddl = _rename_ddl(ddl, name)
    conn = mysql.connector.connect(**DatabaseConfig.get_config(db), autocommit=True)
    try:
        cur = conn.cursor()
        _session_setup(cur)
        if not _table_exists(cur, name):
            cur.execute(new_ddl)
            return jsonify({'success': True, 'action': 'created', 'backup_table': None})

        same, notes = _compare(_show_create(cur, name), ddl)
        backup = _backup_name(cur, name)
        cur.execute(f"CREATE TABLE {_q(backup)} LIKE {_q(name)}")
        cur.execute(f"INSERT INTO {_q(backup)} SELECT * FROM {_q(name)}")

        if same:
            if mode == 'replace':
                cur.execute(f"TRUNCATE TABLE {_q(name)}")
            return jsonify({'success': True, 'action': 'kept_schema', 'backup_table': backup})

        cur.execute(f"DROP TABLE {_q(name)}")
        try:
            cur.execute(new_ddl)
        except Exception as e:
            # 作れなかったらバックアップから元に戻す
            cur.execute(f"CREATE TABLE {_q(name)} LIKE {_q(backup)}")
            cur.execute(f"INSERT INTO {_q(name)} SELECT * FROM {_q(backup)}")
            return jsonify({'success': False, 'error': f'ファイルのスキーマで作成できず元に戻しました: {e}',
                            'backup_table': backup}), 500
        kept = 0
        if mode == 'merge':
            old_cols = _parse_ddl(_show_create(cur, backup))[0]
            new_cols = _parse_ddl(new_ddl)[0]
            common = [c for c in new_cols if c in old_cols]
            if common:
                cl = ', '.join(_q(c) for c in common)
                cur.execute(f"INSERT IGNORE INTO {_q(name)} ({cl}) SELECT {cl} FROM {_q(backup)}")
                kept = cur.rowcount
        return jsonify({'success': True, 'action': 'rebuilt', 'backup_table': backup,
                        'kept_rows': kept, 'notes': notes})
    except Exception as e:
        logging.error("transfer_prepare error: %s", e)
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        conn.close()


@table_share_bp.route('/transfer/rows', methods=['POST'])
@login_required
def transfer_rows():
    """行の塊を1回分取り込む
    入力: {target_database, table_name, columns, rows, mode}
    replace は INSERT，merge は REPLACE（同じキーの行はファイル側で上書き）"""
    denied = _import_guard()
    if denied:
        return denied
    d = request.json or {}
    db, name = d.get('target_database', ''), d.get('table_name', '')
    columns, rows = d.get('columns') or [], d.get('rows') or []
    mode = d.get('mode', 'replace')
    if not _check_database(db) or not is_safe_identifier(name):
        return jsonify({'success': False, 'error': '取り込み先の指定が不正です'}), 400
    if not columns or len(rows) > MAX_ROWS_PER_CHUNK:
        return jsonify({'success': False, 'error': '列が無いか，1回分の行が多すぎます'}), 400
    if not rows:
        return jsonify({'success': True, 'inserted': 0})

    verb = 'REPLACE' if mode == 'merge' else 'INSERT'
    sql = (f"{verb} INTO {_q(name)} ({', '.join(_q(c) for c in columns)}) "
           f"VALUES ({', '.join(['%s'] * len(columns))})")
    conn = mysql.connector.connect(**DatabaseConfig.get_config(db))
    try:
        cur = conn.cursor()
        _session_setup(cur)
        batch = [tuple(_from_json(v) for v in r) for r in rows]
        for i in range(0, len(batch), 1000):
            cur.executemany(sql, batch[i:i + 1000])
        conn.commit()
        return jsonify({'success': True, 'inserted': len(batch)})
    except Exception as e:
        conn.rollback()
        logging.error("transfer_rows error: %s", e)
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        conn.close()


@table_share_bp.route('/transfer/finish', methods=['POST'])
@login_required
def transfer_finish():
    """取り込み後の行数を返す（確認用）"""
    denied = _import_guard()
    if denied:
        return denied
    d = request.json or {}
    db, name = d.get('target_database', ''), d.get('table_name', '')
    if not _check_database(db) or not is_safe_identifier(name):
        return jsonify({'success': False, 'error': '取り込み先の指定が不正です'}), 400
    conn = mysql.connector.connect(**DatabaseConfig.get_config(db))
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {_q(name)}")
        return jsonify({'success': True, 'row_count': cur.fetchone()[0]})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        conn.close()
