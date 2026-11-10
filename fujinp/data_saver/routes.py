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


# data_saver/routes.py
# Data Saver: アクセス制御 / 画面 / 対象ディレクトリ表の編集 / Save / Inspect / Restore
#   （Claude 向け一括調査と結果の照合・反映は routes_bulk.py）

import os
import re
import json
import time
import shutil
import logging
import zipfile
import secrets
import datetime
import mysql.connector
from flask import (render_template, jsonify, session, request,
                   redirect, url_for, send_file)
from decorators import login_required
from auth import redirect_to_dashboard
from db import DatabaseConfig
from config import Config
from . import data_saver_bp


TABLE = 'data_saver_dirs'
ZIP_FORMAT = 'fujinp_data_saver'
ZIP_FORMAT_VERSION = 1
MANIFEST_NAME = 'data_saver_manifest.json'
FILES_PREFIX = 'files/'
SKIP_DIR_NAMES = {'__pycache__'}
SAVE_STALE_SECONDS = 3600            # 送信し損ねた保存用 zip の掃除間隔
RESTORE_STALE_SECONDS = 24 * 3600    # アップロード済み zip の保持時間

_APP_NAME_RE = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')
_TOKEN_RE = re.compile(r'^\d{8}_\d{6}_[0-9a-f]{8}$')


# ============================================================
# 日時（FUJIN-P 日時3層ルール: バックエンドでJSTの文字列にする）
# ============================================================

JST = datetime.timezone(datetime.timedelta(hours=9))


def now_jst():
    return datetime.datetime.now(JST)


def now_jst_str():
    return now_jst().strftime('%Y-%m-%d %H:%M:%S')


def stamp_jst():
    return now_jst().strftime('%Y%m%d_%H%M%S')


# ============================================================
# アクセス制御（Blueprint全体を admin 限定）
# ============================================================

def _is_ajax():
    if request.method != 'GET':
        return True
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


@data_saver_bp.before_request
def require_admin():
    user_id = session.get('user_id')
    if not user_id:
        if _is_ajax():
            return jsonify({'success': False, 'error': 'ログインが必要です'}), 401
        return redirect(url_for('auth.login'))

    if get_user_category(user_id) != 'admin':
        if _is_ajax():
            return jsonify({'success': False, 'error': '管理者権限が必要です'}), 403
        return redirect(url_for('guest.dashboard'))


def get_user_category(user_id):
    cat = session.get('user_category')
    if cat:
        return cat
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT category FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        cat = row['category'] if row else None
    except Exception as e:
        logging.error("data_saver get_user_category error: %s", e)
        cat = None
    finally:
        _close(conn, cursor)
    if cat:
        session['user_category'] = cat
    return cat


def _close(conn, cursor):
    if cursor is not None:
        try:
            cursor.close()
        except Exception:
            pass
    if conn is not None:
        try:
            if conn.is_connected():
                conn.close()
        except Exception:
            pass


# ============================================================
# ディレクトリの基点と作業領域
#   基点   : FUJIN-P のルート（/home/<user>/fujinp）．既定は本アプリの親ディレクトリ．
#            アプリ一覧と入力例に使う．登録できるディレクトリは基点の配下に限らない
#            （~/static/mdimgs や ~/fujinp_file_uploads など，基点の外のデータ置き場がある）．
#            ルート（/）と作業領域だけは登録できない．
#   作業領域: 本アプリ配下 data_saver_work（static の外）．
#            _save/    … ダウンロード用に作った zip（送信後に消す）
#            _restore/ … Inspect でアップロードされた zip
# ============================================================

def base_dir():
    root = getattr(Config, 'DATA_SAVER_BASE_DIR', None)
    if not root:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.realpath(root)


def work_root():
    root = getattr(Config, 'DATA_SAVER_WORK_DIR', None)
    if not root:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data_saver_work')
    os.makedirs(root, exist_ok=True)
    return os.path.realpath(root)


def _sub_work(name):
    path = os.path.join(work_root(), name)
    os.makedirs(path, exist_ok=True)
    return path


def _is_under(path, root):
    return path.startswith(root.rstrip(os.sep) + os.sep)


def check_dir_path(raw):
    """登録・操作できるディレクトリか検査し，(正規化した文字列, 実パス) を返す。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('ディレクトリを入力してください')
    path = os.path.normpath(raw.strip())
    if not os.path.isabs(path):
        raise ValueError('ディレクトリは絶対パスで入力してください（例: %s）'
                         % os.path.join(base_dir(), 'XX', 'static'))
    real = os.path.realpath(path)
    if real == os.sep:
        raise ValueError('ルート（/）は登録できません')
    work = work_root()
    if real == work or _is_under(real, work) or _is_under(work, real):
        raise ValueError('Data Saver の作業領域を含むディレクトリは登録できません')
    return path, real


def check_app_name(raw):
    name = (raw or '').strip()
    if not _APP_NAME_RE.match(name):
        raise ValueError('アプリ名は英数字・_・- の64文字以内で入力してください')
    return name


def _cleanup(sub, seconds):
    root = _sub_work(sub)
    cutoff = time.time() - seconds
    try:
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if os.path.getmtime(path) < cutoff:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
    except Exception as e:
        logging.error("data_saver cleanup %s error: %s", sub, e)


# ============================================================
# テーブル操作
# ============================================================

def _parse_log(raw):
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x) for x in val]
    except Exception:
        pass
    return [s.strip() for s in str(raw).splitlines() if s.strip()]


def fetch_row(row_id):
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, app_name, dir_path, save_log FROM `%s` WHERE id = %%s"
                       % TABLE, (row_id,))
        row = cursor.fetchone()
        if row:
            row['save_log'] = _parse_log(row['save_log'])
        return row
    finally:
        _close(conn, cursor)


def append_save_log(row_id, stamp):
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT save_log FROM `%s` WHERE id = %%s FOR UPDATE" % TABLE, (row_id,))
        row = cursor.fetchone()
        log = _parse_log(row['save_log'] if row else None)
        log.append(stamp)
        cursor.execute("UPDATE `%s` SET save_log = %%s WHERE id = %%s" % TABLE,
                       (json.dumps(log, ensure_ascii=False), row_id))
        conn.commit()
    finally:
        _close(conn, cursor)


def _dir_status(dir_path):
    try:
        _p, real = check_dir_path(dir_path)
    except ValueError as e:
        return {'ok': False, 'exists': False, 'error': str(e)}
    return {'ok': True, 'exists': os.path.isdir(real), 'error': None}


# ============================================================
# 画面
# ============================================================

@data_saver_bp.route('/')
@login_required
def index():
    return render_template('data_saver/data_saver.html')


@data_saver_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    return redirect_to_dashboard()


# ============================================================
# 対象ディレクトリ表（一覧・追加・更新・削除）
#   編集できるのは app_name と dir_path．save_log は表示のみ．
# ============================================================

@data_saver_bp.route('/rows', methods=['GET'])
@login_required
def rows_list():
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, app_name, dir_path, save_log FROM `%s` ORDER BY id" % TABLE)
        rows = []
        for r in cursor.fetchall():
            rows.append({
                'id': r['id'],
                'app_name': r['app_name'],
                'dir_path': r['dir_path'],
                'save_log': _parse_log(r['save_log']),
                'status': _dir_status(r['dir_path']),
            })
        return jsonify({'success': True, 'rows': rows, 'base_dir': base_dir()})
    except Exception as e:
        logging.error("data_saver rows_list error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _close(conn, cursor)


@data_saver_bp.route('/rows/add', methods=['POST'])
@login_required
def rows_add():
    data = request.json or {}
    try:
        app_name = check_app_name(data.get('app_name'))
        dir_path, _real = check_dir_path(data.get('dir_path'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("INSERT INTO `%s` (app_name, dir_path, save_log) VALUES (%%s, %%s, %%s)"
                       % TABLE, (app_name, dir_path, '[]'))
        conn.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid})
    except Exception as e:
        logging.error("data_saver rows_add error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _close(conn, cursor)


@data_saver_bp.route('/rows/<int:row_id>/update', methods=['POST'])
@login_required
def rows_update(row_id):
    data = request.json or {}
    try:
        app_name = check_app_name(data.get('app_name'))
        dir_path, _real = check_dir_path(data.get('dir_path'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("UPDATE `%s` SET app_name = %%s, dir_path = %%s WHERE id = %%s"
                       % TABLE, (app_name, dir_path, row_id))
        conn.commit()
        if cursor.rowcount == 0 and fetch_row(row_id) is None:
            return jsonify({'success': False, 'error': '行が見つかりません'}), 404
        return jsonify({'success': True})
    except Exception as e:
        logging.error("data_saver rows_update error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _close(conn, cursor)


@data_saver_bp.route('/rows/<int:row_id>/delete', methods=['POST'])
@login_required
def rows_delete(row_id):
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("DELETE FROM `%s` WHERE id = %%s" % TABLE, (row_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("data_saver rows_delete error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        _close(conn, cursor)


# ============================================================
# Save: ディレクトリを zip にしてダウンロード
#   zip の中身:
#     data_saver_manifest.json … ID・アプリ名・ディレクトリ・保存日時など
#     files/<相対パス>          … ディレクトリの中身（シンボリックリンクと __pycache__ は除く）
# ============================================================

@data_saver_bp.route('/save/<int:row_id>', methods=['GET'])
@login_required
def save(row_id):
    row = fetch_row(row_id)
    if row is None:
        return jsonify({'success': False, 'error': '行が見つかりません'}), 404
    try:
        _p, real = check_dir_path(row['dir_path'])
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    if not os.path.isdir(real):
        return jsonify({'success': False, 'error': 'ディレクトリがありません: %s' % row['dir_path']}), 404

    _cleanup('_save', SAVE_STALE_SECONDS)
    saved_at = now_jst_str()
    zip_name = '%d_%s.zip' % (row_id, stamp_jst())
    zip_path = os.path.join(_sub_work('_save'), '%s_%s' % (secrets.token_hex(4), zip_name))

    file_count = 0
    total_bytes = 0
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for dirpath, dirs, files in os.walk(real, followlinks=False):
                dirs[:] = [d for d in dirs
                           if d not in SKIP_DIR_NAMES
                           and not os.path.islink(os.path.join(dirpath, d))]
                for fn in files:
                    full = os.path.join(dirpath, fn)
                    if os.path.islink(full) or not os.path.isfile(full):
                        continue
                    rel = os.path.relpath(full, real).replace(os.sep, '/')
                    zf.write(full, FILES_PREFIX + rel)
                    file_count += 1
                    total_bytes += os.path.getsize(full)
            manifest = {
                'format': ZIP_FORMAT,
                'format_version': ZIP_FORMAT_VERSION,
                'id': row_id,
                'app_name': row['app_name'],
                'dir_path': row['dir_path'],
                'saved_at': saved_at,
                'saved_by': session.get('user_id'),
                'site': request.host,
                'file_count': file_count,
                'total_bytes': total_bytes,
            }
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=1))

        append_save_log(row_id, saved_at)
        logging.info("data_saver save: user=%s id=%s files=%d bytes=%d",
                     session.get('user_id'), row_id, file_count, total_bytes)

        # 開いたまま実ファイルを消す（Linux では開いているハンドルから最後まで読める）．
        # 送信後に作業領域へ zip が残らない．
        fh = open(zip_path, 'rb')
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return send_file(fh, as_attachment=True, download_name=zip_name,
                         mimetype='application/zip')
    except Exception as e:
        logging.error("data_saver save error (id=%s): %s", row_id, e)
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================
# Inspect / Restore / Cancel
# ============================================================

def _token_path(raw):
    token = os.path.basename(raw or '')
    if not _TOKEN_RE.match(token):
        raise ValueError('不正なtokenです')
    path = os.path.join(_sub_work('_restore'), token + '.zip')
    if not os.path.exists(path):
        raise FileNotFoundError('アップロードされた zip が見つかりません（再度 Inspect してください）')
    return token, path


def _is_symlink_member(info):
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _read_zip(path):
    """(manifest, [(ZipInfo, 相対パス), ...]) を返す。形式違反は ValueError。"""
    with zipfile.ZipFile(path) as zf:
        try:
            manifest = json.loads(zf.read(MANIFEST_NAME).decode('utf-8'))
        except KeyError:
            raise ValueError('Data Saver の zip ではありません（%s がありません）' % MANIFEST_NAME)
        if manifest.get('format') != ZIP_FORMAT:
            raise ValueError('Data Saver の zip ではありません（format が違います）')
        members = []
        for info in zf.infolist():
            name = info.filename
            if not name.startswith(FILES_PREFIX) or name.endswith('/'):
                continue
            if _is_symlink_member(info):
                continue
            rel = name[len(FILES_PREFIX):]
            if not rel or rel.startswith('/') or '\\' in rel or \
                    any(part in ('', '.', '..') for part in rel.split('/')):
                raise ValueError('不正なzipエントリ: %s' % name)
            members.append((info, rel))
    return manifest, members


def _restore_plan(manifest, members):
    """復元先の行と，上書き／新規の内訳を作る。"""
    try:
        row_id = int(manifest.get('id'))
    except (TypeError, ValueError):
        raise ValueError('zip に ID が記録されていません')
    row = fetch_row(row_id)
    plan = {
        'id': row_id,
        'zip': {k: manifest.get(k) for k in
                ('app_name', 'dir_path', 'saved_at', 'site', 'file_count', 'total_bytes')},
        'row': None,
        'target_ok': False,
        'target_error': None,
        'target_exists': False,
        'mismatch': [],
        'overwrite_count': 0,
        'new_count': 0,
        'overwrite_samples': [],
        'file_count': len(members),
        'total_bytes': sum(i.file_size for i, _r in members),
    }
    if row is None:
        plan['target_error'] = 'このサイトの表に ID %d の行がありません．復元先が決まらないため復元できません' % row_id
        return plan, None
    plan['row'] = {'app_name': row['app_name'], 'dir_path': row['dir_path']}
    if row['app_name'] != manifest.get('app_name'):
        plan['mismatch'].append('アプリ名')
    if os.path.normpath(row['dir_path']) != os.path.normpath(manifest.get('dir_path') or ''):
        plan['mismatch'].append('ディレクトリ')
    try:
        _p, real = check_dir_path(row['dir_path'])
    except ValueError as e:
        plan['target_error'] = str(e)
        return plan, None
    plan['target_ok'] = True
    plan['target_exists'] = os.path.isdir(real)
    for _info, rel in members:
        if os.path.exists(os.path.join(real, *rel.split('/'))):
            plan['overwrite_count'] += 1
            if len(plan['overwrite_samples']) < 20:
                plan['overwrite_samples'].append(rel)
        else:
            plan['new_count'] += 1
    return plan, real


@data_saver_bp.route('/inspect', methods=['POST'])
@login_required
def inspect():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'ファイルがありません'}), 400
    f = request.files['file']
    if not f.filename or not f.filename.lower().endswith('.zip'):
        return jsonify({'success': False, 'error': '.zip ファイルを指定してください'}), 400

    _cleanup('_restore', RESTORE_STALE_SECONDS)
    token = '%s_%s' % (stamp_jst(), secrets.token_hex(4))
    path = os.path.join(_sub_work('_restore'), token + '.zip')
    f.save(path)
    try:
        manifest, members = _read_zip(path)
        plan, _real = _restore_plan(manifest, members)
        return jsonify({'success': True, 'token': token, 'filename': f.filename, 'plan': plan})
    except Exception as e:
        logging.error("data_saver inspect error: %s", e)
        try:
            os.remove(path)
        except OSError:
            pass
        code = 400 if isinstance(e, (ValueError, zipfile.BadZipFile)) else 500
        return jsonify({'success': False, 'error': str(e)}), code


@data_saver_bp.route('/restore', methods=['POST'])
@login_required
def restore():
    data = request.json or {}
    try:
        token, path = _token_path(data.get('token'))
        manifest, members = _read_zip(path)
        plan, real = _restore_plan(manifest, members)
    except FileNotFoundError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    if not plan['target_ok']:
        return jsonify({'success': False, 'error': plan['target_error']}), 400

    written = 0
    errors = []
    try:
        os.makedirs(real, exist_ok=True)
        with zipfile.ZipFile(path) as zf:
            for info, rel in members:
                dest = os.path.realpath(os.path.join(real, *rel.split('/')))
                if not _is_under(dest, real):
                    errors.append({'path': rel, 'error': '展開先がディレクトリの外になります'})
                    continue
                try:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    tmp = dest + '.data_saver_tmp'
                    with zf.open(info) as src, open(tmp, 'wb') as out:
                        shutil.copyfileobj(src, out)
                    os.replace(tmp, dest)
                    written += 1
                except Exception as ie:
                    errors.append({'path': rel, 'error': str(ie)})
        os.remove(path)
        logging.info("data_saver restore: user=%s id=%s written=%d errors=%d",
                     session.get('user_id'), plan['id'], written, len(errors))
        return jsonify({'success': True,
                        'message': '%d ファイルを %s へ復元しました' % (written, plan['row']['dir_path']),
                        'written': written, 'errors': errors[:50],
                        'error_count': len(errors)})
    except Exception as e:
        logging.error("data_saver restore error (token=%s): %s", token, e)
        return jsonify({'success': False, 'error': str(e), 'written': written}), 500


@data_saver_bp.route('/cancel', methods=['POST'])
@login_required
def cancel():
    try:
        _token, path = _token_path((request.json or {}).get('token'))
        os.remove(path)
    except Exception:
        pass
    return jsonify({'success': True})
