"""
ekinaka - データ移行（fujinpshowcase → fujinp）

操作は審議画面のヘッダのボタンだけで完結する（admin にだけ表示）．
  書き出し元（SOURCE_HOST）：📤 移行データ書き出し
      → zip を1本ダウンロード．中身は data.json（7表の全件・登場する利用者の
        番号／メール／氏名）と ekinaka_imgs/（イベント詳細の画像）・ekinaka_displays/（メッセージディスプレイの画像と表示状態）．
  取り込み先（TARGET_HOST）：📥 移行データ取り込み
      → zip を選ぶと点検画面（利用者番号の対応表・件数・書類）
      → 「この内容で置き換える」で 4 表と画像フォルダを丸ごと入れ替える．
どちらのボタンを出すかはサイト名で決める．それ以外のサイトでは何も出ない．

取り込みは置き換え方式：4 表を全件削除してから入れ直す（1トランザクション）．
画像フォルダはアプリ配下（ekinaka/static/ 以下）を丸ごと入れ替え，本文中の旧URL（/static/ekinaka_imgs/）を /ekinaka/img/ に書き換える．
案件などの番号は書き出し元のまま入れる．利用者番号はメールアドレスで照合して付け替える．照合できない
人が一人でもいれば適用しない．毎回元の番号から付け替えるので何度繰り返してよい．

作業ファイルは <app>/static/migration_staging/ に置く（GitHub に送らず，
Blueprint は static_folder を持たないので Web にも配信されない）．
"""
import os
import io
import csv
import json
import shutil
import zipfile
import hashlib
import logging
import tempfile
import datetime
import decimal

from flask import render_template, request, session, abort, send_file, Response, url_for

from decorators import login_required

import mysql.connector
from db import DatabaseConfig

from flask import current_app

from . import ekinaka_bp
from .routes import get_jst_now, is_admin_user


def is_admin(user_id):
    """移行はサイト管理者（user_category = 'admin'）だけに許す．"""
    return bool(user_id) and is_admin_user()


# 運ぶフォルダ．取り込み先はアプリ配下（標準の置き場）．書き出しは標準の置き場と，
# 旧版の置き場（Flask の static フォルダ・~/static/）の両方から集める（旧版サイトにも対応）．
APP_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
FOLDERS = ['ekinaka_imgs', 'ekinaka_displays']
DOCS_ROOT = APP_STATIC          # 書類フォルダの取り込みを有効にする印


def _folder_target(sub):
    return os.path.join(APP_STATIC, sub)


def _folder_sources(sub):
    out = [_folder_target(sub)]
    try:
        if current_app.static_folder:
            out.append(os.path.join(current_app.static_folder, sub))
    except Exception:
        pass
    out.append(os.path.join(os.path.expanduser('~'), 'static', sub))
    seen, uniq = set(), []
    for p in out:
        q = os.path.normpath(p)
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq


# 文字列の値に含まれる旧URL（/static/ekinaka_imgs/<名前>）は，取り込み時に画像ルートのURLへ書き換える
LEGACY_IMG_URL = '/static/ekinaka_imgs/'


def _doc_absolute_path(_rel):
    return None


def _conn():
    return mysql.connector.connect(**DatabaseConfig.default())


SOURCE_HOST = 'fujinpshowcase.pythonanywhere.com'   # 書き出しボタンを出すサイト
TARGET_HOST = 'fujinp.pythonanywhere.com'           # 取り込みボタンを出すサイト

FORMAT = 'ekinaka_migration'
FORMAT_VERSION = 1

# 親 → 子の順（取り込みはこの順，削除は逆順）
TABLE_ORDER = [
    'ekinaka_events',
    'ekinaka_performances',
    'ekinaka_access_settings',
    'ekinaka_access_logs',
]

# 利用者番号（users.id）を持つ列
USER_COLUMNS = {
    'ekinaka_events':       ['applicant_id', 'approver_id'],
    'ekinaka_access_logs':  ['user_id'],
}

# 書類表は無い．画像フォルダだけを丸ごと運ぶ
DOC_TABLE = None
DOC_PATH_COLUMN = None
DOCS_DIRNAME = 'ekinaka_imgs'   # 互換用（実際の運搬は FOLDERS）

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STAGING_DIR = os.path.join(APP_DIR, 'static', 'migration_staging')
STAGING_ZIP = os.path.join(STAGING_DIR, 'staging.zip')
IMPORT_LOG = os.path.join(STAGING_DIR, 'import_log.json')
INSERT_BATCH = 200


def migration_role():
    host = (request.host or '').split(':')[0].lower()
    if host == SOURCE_HOST:
        return 'export'
    if host == TARGET_HOST:
        return 'import'
    return None


@ekinaka_bp.context_processor
def _inject_migration_role():
    try:
        uid = session.get('user_id')
        role = migration_role() if is_admin(uid) else None
        return {'ekinaka_migration_role': role}
    except Exception:
        return {'ekinaka_migration_role': None}


# ────────────────────────────────────────────
# 共通
# ────────────────────────────────────────────

def _require_admin():
    user_id = session.get('user_id')
    if not user_id or not is_admin(user_id):
        abort(403)
    return user_id


def _stamp():
    return get_jst_now().strftime('%Y%m%d_%H%M%S')


def _now_text():
    return get_jst_now().strftime('%Y-%m-%d %H:%M:%S')


def _jsonable(v):
    if isinstance(v, datetime.datetime):
        return v.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, datetime.timedelta):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return v.decode('utf-8', 'replace')
    return v


def _is_external(rel):
    """旧方式で Google Drive などの URL が入っている書類（ファイル本体はこのサイトに無い）．"""
    return bool(rel) and str(rel).lower().startswith(('http://', 'https://'))


def _rel(rel):
    return (rel or '').replace('\\', '/').lstrip('/')


def _norm_email(e):
    return (e or '').strip().lower()


def _local_counts(cursor):
    out = {}
    for t in TABLE_ORDER:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM `{t}`")
            out[t] = cursor.fetchone()[0]
        except Exception:
            out[t] = None
    return out


def _local_columns(cursor):
    out = {}
    for t in TABLE_ORDER:
        cursor.execute(f"SHOW COLUMNS FROM `{t}`")
        out[t] = [r[0] for r in cursor.fetchall()]
    return out


# ────────────────────────────────────────────
# 書き出し
# ────────────────────────────────────────────

def _build_export(user_id):
    conn = _conn()
    cur = conn.cursor()
    tables = {}
    try:
        for t in TABLE_ORDER:
            cur.execute(f"SELECT * FROM `{t}`")
            cols = [d[0] for d in cur.description]
            rows = [[_jsonable(v) for v in r] for r in cur.fetchall()]
            rows.sort(key=lambda r: (str(type(r[0])), r[0]))   # 先頭列（id／skey）順
            tables[t] = {'columns': cols, 'rows': rows}

        # 登場する利用者番号と，その参照件数
        refs = {}
        for t, ucols in USER_COLUMNS.items():
            tb = tables[t]
            idx = [tb['columns'].index(c) for c in ucols if c in tb['columns']]
            for r in tb['rows']:
                for i in idx:
                    if r[i] is not None:
                        uid = int(r[i])
                        refs[uid] = refs.get(uid, 0) + 1

        users = []
        if refs:
            ids = sorted(refs)
            ph = ','.join(['%s'] * len(ids))
            cur.execute(f"SELECT id, email, full_name FROM users WHERE id IN ({ph}) ORDER BY id",
                        tuple(ids))
            found = {r[0]: r for r in cur.fetchall()}
            for uid in ids:
                r = found.get(uid)
                users.append({
                    'id': uid,
                    'email': r[1] if r else None,
                    'full_name': r[2] if r else None,
                    'refs': refs[uid],
                })
    finally:
        cur.close()
        conn.close()

    docs = []
    tb = tables[DOC_TABLE] if DOC_TABLE else {'columns': [], 'rows': []}
    ci = {c: i for i, c in enumerate(tb['columns'])}
    for r in tb['rows']:
        rel = r[ci[DOC_PATH_COLUMN]] if DOC_PATH_COLUMN in ci else None
        if _is_external(rel):
            docs.append({'id': r[ci['id']], 'case_id': r[ci['case_id']],
                         'file_path': rel, 'external': True, 'exists': True, 'size': None})
            continue
        p = _doc_absolute_path(rel) if rel else None
        ok = bool(p and os.path.isfile(p))
        docs.append({
            'id': r[ci['id']],
            'case_id': r[ci['case_id']],
            'file_path': rel,
            'exists': ok,
            'size': os.path.getsize(p) if ok else None,
        })

    return {
        'format': FORMAT,
        'format_version': FORMAT_VERSION,
        'source_host': request.host,
        'generated_at': _now_text(),
        'generated_by_user_id': user_id,
        'counts': {t: len(tables[t]['rows']) for t in TABLE_ORDER},
        'users': users,
        'documents': docs,
        'tables': tables,
    }


# ────────────────────────────────────────────
# 点検（取り込み前）
# ────────────────────────────────────────────

def _analyze(data):
    """取り込み内容を点検し，利用者番号の対応表を作る．"""
    rep = {'errors': [], 'warnings': []}
    if data.get('format') != FORMAT:
        rep['errors'].append('えきなかの移行用データではありません．')
        return rep
    if data.get('format_version') != FORMAT_VERSION:
        rep['errors'].append(f"形式の版が違います（{data.get('format_version')}）．")
        return rep
    tables = data.get('tables') or {}
    missing = [t for t in TABLE_ORDER if t not in tables]
    if missing:
        rep['errors'].append('表が欠けています：' + '，'.join(missing))
        return rep

    rep['source_host'] = data.get('source_host')
    rep['generated_at'] = data.get('generated_at')
    rep['counts'] = {t: len(tables[t]['rows']) for t in TABLE_ORDER}

    conn = _conn()
    cur = conn.cursor()
    try:
        local_cols = _local_columns(cur)
        rep['local_counts'] = _local_counts(cur)

        # 列の突き合わせ（取り込み先に無い列は入れない）
        skipped = {}
        for t in TABLE_ORDER:
            extra = [c for c in tables[t]['columns'] if c not in local_cols[t]]
            if extra:
                skipped[t] = extra
                rep['warnings'].append(f"{t}：取り込み先に無い列を入れません（{'，'.join(extra)}）")
        rep['skipped_columns'] = skipped

        # 取り込み先の users（メールアドレス → 該当者）
        cur.execute("SELECT id, email, full_name FROM users")
        by_email = {}
        for uid, email, name in cur.fetchall():
            key = _norm_email(email)
            if key:
                by_email.setdefault(key, []).append((uid, name))
    finally:
        cur.close()
        conn.close()

    mapping_rows = []
    mapping = {}
    unresolved = 0
    for u in data.get('users') or []:
        key = _norm_email(u.get('email'))
        hits = by_email.get(key, []) if key else []
        row = {
            'old_id': u['id'],
            'email': u.get('email'),
            'old_name': u.get('full_name'),
            'refs': u.get('refs'),
            'new_id': None,
            'new_name': None,
        }
        if not key:
            row['status'] = 'メールなし'
        elif not hits:
            row['status'] = '未登録'
        elif len(hits) > 1:
            row['status'] = '重複'
            row['new_name'] = '，'.join(f"#{h[0]} {h[1] or ''}" for h in hits)
        else:
            row['status'] = '一致'
            row['new_id'], row['new_name'] = hits[0]
            mapping[int(u['id'])] = int(hits[0][0])
        if row['status'] != '一致':
            unresolved += 1
        mapping_rows.append(row)

    # users 表に載っていない番号が行に残っていないか
    listed = {int(u['id']) for u in data.get('users') or []}
    stray = set()
    for t, ucols in USER_COLUMNS.items():
        tb = tables[t]
        idx = [tb['columns'].index(c) for c in ucols if c in tb['columns']]
        for r in tb['rows']:
            for i in idx:
                if r[i] is not None and int(r[i]) not in listed:
                    stray.add(int(r[i]))
    if stray:
        rep['errors'].append('利用者表に無い番号が使われています：' +
                             '，'.join(str(s) for s in sorted(stray)))

    if unresolved:
        rep['errors'].append(f'対応の決まらない利用者が {unresolved} 人います．'
                             'fujinp に登録するか，メールアドレスをそろえてから点検し直してください．')

    rep['mapping_rows'] = mapping_rows
    rep['mapping'] = mapping

    # 書類ファイルの所在（取り込み先）
    docs = data.get('documents') or []
    present = 0
    absent = []
    src_absent = 0
    for d in docs:
        if d.get('external'):
            present += 1
            continue
        if not d.get('exists'):
            src_absent += 1
        p = _doc_absolute_path(d.get('file_path')) if d.get('file_path') else None
        if p and os.path.isfile(p):
            present += 1
        else:
            absent.append(d)
    rep['docs_total'] = len(docs)
    rep['docs_present'] = present
    rep['docs_absent'] = absent[:50]
    rep['docs_absent_count'] = len(absent)
    rep['docs_src_absent'] = src_absent
    if absent:
        rep['warnings'].append(f'書類ファイルが {len(absent)} 件まだ置かれていません'
                               '（表の取り込みはできます．書類一式を取り込めば解消します）．')
    if src_absent:
        rep['warnings'].append(f'書き出し元でもファイルが見つからなかった書類が {src_absent} 件あります．')

    rep['ok'] = not rep['errors']
    return rep





# ────────────────────────────────────────────
# zip の読み書き
# ────────────────────────────────────────────

def _read_package(path):
    """zip から data.json を読む．形式が違えば例外．"""
    with zipfile.ZipFile(path) as zf:
        with zf.open('data.json') as f:
            return json.loads(f.read().decode('utf-8'))


def _safe_doc_members(zf, sub=None):
    """zip 内の運搬フォルダ配下の安全なファイルだけを返す．(info, フォルダ名, 相対パス)"""
    subs = [sub] if sub else FOLDERS
    out = []
    for info in zf.infolist():
        name = info.filename.replace('\\', '/')
        if info.is_dir():
            continue
        top = name.split('/', 1)[0]
        if top not in subs:
            continue
        parts = name.split('/')
        if name.startswith('/') or '..' in parts or '' in parts[1:] or len(parts) < 2:
            continue
        out.append((info, top, name[len(top) + 1:]))
    return out


def _replace_docs_from_zip(path):
    """zip の各フォルダで，取り込み先（アプリ配下）の同名フォルダを丸ごと入れ替える．"""
    n = 0
    with zipfile.ZipFile(path) as zf:
        for sub in FOLDERS:
            root = os.path.normpath(_folder_target(sub))
            os.makedirs(os.path.dirname(root), exist_ok=True)
            new_dir = root + '.new'
            old_dir = root + '.old'
            for d in (new_dir, old_dir):
                if os.path.isdir(d):
                    shutil.rmtree(d)
            os.makedirs(new_dir)
            for info, _top, rel in _safe_doc_members(zf, sub):
                target = os.path.normpath(os.path.join(new_dir, rel))
                if not target.startswith(new_dir + os.sep):
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, 'wb') as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                n += 1
            if os.path.isdir(root):
                os.rename(root, old_dir)
            os.rename(new_dir, root)
            if os.path.isdir(old_dir):
                shutil.rmtree(old_dir, ignore_errors=True)
    return n


def _docs_in_zip(path):
    """zip に入っている運搬ファイルの集合（フォルダ名/相対パス）．"""
    with zipfile.ZipFile(path) as zf:
        return {top + '/' + rel for _i, top, rel in _safe_doc_members(zf)}


# ────────────────────────────────────────────
# 画面
# ────────────────────────────────────────────

def _render(mode, **kw):
    return render_template('ekinaka_migration.html',
                           mode=mode, host=request.host, table_order=TABLE_ORDER, **kw)


def _require_role(role):
    user_id = session.get('user_id')
    if not user_id or not is_admin(user_id):
        abort(403)
    if migration_role() != role:
        abort(403)
    return user_id


@ekinaka_bp.route('/migration/export')
@login_required
def migration_export():
    """📤 書き出し：data.json と画像フォルダを入れた zip を返す．"""
    user_id = _require_role('export')
    data = _build_export(user_id)
    tmp = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('data.json', json.dumps(data, ensure_ascii=False))
        for sub in FOLDERS:
            seen = set()
            for root in _folder_sources(sub):
                if not os.path.isdir(root):
                    continue
                for base, _dirs, files in os.walk(root):
                    for fn in files:
                        p = os.path.join(base, fn)
                        rel = os.path.relpath(p, root).replace(os.sep, '/')
                        if rel in seen:
                            continue
                        seen.add(rel)
                        zf.write(p, sub + '/' + rel)
    resp = send_file(tmp.name, as_attachment=True,
                     download_name=f'ekinaka_migration_{_stamp()}.zip',
                     mimetype='application/zip')

    def _cleanup():
        try:
            os.remove(tmp.name)
        except OSError:
            pass
    resp.call_on_close(_cleanup)
    return resp


def _preview_report(path):
    data = _read_package(path)
    rep = _analyze(data)
    if rep.get('counts') is None:
        return data, rep
    # 書類は zip に入っているかで判定し直す（取り込むと zip の内容に入れ替わるため）
    in_zip = _docs_in_zip(path)
    rep['folder_files'] = len(in_zip)
    docs = data.get('documents') or []
    missing = [d for d in docs
               if not d.get('external') and _rel(d.get('file_path')) not in in_zip]
    rep['docs_total'] = len(docs)
    rep['docs_in_zip'] = len(docs) - len(missing)
    rep['docs_missing'] = missing[:50]
    rep['docs_missing_count'] = len(missing)
    rep['warnings'] = [w for w in rep['warnings']
                      if not w.startswith(('書類ファイルが', '書き出し元でも'))]
    if missing:
        rep['warnings'].append(f'zip に入っていない書類が {len(missing)} 件あります（書き出し元でファイルが見つかりませんでした）．'
                               'これらは取り込み後も開けません．')
    return data, rep


@ekinaka_bp.route('/migration/upload', methods=['POST'])
@login_required
def migration_upload():
    """📥 取り込み（1段目）：zip を受け取り，点検結果と対応表を表示する（小さい zip 用）．"""
    _require_role('import')
    f = request.files.get('package')
    if not f or not f.filename:
        return _render('error', message='zip ファイルが選ばれていません．')
    os.makedirs(STAGING_DIR, exist_ok=True)
    f.save(STAGING_ZIP)
    return _preview_staging(f.filename)


def _preview_staging(filename):
    try:
        _data, rep = _preview_report(STAGING_ZIP)
    except Exception as e:
        logging.error('ekinaka migration upload error: %s', e)
        return _render('error', message='移行データの zip として読めませんでした．'
                                        '書き出し元の「📤 移行データ書き出し」で作った zip を選んでください．')
    with open(STAGING_ZIP, 'rb') as zf:
        digest = hashlib.sha256(zf.read()).hexdigest()
    return _render('preview', report=rep, filename=filename, digest=digest)


# ── 分割アップロード ─────────────────────────────
# 画像を含む zip は大きく，サーバの手前（PythonAnywhere の受付）で 413 になるため，
# 画面側で 4MB ずつに切って順に送り，最後に組み立てて点検画面を出す．
CHUNK_PART = os.path.join(STAGING_DIR, 'staging.zip.part')


@ekinaka_bp.route('/migration/upload_chunk', methods=['POST'])
@login_required
def migration_upload_chunk():
    _require_role('import')
    try:
        index = int(request.form.get('index', '-1'))
        total = int(request.form.get('total', '0'))
    except ValueError:
        return {'ok': False, 'error': 'bad index'}, 400
    chunk = request.files.get('chunk')
    if index < 0 or total <= 0 or index >= total or chunk is None:
        return {'ok': False, 'error': 'bad request'}, 400
    os.makedirs(STAGING_DIR, exist_ok=True)
    mode = 'wb' if index == 0 else 'ab'
    with open(CHUNK_PART, mode) as out:
        shutil.copyfileobj(chunk.stream, out, 1 << 20)
    return {'ok': True, 'index': index}


@ekinaka_bp.route('/migration/upload_finish', methods=['POST'])
@login_required
def migration_upload_finish():
    _require_role('import')
    if not os.path.isfile(CHUNK_PART):
        return _render('error', message='アップロードされたデータが見つかりません．もう一度「📥 移行データ取り込み」から始めてください．')
    os.replace(CHUNK_PART, STAGING_ZIP)
    return _preview_staging(request.form.get('filename') or '（移行データ）')


@ekinaka_bp.route('/migration/mapping.csv')
@login_required
def migration_mapping_csv():
    _require_role('import')
    try:
        _data, rep = _preview_report(STAGING_ZIP)
    except Exception:
        return _render('error', message='点検済みの zip がありません．')
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['旧番号', 'メール', '旧サイトの氏名', '参照件数', '新番号', '新サイトの氏名', '状態'])
    for r in rep.get('mapping_rows', []):
        w.writerow([r['old_id'], r['email'] or '', r['old_name'] or '', r['refs'] or '',
                    r['new_id'] or '', r['new_name'] or '', r['status']])
    return Response('\ufeff' + buf.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition':
                             f'attachment; filename="ekinaka_user_map_{_stamp()}.csv"'})


@ekinaka_bp.route('/migration/apply', methods=['POST'])
@login_required
def migration_apply():
    """📥 取り込み（2段目）：7表と書類を zip の内容で置き換える．"""
    user_id = _require_role('import')
    try:
        with open(STAGING_ZIP, 'rb') as zf:
            digest = hashlib.sha256(zf.read()).hexdigest()
        data, rep = _preview_report(STAGING_ZIP)
    except Exception:
        return _render('error', message='点検済みの zip がありません．もう一度「📥 移行データ取り込み」から始めてください．')
    if digest != request.form.get('digest'):
        return _render('error', message='点検のあとに別の zip が読み込まれています．もう一度「📥 移行データ取り込み」から始めてください．')
    if not rep.get('ok'):
        return _render('preview', report=rep, filename='（点検済みの zip）', digest=digest)

    mapping = rep['mapping']
    tables = data['tables']
    new_img_url = url_for('ekinaka.image', filename='x')[:-1]   # 例 /ekinaka/img/
    conn = _conn()
    conn.autocommit = False
    cur = conn.cursor()
    inserted = {}
    try:
        local_cols = _local_columns(cur)
        cur.execute('SET FOREIGN_KEY_CHECKS=0')
        for t in reversed(TABLE_ORDER):
            cur.execute(f'DELETE FROM `{t}`')
        for t in TABLE_ORDER:
            tb = tables[t]
            cols = [c for c in tb['columns'] if c in local_cols[t]]
            idx = [tb['columns'].index(c) for c in cols]
            ucol_pos = [cols.index(c) for c in USER_COLUMNS.get(t, []) if c in cols]
            rows = []
            for r in tb['rows']:
                vals = [r[i] for i in idx]
                for j in ucol_pos:
                    if vals[j] is not None:
                        vals[j] = mapping[int(vals[j])]
                for j, v in enumerate(vals):
                    if isinstance(v, str) and LEGACY_IMG_URL in v:
                        vals[j] = v.replace(LEGACY_IMG_URL, new_img_url)
                rows.append(tuple(vals))
            if rows:
                sql = (f"INSERT INTO `{t}` (" + ','.join(f'`{c}`' for c in cols) +
                       ') VALUES (' + ','.join(['%s'] * len(cols)) + ')')
                for k in range(0, len(rows), INSERT_BATCH):
                    cur.executemany(sql, rows[k:k + INSERT_BATCH])
            inserted[t] = len(rows)
        cur.execute('SET FOREIGN_KEY_CHECKS=1')
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error('ekinaka migration apply error: %s', e)
        try:
            cur.execute('SET FOREIGN_KEY_CHECKS=1')
        except Exception:
            pass
        return _render('error', message=f'表の取り込みに失敗したので元に戻しました（書類も変えていません）：{e}')
    finally:
        cur.close()
        conn.close()

    try:
        docs_written = _replace_docs_from_zip(STAGING_ZIP) if DOCS_ROOT else 0
        docs_error = None
    except Exception as e:
        logging.error('ekinaka migration docs error: %s', e)
        docs_written = 0
        docs_error = str(e)

    log = {
        'imported_at': _now_text(),
        'imported_by_user_id': user_id,
        'source_host': data.get('source_host'),
        'generated_at': data.get('generated_at'),
        'counts': inserted,
        'users_mapped': len(mapping),
        'docs_written': docs_written,
    }
    try:
        with open(IMPORT_LOG, 'w', encoding='utf-8') as f:
            json.dump(log, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logging.error('ekinaka migration log error: %s', e)
    return _render('applied', result=log, docs_error=docs_error,
                   docs_missing_count=rep.get('docs_missing_count', 0))
