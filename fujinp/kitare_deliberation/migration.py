"""
kitare_deliberation - データ移行（fujinpshowcase → fujinp）

操作は審議画面のヘッダのボタンだけで完結する（admin にだけ表示）．
  書き出し元（SOURCE_HOST）：📤 移行データ書き出し
      → zip を1本ダウンロード．中身は data.json（7表の全件・登場する利用者の
        番号／メール／氏名・書類目録）と kitare_docs/（書類ファイル一式）．
  取り込み先（TARGET_HOST）：📥 移行データ取り込み
      → zip を選ぶと点検画面（利用者番号の対応表・件数・書類）
      → 「この内容で置き換える」で 7 表と書類を丸ごと入れ替える．
どちらのボタンを出すかはサイト名で決める．それ以外のサイトでは何も出ない．

取り込みは置き換え方式：7 表を全件削除してから入れ直す（1トランザクション）．
案件などの番号は書き出し元のまま入れるので，書類の置き場所 case_<案件ID>/ は
そのまま対応する．利用者番号はメールアドレスで照合して付け替える．照合できない
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

from flask import render_template, request, session, abort, send_file, Response

from decorators import login_required

from . import kitare_deliberation_bp
from .routes import _conn, is_admin, get_jst_now, KITARE_DOCS_ROOT, _doc_absolute_path


SOURCE_HOST = 'fujinpshowcase.pythonanywhere.com'   # 書き出しボタンを出すサイト
TARGET_HOST = 'fujinp.pythonanywhere.com'           # 取り込みボタンを出すサイト

FORMAT = 'kitare_migration'
FORMAT_VERSION = 1

# 親 → 子の順（取り込みはこの順，削除は逆順）
TABLE_ORDER = [
    'kitare_categories',
    'kitare_cases',
    'kitare_documents',
    'kitare_review_messages',
    'kitare_applicant_messages',
    'kitare_status_history',
    'kitare_coi',
]

# 利用者番号（users.id）を持つ列
USER_COLUMNS = {
    'kitare_categories':         ['created_by_user_id'],
    'kitare_cases':              ['applicant_user_id', 'accepted_decided_by_user_id',
                                  'decision_by_user_id'],
    'kitare_documents':          ['uploaded_by_user_id'],
    'kitare_review_messages':    ['user_id'],
    'kitare_applicant_messages': ['user_id'],
    'kitare_status_history':     ['changed_by_user_id'],
    'kitare_coi':                ['user_id', 'set_by_user_id'],
}

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


@kitare_deliberation_bp.context_processor
def _inject_migration_role():
    try:
        return {'kitare_migration_role': migration_role()}
    except Exception:
        return {'kitare_migration_role': None}


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
            cur.execute(f"SELECT * FROM `{t}` ORDER BY id")
            cols = [d[0] for d in cur.description]
            rows = [[_jsonable(v) for v in r] for r in cur.fetchall()]
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
    tb = tables['kitare_documents']
    ci = {c: i for i, c in enumerate(tb['columns'])}
    for r in tb['rows']:
        rel = r[ci['file_path']] if 'file_path' in ci else None
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
        rep['errors'].append('キターレ移行用の JSON ではありません．')
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


def _safe_doc_members(zf):
    """zip 内の kitare_docs/ 配下の安全なファイルだけを返す．"""
    out = []
    for info in zf.infolist():
        name = info.filename.replace('\\', '/')
        if info.is_dir() or not name.startswith('kitare_docs/'):
            continue
        parts = name.split('/')
        if name.startswith('/') or '..' in parts or '' in parts[1:]:
            continue
        out.append((info, name))
    return out


def _replace_docs_from_zip(path):
    """zip の kitare_docs/ で取り込み先の kitare_docs/ を丸ごと入れ替える．"""
    root = os.path.normpath(KITARE_DOCS_ROOT)
    parent = os.path.dirname(root)
    os.makedirs(parent, exist_ok=True)
    new_dir = root + '.new'
    old_dir = root + '.old'
    for d in (new_dir, old_dir):
        if os.path.isdir(d):
            shutil.rmtree(d)
    os.makedirs(new_dir)
    n = 0
    with zipfile.ZipFile(path) as zf:
        for info, name in _safe_doc_members(zf):
            rel = name[len('kitare_docs/'):]
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
    """zip に入っている書類の相対パス集合（case_x/yyy.pdf の形）．"""
    with zipfile.ZipFile(path) as zf:
        return {name[len('kitare_docs/'):] for _i, name in _safe_doc_members(zf)}


# ────────────────────────────────────────────
# 画面
# ────────────────────────────────────────────

def _render(mode, **kw):
    return render_template('kitare_deliberation/migration.html',
                           mode=mode, host=request.host, table_order=TABLE_ORDER, **kw)


def _require_role(role):
    user_id = session.get('user_id')
    if not user_id or not is_admin(user_id):
        abort(403)
    if migration_role() != role:
        abort(403)
    return user_id


@kitare_deliberation_bp.route('/migration/export')
@login_required
def migration_export():
    """📤 書き出し：data.json と kitare_docs/ を入れた zip を返す．"""
    user_id = _require_role('export')
    data = _build_export(user_id)
    tmp = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('data.json', json.dumps(data, ensure_ascii=False))
        if os.path.isdir(KITARE_DOCS_ROOT):
            for base, _dirs, files in os.walk(KITARE_DOCS_ROOT):
                for fn in files:
                    p = os.path.join(base, fn)
                    rel = os.path.relpath(p, KITARE_DOCS_ROOT).replace(os.sep, '/')
                    zf.write(p, 'kitare_docs/' + rel)
    resp = send_file(tmp.name, as_attachment=True,
                     download_name=f'kitare_migration_{_stamp()}.zip',
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
    docs = data.get('documents') or []
    missing = [d for d in docs if (d.get('file_path') or '').replace('\\', '/') not in in_zip]
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


@kitare_deliberation_bp.route('/migration/upload', methods=['POST'])
@login_required
def migration_upload():
    """📥 取り込み（1段目）：zip を受け取り，点検結果と対応表を表示する．"""
    _require_role('import')
    f = request.files.get('package')
    if not f or not f.filename:
        return _render('error', message='zip ファイルが選ばれていません．')
    os.makedirs(STAGING_DIR, exist_ok=True)
    f.save(STAGING_ZIP)
    try:
        _data, rep = _preview_report(STAGING_ZIP)
    except Exception as e:
        logging.error('kitare migration upload error: %s', e)
        return _render('error', message='移行データの zip として読めませんでした．'
                                        '書き出し元の「📤 移行データ書き出し」で作った zip を選んでください．')
    with open(STAGING_ZIP, 'rb') as zf:
        digest = hashlib.sha256(zf.read()).hexdigest()
    return _render('preview', report=rep, filename=f.filename, digest=digest)


@kitare_deliberation_bp.route('/migration/mapping.csv')
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
                             f'attachment; filename="kitare_user_map_{_stamp()}.csv"'})


@kitare_deliberation_bp.route('/migration/apply', methods=['POST'])
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
        logging.error('kitare migration apply error: %s', e)
        try:
            cur.execute('SET FOREIGN_KEY_CHECKS=1')
        except Exception:
            pass
        return _render('error', message=f'表の取り込みに失敗したので元に戻しました（書類も変えていません）：{e}')
    finally:
        cur.close()
        conn.close()

    try:
        docs_written = _replace_docs_from_zip(STAGING_ZIP)
        docs_error = None
    except Exception as e:
        logging.error('kitare migration docs error: %s', e)
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
        logging.error('kitare migration log error: %s', e)
    return _render('applied', result=log, docs_error=docs_error,
                   docs_missing_count=rep.get('docs_missing_count', 0))
