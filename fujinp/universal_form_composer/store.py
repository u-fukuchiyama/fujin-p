"""ゆにこん — プロジェクトの器（SQL）。

プロジェクト1件は unicon_projects の1行。中身（部品群・対応付け・担当表など）は
data 列（LONGTEXT）に1本の JSON として丸ごと持つ。一覧は data 列を読まずに出す。
利用者とまいぐるのグループは FUJIN-P の共通 DB（default）から引く。
"""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import db as _db_module

JST = timezone(timedelta(hours=9))

FORMAT = 'unicon_project'
FORMAT_VERSION = 1
EDITOR_GROUPS = ('admin', 'ゆにこん編集者')     # まいぐるのこのグループが「ゆにこんの編集者」


def _now():
    """日時3層ルール：JST の DATETIME。"""
    return datetime.now(JST).replace(tzinfo=None)


def _fmt(dt):
    if not dt:
        return ''
    if isinstance(dt, str):
        return dt[:16]
    return dt.strftime('%Y-%m-%d %H:%M')


# ================================================================ DB

_HELPER_NAMES = ('get_db_cursor', 'get_db_connection', 'get_connection', 'get_conn', 'connect')


def _call_helper(database=None):
    last = None
    for name in _HELPER_NAMES:
        f = getattr(_db_module, name, None)
        if not callable(f):
            continue
        try:
            return f(database=database) if database else f()
        except TypeError as e:
            last = e
            for arg in ((database,) if database else ('default', None)):
                try:
                    return f(arg)
                except TypeError as e2:
                    last = e2
    raise RuntimeError('db.py に使える接続ヘルパが見つかりません: %s' % last)


@contextmanager
def _db(database=None):
    """(cursor, connection)。カーソルは辞書型。database='default' で共通 DB。"""
    obj = _call_helper(database)
    ctx = None
    if hasattr(obj, '__enter__') and not hasattr(obj, 'cursor'):
        ctx = obj
        obj = ctx.__enter__()
    if isinstance(obj, (tuple, list)):
        conn = obj[1]; owns = False
    else:
        conn = obj; owns = ctx is None
    cur = conn.cursor(dictionary=True)
    try:
        yield cur, conn
    finally:
        try:
            cur.close()
        except Exception:
            pass
        if ctx is not None:
            ctx.__exit__(None, None, None)
        elif owns:
            try:
                conn.close()
            except Exception:
                pass


# ================================================================ プロジェクトの中身（JSON）

def empty_data(title=''):
    return {'export_type': FORMAT, 'format_version': FORMAT_VERSION, 'title': title or '',
            'source': {}, 'parts': {'next_id': 1, 'quanta': {}, 'links': []},
            'mapping': {}, 'assignments': {}}


def _from_cqm_bundle(b):
    """こんかの作品ごとの書き出し（cqm_bundle）もそのまま受ける。ID はそのまま使う。"""
    quanta, links = {}, []
    for a in b.get('arenas') or []:
        attrs = dict(a.get('attrs') or {}); attrs['level'] = a.get('level') or attrs.get('level') or 'part'
        quanta[str(a['id'])] = {'id': a['id'], 'kind': 'box', 'key_path': a.get('key_path') or None,
                                'title': a.get('title') or '', 'recipe': 'arena', 'attrs': attrs,
                                'body': json.dumps(a.get('layout') or {}, ensure_ascii=False)}
    for x in b.get('boxes') or []:
        quanta[str(x['id'])] = {'id': x['id'], 'kind': 'box', 'key_path': x.get('key_path') or None,
                                'title': x.get('title') or '', 'recipe': x.get('recipe') or '',
                                'attrs': x.get('attrs') or {}, 'body': x.get('body') or ''}
    for it in b.get('items') or []:
        quanta[str(it['id'])] = {'id': it['id'], 'kind': 'item', 'key_path': None, 'title': None,
                                 'recipe': None, 'attrs': it.get('attrs') or {}, 'body': it.get('body') or ''}
        links.append({'parent': it['parent'], 'child': it['id'], 'ord': it.get('ord') or 0})
    nid = max([int(k) for k in quanta] or [0]) + 1
    d = empty_data('')
    d['source'] = {'app': 'cqm', 'format': 'cqm_bundle', 'root': b.get('root'),
                   'exported_at': b.get('exported_at') or ''}
    d['parts'] = {'next_id': nid, 'quanta': quanta, 'links': links}
    return d


def normalize(obj):
    """読み込んだ JSON をプロジェクトの形にそろえる。(data, エラー文) を返す。"""
    if not isinstance(obj, dict):
        return None, 'JSON の一番外側がオブジェクトではありません'
    et = obj.get('export_type')
    if et == 'cqm_bundle':
        return _from_cqm_bundle(obj), None
    if et != FORMAT:
        return None, 'ゆにこんのプロジェクト（export_type=%s）ではありません（%s）' % (FORMAT, et or '種別なし')
    d = empty_data(obj.get('title') or '')
    for k, v in obj.items():
        d[k] = v
    parts = d.get('parts') if isinstance(d.get('parts'), dict) else {}
    parts.setdefault('quanta', {}); parts.setdefault('links', []); parts.setdefault('next_id', 1)
    d['parts'] = parts
    d.setdefault('mapping', {}); d.setdefault('assignments', {})
    d['export_type'] = FORMAT
    return d, None


def summarize(data):
    """作品・複合部品・倉庫部品・箇条の数。"""
    s = {'works': 0, 'parts': 0, 'boxes': 0, 'items': 0}
    for q in ((data or {}).get('parts') or {}).get('quanta', {}).values():
        if q.get('kind') == 'item':
            s['items'] += 1
        elif q.get('recipe') == 'arena':
            attrs = q.get('attrs')
            if isinstance(attrs, str):
                try:
                    attrs = json.loads(attrs)
                except ValueError:
                    attrs = {}
            s['works' if (attrs or {}).get('level') == 'work' else 'parts'] += 1
        else:
            s['boxes'] += 1
    return s


def _dumps(data):
    return json.dumps(data, ensure_ascii=False)


# ================================================================ unicon_projects

_COLS = 'id, title, editor_user_id, note, data_bytes, summary, created_by, created_at, updated_by, updated_at'


def _row(r):
    if not r:
        return None
    r = dict(r)
    try:
        r['summary'] = json.loads(r.get('summary') or '{}')
    except ValueError:
        r['summary'] = {}
    r['created_at_s'] = _fmt(r.get('created_at'))
    r['updated_at_s'] = _fmt(r.get('updated_at'))
    return r


def list_projects():
    with _db() as (cur, conn):
        cur.execute('SELECT %s FROM unicon_projects WHERE deleted_at IS NULL '
                    'ORDER BY updated_at DESC, id DESC' % _COLS)
        rows = [_row(r) for r in cur.fetchall()]
    names = user_names([r.get('editor_user_id') for r in rows] + [r.get('updated_by') for r in rows])
    for r in rows:
        r['editor_name'] = names.get(r.get('editor_user_id'), '')
        r['updated_by_name'] = names.get(r.get('updated_by'), '')
    return rows


def get_project(pid, with_data=False):
    cols = _COLS + (', data' if with_data else '')
    with _db() as (cur, conn):
        cur.execute('SELECT %s FROM unicon_projects WHERE id = %%s AND deleted_at IS NULL' % cols, (pid,))
        r = _row(cur.fetchone())
    if not r:
        return None
    if with_data:
        try:
            r['data'] = json.loads(r.get('data') or '{}')
        except ValueError:
            r['data'] = empty_data(r.get('title'))
    names = user_names([r.get('editor_user_id'), r.get('updated_by'), r.get('created_by')])
    r['editor_name'] = names.get(r.get('editor_user_id'), '')
    r['updated_by_name'] = names.get(r.get('updated_by'), '')
    return r


def create_project(title, data, actor_id):
    data = data or empty_data(title)
    data['title'] = title or data.get('title') or ''
    body = _dumps(data)
    now = _now()
    with _db() as (cur, conn):
        cur.execute('INSERT INTO unicon_projects (title, editor_user_id, note, data, data_bytes, summary, '
                    'created_by, created_at, updated_by, updated_at) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (data['title'] or '（無題）', actor_id, '', body, len(body.encode('utf-8')),
                     json.dumps(summarize(data)), actor_id, now, actor_id, now))
        pid = cur.lastrowid
        conn.commit()
    return pid


def update_meta(pid, title, editor_user_id, note, actor_id):
    with _db() as (cur, conn):
        cur.execute('UPDATE unicon_projects SET title=%s, editor_user_id=%s, note=%s, updated_by=%s, updated_at=%s '
                    'WHERE id=%s AND deleted_at IS NULL',
                    (title or '（無題）', editor_user_id, note or '', actor_id, _now(), pid))
        conn.commit()


def replace_data(pid, data, actor_id, keep_title=True):
    """中身を丸ごと差し替える。題目は一覧の側を正とし，中身の title をそれに合わせる。"""
    p = get_project(pid)
    if not p:
        return False
    if keep_title:
        data['title'] = p['title']
    body = _dumps(data)
    with _db() as (cur, conn):
        cur.execute('UPDATE unicon_projects SET data=%s, data_bytes=%s, summary=%s, updated_by=%s, updated_at=%s '
                    'WHERE id=%s AND deleted_at IS NULL',
                    (body, len(body.encode('utf-8')), json.dumps(summarize(data)), actor_id, _now(), pid))
        conn.commit()
    return True


def delete_project(pid, actor_id):
    """論理削除（行は残す）。"""
    with _db() as (cur, conn):
        cur.execute('UPDATE unicon_projects SET deleted_at=%s, updated_by=%s WHERE id=%s AND deleted_at IS NULL',
                    (_now(), actor_id, pid))
        conn.commit()


# ================================================================ 利用者とグループ（共通 DB）

def _ids(v):
    out = []
    for x in v or []:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n and n not in out:
            out.append(n)
    return out


def user_names(ids):
    """{id: 氏名}（氏名が無ければメール）"""
    ids = _ids(ids)
    if not ids:
        return {}
    marks = ','.join(['%s'] * len(ids))
    try:
        with _db('default') as (cur, conn):
            cur.execute('SELECT id, full_name, email FROM users WHERE id IN (%s)' % marks, tuple(ids))
            return {r['id']: (r.get('full_name') or r.get('email') or ('#%s' % r['id'])) for r in cur.fetchall()}
    except Exception:
        return {i: '#%s' % i for i in ids}


def users_list(limit=2000):
    """編集者を選ぶための一覧（削除済み・無効を除く）。"""
    try:
        with _db('default') as (cur, conn):
            cur.execute("SELECT id, email, full_name, affiliation FROM users "
                        "WHERE deleted_at IS NULL AND COALESCE(is_active, 1) = 1 "
                        "ORDER BY full_name, email LIMIT %s", (limit,))
            return cur.fetchall()
    except Exception:
        return []


def group_names_of(user_id):
    """本人がいま所属しているまいぐるのグループ名。"""
    if not user_id:
        return set()
    now = _now()
    try:
        with _db('default') as (cur, conn):
            cur.execute("SELECT g.name FROM user_group_memberships m JOIN user_groups g ON g.id = m.group_id "
                        "WHERE m.user_id = %s AND (m.valid_from IS NULL OR m.valid_from <= %s) "
                        "AND (m.valid_until IS NULL OR m.valid_until >= %s)", (user_id, now, now))
            return {r['name'] for r in cur.fetchall() if r.get('name')}
    except Exception:
        return set()
