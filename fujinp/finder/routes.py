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

"""finder：議事録の閲覧・検索・JSON 入出力・データ構築

表は finder_minutes 1本（fujinp DB）．1行が1つの議事録で，列は
id・fiscal_year（年度）・committee（委員会名）・held_on（開催日）・body（本文）・note（備考．主に出所 URL）．
閲覧と検索はログインしている人なら誰でも，編集などの操作は admin，更新（登録）は admin と設定で選んだグループ．
"""

import hmac
import json
import logging
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from markupsafe import Markup, escape
import requests
from flask import (Response, abort, flash, redirect, render_template, request,
                   session, stream_with_context, url_for)

from config import Config

from auth import redirect_to_dashboard
from db import get_db_cursor
from decorators import login_required

from . import finder_bp as bp

logger = logging.getLogger(__name__)

TABLE = 'finder_minutes'
DB = 'fujinp'
SEARCH_LIMIT = 300
SNIPPET = 60
TERM_MAX = 100
JST = timezone(timedelta(hours=9), 'JST')
FIELDS = ('fiscal_year', 'committee', 'held_on', 'body', 'note')


def now_jst():
    return datetime.now(JST).replace(tzinfo=None)


def current_user_id():
    raw = session.get('user_id')
    try:
        return int(raw) if raw not in (None, '') else 0
    except (TypeError, ValueError):
        return 0


def is_admin():
    return session.get('user_category') == 'admin'


def require_admin():
    if not is_admin():
        abort(403)


# ── CSRF ──

CSRF_KEY = 'finder_csrf_token'


def csrf_token():
    token = session.get(CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_KEY] = token
    return token


@bp.context_processor
def inject():
    return {'csrf_token': csrf_token, 'is_admin': is_admin(), 'can_update': can_update()}


# ── アクセス（★2026-10-02） ──
# 閲覧：ログインしている人なら誰でも（区分を問わない）．
# 編集・削除・JSON 入出力・設定・登録ログなどの操作：admin だけ．
# 更新（議事録・文書・アプリの登録）：admin と，設定で選んだグループ（複数可）の構成員．
# グループの所属はまいぐる（ユーザとグループ）の公開 API で判定し，無ければ表を直接読む．グループは名前で持つ．

def _ug(name):
    try:
        from fujinp.user_groups import utils as ug_utils
        return getattr(ug_utils, name, None)
    except Exception:
        return None


def my_group_names():
    from flask import g
    if hasattr(g, 'finder_groups'):
        return g.finder_groups
    uid, names = current_user_id(), set()
    fn = _ug('get_user_group_names')
    try:
        if fn:
            names = set(fn(uid) or [])
        else:
            with get_db_cursor(database='default') as (cursor, conn):
                cursor.execute("SELECT g.name FROM user_group_memberships m JOIN user_groups g ON g.id = m.group_id "
                               "WHERE m.user_id = %s AND (m.valid_from IS NULL OR m.valid_from <= %s) "
                               "AND (m.valid_until IS NULL OR m.valid_until >= %s)", (uid, now_jst(), now_jst()))
                names = {r['name'] for r in cursor.fetchall()}
    except Exception as e:
        logger.warning('finder group lookup: %s', e)
    g.finder_groups = names
    return names


def update_groups():
    v = get_setting('update_groups', []) or []
    return [x for x in v if isinstance(x, str) and x]


def can_update():
    from flask import g
    if not hasattr(g, 'finder_can_update'):
        g.finder_can_update = is_admin() or bool(set(update_groups()) & my_group_names())
    return g.finder_can_update


def require_update():
    if not can_update():
        abort(403)


def all_group_names():
    try:
        with get_db_cursor(database='default') as (cursor, conn):
            cursor.execute("SELECT name FROM user_groups ORDER BY name")
            return [r['name'] for r in cursor.fetchall() if r['name']]
    except Exception as e:
        logger.warning('finder group list: %s', e)
        return []


@bp.before_request
def guard():
    if not current_user_id():
        return redirect(url_for('auth.login', next=request.url))
    if request.method == 'POST':
        expected = session.get(CSRF_KEY)
        submitted = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
        if not expected or not submitted or not hmac.compare_digest(str(expected), str(submitted)):
            abort(403)
    return None


# ── 共通 ──

def like_pat(term):
    return '%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def parse_date(value):
    """'YYYY-MM-DD'（/ や . 区切りも可）を date に．空は None．読めなければ ValueError"""
    if value in (None, ''):
        return None
    if isinstance(value, date):
        return value
    m = re.fullmatch(r'\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*', str(value))
    if not m:
        raise ValueError(f'開催日 {value!r} は YYYY-MM-DD で書いてください')
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def parse_year(value):
    if value in (None, ''):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        raise ValueError(f'年度 {value!r} は整数で書いてください')


def filters_from(args):
    year = args.get('year', '').strip()
    committee = args.get('committee', '').strip()
    where, params = [], []
    if year:
        where.append('fiscal_year = %s')
        params.append(int(year) if year.isdigit() else -1)
    if committee:
        where.append('committee = %s')
        params.append(committee)
    return year, committee, where, params


def facets():
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute(f"SELECT fiscal_year, committee, COUNT(*) AS n FROM {TABLE} "
                       "GROUP BY fiscal_year, committee ORDER BY fiscal_year DESC, committee")
        rows = cursor.fetchall()
    years = sorted({r['fiscal_year'] for r in rows if r['fiscal_year'] is not None}, reverse=True)
    committees = sorted({r['committee'] for r in rows if r['committee']})
    return rows, years, committees


# ── Drive の原本をサーバ経由で見せる（★2026-10-01） ──
# 閲覧者のブラウザのアカウントでは開けない原本を，サイトの通行証（config.py）で取り寄せて渡す．
# 通行証は UNIV_DRIVE_REFRESH_TOKEN を優先し，無ければ DRIVE_REFRESH_TOKEN．
# クライアントは UNIV_DRIVE_CLIENT_ID / _SECRET を優先し，無ければ DRIVE_CLIENT_ID / _SECRET．

DRIVE_FILE_RE = re.compile(r'https://(?:drive\.google\.com/file/d|docs\.google\.com/(?:document|spreadsheets|presentation)/d)/([A-Za-z0-9_-]+)')
_token_cache = {'token': None, 'expires': 0.0}


def drive_file_id(note):
    m = DRIVE_FILE_RE.search(note or '')
    return m.group(1) if m else None


def drive_access_token():
    import time
    if _token_cache['token'] and _token_cache['expires'] > time.time() + 60:
        return _token_cache['token']
    cid = getattr(Config, 'UNIV_DRIVE_CLIENT_ID', None) or getattr(Config, 'DRIVE_CLIENT_ID', None)
    secret = getattr(Config, 'UNIV_DRIVE_CLIENT_SECRET', None) or getattr(Config, 'DRIVE_CLIENT_SECRET', None)
    refresh = getattr(Config, 'UNIV_DRIVE_REFRESH_TOKEN', None) or getattr(Config, 'DRIVE_REFRESH_TOKEN', None)
    if not (cid and secret and refresh):
        raise RuntimeError('config.py に Drive の通行証（UNIV_DRIVE_* または DRIVE_*）がありません')
    r = requests.post('https://oauth2.googleapis.com/token', data={
        'client_id': cid, 'client_secret': secret, 'refresh_token': refresh,
        'grant_type': 'refresh_token'}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f'Drive の通行証が使えません（{r.status_code}）')
    d = r.json()
    _token_cache['token'] = d['access_token']
    _token_cache['expires'] = time.time() + int(d.get('expires_in', 3600))
    return _token_cache['token']


# ── 検索式 ──

def parse_query(text):
    """1行が1つのまとまり．行の中は空白（全角も可）区切り．空のまとまりは捨てる"""
    groups = []
    for line in (text or '').splitlines():
        terms = [t[:TERM_MAX] for t in re.split(r'[\s\u3000]+', line.strip()) if t]
        if terms:
            groups.append(terms)
    return groups[:20]


def build_search(groups, mode):
    """mode='and_or'：行どうし AND，行の中 OR．mode='or_and'：行どうし OR，行の中 AND"""
    inner_op, outer_op = (' OR ', ' AND ') if mode == 'and_or' else (' AND ', ' OR ')
    parts, params = [], []
    for terms in groups:
        conds = []
        for t in terms:
            conds.append('body LIKE %s')
            params.append(like_pat(t))
        parts.append('(' + inner_op.join(conds) + ')')
    return '(' + outer_op.join(parts) + ')', params


def snippet(body, terms):
    if not body:
        return ''
    best = None
    for t in terms:
        i = body.find(t)
        if i >= 0 and (best is None or i < best[0]):
            best = (i, t)
    if best is None:
        return body[:SNIPPET * 2]
    i = best[0]
    start = max(i - SNIPPET, 0)
    return ('…' if start else '') + body[start:i + len(best[1]) + SNIPPET] + '…'


@bp.app_template_filter('finder_mark')
def finder_mark(text, terms):
    """本文を HTML エスケープしてから検索語を <mark> で囲む"""
    text = text or ''
    if not terms:
        return escape(text)
    pat = re.compile('|'.join(re.escape(t) for t in terms))
    out, pos = [], 0
    for m in pat.finditer(text):
        out.append(str(escape(text[pos:m.start()])))
        out.append('<mark>' + str(escape(m.group(0))) + '</mark>')
        pos = m.end()
    out.append(str(escape(text[pos:])))
    return Markup(''.join(out))


# ── 設定（委員会の並び） ──
# finder_settings（k, v）に JSON で持つ．committee_order：表示順の委員会名の配列，
# committee_hidden：一覧に出さない委員会名の配列．設定に無い委員会は末尾に名前順で並ぶ．

SETTINGS_TABLE = 'finder_settings'
SETTINGS_DDL = (f"CREATE TABLE IF NOT EXISTS `{SETTINGS_TABLE}` ("
                "`k` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL, "
                "`v` mediumtext COLLATE utf8mb4_unicode_ci, "
                "`updated_at` datetime DEFAULT NULL, PRIMARY KEY (`k`)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")


def get_setting(key, default=None):
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute(SETTINGS_DDL)
        cursor.execute(f"SELECT v FROM {SETTINGS_TABLE} WHERE k = %s", (key,))
        row = cursor.fetchone()
    if not row or row['v'] in (None, ''):
        return default
    try:
        return json.loads(row['v'])
    except ValueError:
        return default


def put_setting(key, value):
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute(SETTINGS_DDL)
        cursor.execute(f"REPLACE INTO {SETTINGS_TABLE} (k, v, updated_at) VALUES (%s, %s, %s)",
                       (key, json.dumps(value, ensure_ascii=False), now_jst()))
        conn.commit()


def committee_ranks():
    """{委員会名: 順序数}．古い並び（committee_order）しか無ければ 1, 2, 3, … に読み替える"""
    ranks = get_setting('committee_rank')
    if isinstance(ranks, dict):
        out = {}
        for k, v in ranks.items():
            try:
                out[k] = round(float(v), 2)
            except (TypeError, ValueError):
                pass
        return out
    order = get_setting('committee_order', []) or []
    return {c: float(i + 1) for i, c in enumerate(order)}


def committee_list(include_hidden=False):
    """[{'name', 'n', 'rank', 'hidden'}] を順序数の小さい順に返す．順序数の無い委員会は末尾に名前順．
    議事録の無い委員会も，設定 committee_extra に登録してあれば件数0で出す"""
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute(f"SELECT committee, COUNT(*) AS n FROM {TABLE} GROUP BY committee")
        counts = {r['committee'] or '': r['n'] for r in cursor.fetchall()}
    for c in get_setting('committee_extra', []) or []:   # 議事録の無い委員会も一覧に出す
        counts.setdefault(c, 0)
    ranks = committee_ranks()
    hidden = set(get_setting('committee_hidden', []) or [])
    names = sorted(counts, key=lambda c: (c not in ranks, ranks.get(c, 0), c))
    out = [{'name': c, 'n': counts[c], 'rank': ranks.get(c), 'hidden': c in hidden} for c in names]
    return out if include_hidden else [x for x in out if not x['hidden']]


def meetings_of(committee):
    """委員会の全議事録：年度は新しい順，年度内は開催日の古い順（日付なしは末尾）"""
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute(f"SELECT id, fiscal_year, held_on, CHAR_LENGTH(body) AS chars FROM {TABLE} "
                       "WHERE committee = %s ORDER BY fiscal_year IS NULL, fiscal_year DESC, "
                       "held_on IS NULL, held_on, id", (committee,))
        return cursor.fetchall()


def minutes_full(committee):
    """委員会の全議事録を本文つきで，索引（meetings_of）と同じ順に"""
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute(f"SELECT * FROM {TABLE} WHERE committee = %s ORDER BY fiscal_year IS NULL, "
                       "fiscal_year DESC, held_on IS NULL, held_on, id", (committee,))
        return cursor.fetchall()


def minute_row(minute_id):
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute(f"SELECT * FROM {TABLE} WHERE id = %s", (minute_id,))
        return cursor.fetchone()


# ── 閲覧（3カラム：委員会｜開催日｜議事録）★2026-10-01 ──

@bp.route('/')
@login_required
def index():
    committees = committee_list()
    c = request.args.get('c', request.args.get('committee', '')).strip()
    mid = request.args.get('m', type=int)
    m = minute_row(mid) if mid else None
    if m and not c:
        c = m['committee'] or ''
    meetings = meetings_of(c) if c else []
    return render_template('finder/index.html', committees=committees, c=c,
                           meetings=meetings, m=m, terms=[],
                           minutes=minutes_full(c) if c else [])


@bp.route('/pane/stream')
@login_required
def pane_stream():
    """委員会の全議事録をつなげた読み物（★2026-10-02）．日付の索引から飛ぶ"""
    c = request.args.get('c', '').strip()
    return render_template('finder/_stream.html', c=c, minutes=minutes_full(c) if c else [], terms=[])


@bp.route('/pane/dates')
@login_required
def pane_dates():
    c = request.args.get('c', '').strip()
    return render_template('finder/_dates.html', c=c, meetings=meetings_of(c), m=None)


@bp.route('/pane/minute/<int:minute_id>')
@login_required
def pane_minute(minute_id):
    m = minute_row(minute_id)
    if not m:
        abort(404)
    return render_template('finder/_minute.html', m=m, terms=[],
                           editable=is_admin() and request.args.get('editable') == '1')


# ── 議事録の編集（admin）★2026-10-02 ──
# 本文は2次資料（原本から起こしたテキスト）なので，admin が通し読みの画面でその場で直せるようにする．
# 応答は JSON：{ok, html（描き直した回）, moved（委員会を変えて別の委員会へ移ったか）, error}

@bp.route('/pane/minute/<int:minute_id>/edit')
@login_required
def pane_minute_edit(minute_id):
    require_admin()
    m = minute_row(minute_id)
    if not m:
        abort(404)
    return render_template('finder/_minute_edit.html', m=m)


@bp.route('/m/<int:minute_id>/save', methods=['POST'])
@login_required
def minute_save(minute_id):
    require_admin()
    m = minute_row(minute_id)
    if not m:
        return {'ok': False, 'error': 'この議事録は見つかりません'}, 404
    f = request.form
    try:
        vals = {'fiscal_year': parse_year((f.get('fiscal_year') or '').strip()),
                'committee': (f.get('committee') or '').strip(),
                'held_on': parse_date((f.get('held_on') or '').strip()),
                'note': (f.get('note') or '').strip() or None,
                'body': (f.get('body') or '').replace('\r\n', '\n')}
    except ValueError as e:
        return {'ok': False, 'error': str(e)}
    if not vals['committee']:
        return {'ok': False, 'error': '委員会名を書いてください'}
    if len(vals['committee']) > 200:
        return {'ok': False, 'error': '委員会名は200文字までです'}
    with get_db_cursor(database=DB) as (cursor, conn):
        sets = ', '.join(f'{k} = %s' for k in vals) + ', updated_at = %s'
        cursor.execute(f"UPDATE {TABLE} SET {sets} WHERE id = %s", list(vals.values()) + [now_jst(), minute_id])
        conn.commit()
    logger.info('finder minute save: id=%s by %s', minute_id, current_user_id())
    write_log('議事録', '編集', minute_id, f"{vals['committee']} {vals['held_on'] or ''}",
              dict({k: str(m[k]) + ' → ' + str(vals[k]) for k in vals if k != 'body' and m[k] != vals[k]},
                   **({'body': f"{len(m['body'] or '')} → {len(vals['body'] or '')} 文字"}
                      if (m['body'] or '') != (vals['body'] or '') else {})) or None)
    row = minute_row(minute_id)
    return {'ok': True, 'moved': row['committee'] != m['committee'],
            'resorted': (row['fiscal_year'], row['held_on']) != (m['fiscal_year'], m['held_on']),
            'html': render_template('finder/_minute.html', m=row, terms=[], editable=True)}


@bp.route('/m/<int:minute_id>/delete', methods=['POST'])
@login_required
def minute_delete(minute_id):
    require_admin()
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute(f"DELETE FROM {TABLE} WHERE id = %s", (minute_id,))
        n = cursor.rowcount
        conn.commit()
    logger.info('finder minute delete: id=%s by %s', minute_id, current_user_id())
    if n:
        write_log('議事録', '削除', minute_id, None)
    return {'ok': bool(n)}


RANK_RE = re.compile(r'-?\d{1,6}(\.\d{1,2})?')


@bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """委員会ごとに順序数（小数点以下2桁まで）を付け，小さい順に並べる．空欄は末尾に名前順"""
    require_admin()
    if request.method == 'POST':
        names = request.form.getlist('name')
        ranks, hidden, bad = {}, [], []
        for i, name in enumerate(names):
            raw = (request.form.get(f'rank_{i}') or '').strip().translate(
                str.maketrans('０１２３４５６７８９．－', '0123456789.-'))
            if raw:
                if not RANK_RE.fullmatch(raw):
                    bad.append(f'{name or "（委員会未定）"}：{raw}')
                    continue
                ranks[name] = round(float(raw), 2)
            if request.form.get(f'hide_{i}'):
                hidden.append(name)
        # 委員会の追加（1行に1つ）と，議事録の無い委員会の削除
        extra = [c for c in (get_setting('committee_extra', []) or [])]
        drop = set(request.form.getlist('drop'))
        extra = [c for c in extra if c not in drop]
        for line in (request.form.get('add') or '').splitlines():
            c = line.strip()[:200]
            if c and c not in extra and c not in names:
                extra.append(c)
        if bad:
            flash('順序数は小数点以下2桁までの数で書いてください（' + '，'.join(bad) + '）', 'error')
            return redirect(url_for('finder.settings'))
        put_setting('committee_hidden', [c for c in hidden if c not in drop])
        put_setting('committee_extra', extra)
        for c in drop:
            ranks.pop(c, None)
        put_setting('committee_rank', ranks)
        flash('委員会の設定を保存しました', 'success')
        return redirect(url_for('finder.settings'))
    return render_template('finder/settings.html', committees=committee_list(include_hidden=True),
                           groups=all_group_names(), update_groups=update_groups())


@bp.route('/settings/groups', methods=['POST'])
@login_required
def settings_groups():
    """更新できるグループ（複数）を保存する．admin だけ"""
    require_admin()
    chosen = sorted({x.strip() for x in request.form.getlist('group') if x.strip()})
    before = update_groups()
    put_setting('update_groups', chosen)
    write_log('設定', '更新グループ', None, '，'.join(chosen) or '（なし）',
              {'before': before, 'after': chosen})
    flash('更新できるグループを保存しました（' + ('，'.join(chosen) or 'admin のみ') + '）', 'success')
    return redirect(url_for('finder.settings'))


@bp.route('/m/<int:minute_id>')
@login_required
def show(minute_id):
    row = minute_row(minute_id)
    if not row:
        abort(404)
    terms = sorted({t for g in parse_query(request.args.get('q', '')) for t in g}, key=len, reverse=True)
    return render_template('finder/show.html', m=row, terms=terms, q=request.args.get('q', ''),
                           mode=request.args.get('mode', 'and_or'))


def stream_drive(url, back):
    """Drive の原本をサイトの通行証で取り寄せてそのまま返す．失敗したら back へ戻して理由を出す"""
    fid = drive_file_id(url)
    if not fid:
        abort(404)
    try:
        h = {'Authorization': f'Bearer {drive_access_token()}'}
        meta = requests.get(f'https://www.googleapis.com/drive/v3/files/{fid}',
                            params={'fields': 'name,mimeType', 'supportsAllDrives': 'true'},
                            headers=h, timeout=30)
        if meta.status_code != 200:
            raise RuntimeError(f'原本の情報を取れません（Drive {meta.status_code}）．'
                               'サイトの通行証のアカウントに，このファイルを見る権限が無い可能性があります')
        m = meta.json()
        if m.get('mimeType', '').startswith('application/vnd.google-apps.'):
            src = f'https://www.googleapis.com/drive/v3/files/{fid}/export'
            params = {'mimeType': 'application/pdf'}
            ctype, name = 'application/pdf', m.get('name', 'document') + '.pdf'
        else:
            src = f'https://www.googleapis.com/drive/v3/files/{fid}'
            params = {'alt': 'media', 'supportsAllDrives': 'true'}
            ctype, name = m.get('mimeType') or 'application/octet-stream', m.get('name', 'file')
        r = requests.get(src, params=params, headers=h, stream=True, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f'原本を取り寄せられません（Drive {r.status_code}）')
    except (RuntimeError, requests.RequestException) as e:
        logger.warning('finder original %s: %s', url, e)
        flash(str(e), 'error')
        return redirect(back)
    from urllib.parse import quote
    return Response(stream_with_context(r.iter_content(64 * 1024)), content_type=ctype,
                    headers={'Content-Disposition': "inline; filename*=UTF-8''" + quote(name),
                             'Cache-Control': 'private, max-age=600'})


@bp.route('/m/<int:minute_id>/original')
@login_required
def original(minute_id):
    """備考の Drive 原本を，サイトの通行証で取り寄せてそのまま見せる"""
    row = minute_row(minute_id)
    if not row:
        abort(404)
    return stream_drive(row['note'], url_for('finder.show', minute_id=minute_id))


# ── 文書類・アプリ（finder_items）★2026-10-01 ──
# 1行が1つの文書（様式を含む）・フォルダ，またはアプリへのリンク．item_type は「文書類」か「アプリ」．
# category は学内情報ポータル等の見出し，sort_order は同じ親の中での順．
# parent_id（★2026-10-01 夜）：文書類の入れ子．NULL なら分類の直下，値があればその行（フォルダ）の中．
# 子の category は親と同じに揃える．

ITEMS = 'finder_items'
ITEM_TYPES = ('文書類', 'アプリ')
ITEM_FIELDS = ('item_type', 'category', 'title', 'url', 'body', 'note', 'sort_order', 'parent_id')
ITEMS_DDL = (f"CREATE TABLE IF NOT EXISTS `{ITEMS}` ("
             "`id` int NOT NULL AUTO_INCREMENT, "
             "`item_type` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL, "
             "`category` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
             "`title` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
             "`url` text COLLATE utf8mb4_unicode_ci, "
             "`body` longtext COLLATE utf8mb4_unicode_ci, "
             "`note` text COLLATE utf8mb4_unicode_ci, "
             "`sort_order` decimal(10,2) DEFAULT NULL, "
             "`parent_id` int DEFAULT NULL, "
             "`created_at` datetime DEFAULT NULL, `updated_at` datetime DEFAULT NULL, "
             "PRIMARY KEY (`id`), KEY `idx_type_cat` (`item_type`,`category`,`sort_order`), "
             "KEY `idx_parent` (`parent_id`)"
             ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
_items_ready = {'ok': False}
DRIVE_FOLDER_RE = re.compile(r'https://drive\.google\.com/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)')
FOLDER_MIME = 'application/vnd.google-apps.folder'
IMPORT_MAX = 3000          # フォルダ取り込み1回あたりの上限件数
IMPORT_DEPTH = 8           # 下の階層までたどるときの深さの上限


def items_cursor():
    """finder_items が無ければ作り，parent_id 列が無ければ足してからカーソルを渡す"""
    if not _items_ready['ok']:
        with get_db_cursor(database=DB) as (cursor, conn):
            cursor.execute(ITEMS_DDL)
            cursor.execute(f"SHOW COLUMNS FROM {ITEMS} LIKE 'parent_id'")
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE {ITEMS} ADD COLUMN `parent_id` int DEFAULT NULL AFTER `sort_order`, "
                               "ADD KEY `idx_parent` (`parent_id`)")
            conn.commit()
        _items_ready['ok'] = True
    return get_db_cursor(database=DB)


def item_row(item_id):
    with items_cursor() as (cursor, conn):
        cursor.execute(f"SELECT * FROM {ITEMS} WHERE id = %s", (item_id,))
        return cursor.fetchone()


def drive_folder_id(url):
    m = DRIVE_FOLDER_RE.search(url or '')
    return m.group(1) if m else None


def url_kind(url):
    """表示用：none（URL なし）／drive（原本を取り寄せられる）／folder（Drive フォルダ）／web"""
    u = (url or '').strip()
    if not u:
        return 'none'
    if drive_file_id(u):
        return 'drive'
    if drive_folder_id(u):
        return 'folder'
    return 'web'


def embeddable(it):
    """第3カラムに原本をそのまま埋め込めるか．PDF と Google ドキュメント類（PDF に書き出して見せる）"""
    if url_kind(it.get('url')) != 'drive':
        return False
    return 'docs.google.com/' in (it.get('url') or '') or (it.get('title') or '').lower().endswith('.pdf')


@bp.app_template_filter('finder_is_image')
def finder_is_image(it):
    return url_kind(it.get('url')) == 'drive' and (it.get('title') or '').lower().endswith(IMAGE_EXT)


@bp.app_template_filter('finder_embeddable')
def finder_embeddable(it):
    return embeddable(it)


LINK_RE = re.compile(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)|(https?://[^\s<>()（）「」，、。]+)')


@bp.app_template_filter('finder_linkify')
def finder_linkify(text):
    """本文を HTML エスケープし，[文字](URL) と裸の URL をリンクにする（★2026-10-01）"""
    text = text or ''
    out, pos = [], 0
    for m in LINK_RE.finditer(text):
        out.append(str(escape(text[pos:m.start()])))
        label, url = (m.group(1), m.group(2)) if m.group(2) else (m.group(3), m.group(3))
        out.append(f'<a href="{escape(url)}" target="_blank" rel="noopener">{escape(label)}</a>')
        pos = m.end()
    out.append(str(escape(text[pos:])))
    return Markup(''.join(out))


@bp.app_template_filter('finder_url_kind')
def finder_url_kind(url):
    return url_kind(url)


def doc_tree():
    """文書類の木：[{'name': 分類, 'n': 件数, 'nodes': [節点…]}]．節点は行に 'children' を足したもの．
    親の見つからない行は分類の直下に出す（取りこぼさない）"""
    with items_cursor() as (cursor, conn):
        cursor.execute(f"SELECT id, category, title, url, note, parent_id, sort_order, "
                       f"CHAR_LENGTH(body) AS chars FROM {ITEMS} WHERE item_type = '文書類' "
                       "ORDER BY sort_order IS NULL, sort_order, id")
        rows = cursor.fetchall()
    by_id = {r['id']: dict(r, children=[]) for r in rows}
    cats, order = {}, []
    for r in rows:
        node = by_id[r['id']]
        p = by_id.get(r['parent_id']) if r['parent_id'] else None
        if p is not None and p['id'] != node['id']:
            p['children'].append(node)
            continue
        c = r['category'] or ''
        if c not in cats:
            cats[c] = {'name': c, 'nodes': [], 'first': (r['sort_order'] is None, r['sort_order'] or 0)}
            order.append(c)
        cats[c]['nodes'].append(node)

    def count(n):
        return 1 + sum(count(ch) for ch in n['children'])
    out = sorted((cats[c] for c in order), key=lambda x: (x['first'], x['name']))
    for x in out:
        x['n'] = sum(count(n) for n in x['nodes'])
    return out, by_id


def ancestors(by_id, item_id):
    """item_id の祖先の id（根から順）．輪があっても止まる"""
    path, seen = [], set()
    cur = by_id.get(item_id)
    while cur and cur['parent_id'] and cur['parent_id'] not in seen and cur['parent_id'] in by_id:
        seen.add(cur['parent_id'])
        path.append(cur['parent_id'])
        cur = by_id[cur['parent_id']]
    return list(reversed(path))


def descendants(cursor, item_id):
    """item_id の子孫の id をすべて（幅優先）"""
    out, frontier = [], [item_id]
    while frontier and len(out) < 20000:
        cursor.execute(f"SELECT id FROM {ITEMS} WHERE parent_id IN ({','.join(['%s'] * len(frontier))})",
                       frontier)
        frontier = [r['id'] for r in cursor.fetchall() if r['id'] not in out and r['id'] != item_id]
        out += frontier
    return out


def child_count(item_id):
    with items_cursor() as (cursor, conn):
        cursor.execute(f"SELECT COUNT(*) AS n FROM {ITEMS} WHERE parent_id = %s", (item_id,))
        return cursor.fetchone()['n']


@bp.route('/docs')
@login_required
def docs():
    """文書類：左が分類とフォルダの木，右が中身（★2026-10-01 夜 2カラム化）"""
    tree, by_id = doc_tree()
    iid = request.args.get('i', type=int)
    c = request.args.get('c', '').strip()
    it = item_row(iid) if iid else None
    if it and it['item_type'] != '文書類':
        it = None
    if it:
        c = it['category'] or ''
    open_ids = set(ancestors(by_id, it['id'])) if it else set()
    form = None                                   # admin の編集・追加フォーム
    if is_admin():
        if request.args.get('new') == '1':
            parent = item_row(request.args.get('parent', type=int) or 0) if request.args.get('parent') else None
            form = {'id': None, 'item_type': '文書類', 'category': parent['category'] if parent else c,
                    'title': '', 'url': '', 'note': '', 'body': '', 'sort_order': None,
                    'parent_id': parent['id'] if parent else None}
            if parent:
                open_ids |= set(ancestors(by_id, parent['id'])) | {parent['id']}
            it = None
        elif it and request.args.get('edit') == '1':
            form = it
    return render_template('finder/docs.html', tree=tree, c=c, it=it, open_ids=open_ids, form=form,
                           n_children=child_count(it['id']) if it else 0,
                           all_cats=all_item_categories() if form else [])


@bp.route('/pane/item/<int:item_id>')
@login_required
def pane_item(item_id):
    it = item_row(item_id)
    if not it:
        abort(404)
    return render_template('finder/_item.html', it=it, n_children=child_count(item_id))


@bp.route('/i/<int:item_id>/original')
@login_required
def item_original(item_id):
    it = item_row(item_id)
    if not it:
        abort(404)
    return stream_drive(it['url'], url_for('finder.docs', i=item_id))


def all_item_categories():
    with items_cursor() as (cursor, conn):
        cursor.execute(f"SELECT DISTINCT category FROM {ITEMS} WHERE category IS NOT NULL AND category <> '' "
                       "ORDER BY category")
        return [r['category'] for r in cursor.fetchall()]


@bp.route('/i/save', methods=['POST'])
@login_required
def item_save():
    """文書類・アプリの1件を保存（id があれば上書き，無ければ追加）．admin だけ"""
    require_admin()
    f = request.form
    rid = f.get('id', type=int)
    vals = {
        'item_type': (f.get('item_type') or '文書類').strip(),
        'category': (f.get('category') or '').strip()[:200],
        'title': (f.get('title') or '').strip()[:500],
        'url': (f.get('url') or '').strip(),
        'note': (f.get('note') or '').strip(),
        'body': (f.get('body') or '').replace('\r\n', '\n'),
    }
    raw = (f.get('sort_order') or '').strip().translate(str.maketrans('０１２３４５６７８９．－', '0123456789.-'))
    praw = (f.get('parent_id') or '').strip().translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    back = url_for('finder.docs', i=rid, edit=1) if rid else url_for('finder.docs', c=vals['category'], new=1)
    if vals['item_type'] not in ITEM_TYPES:
        flash('種類は「文書類」か「アプリ」です', 'error'); return redirect(back)
    if not vals['title']:
        flash('題名を書いてください', 'error'); return redirect(back)
    if raw and not RANK_RE.fullmatch(raw):
        flash('並び順は小数点以下2桁までの数で書いてください', 'error'); return redirect(back)
    if praw and not praw.isdigit():
        flash('親の ID は整数で書いてください（空なら分類の直下）', 'error'); return redirect(back)
    pid = int(praw) if praw else None
    stamp = now_jst()
    with items_cursor() as (cursor, conn):
        if pid is not None:
            cursor.execute(f"SELECT id, category, item_type FROM {ITEMS} WHERE id = %s", (pid,))
            p = cursor.fetchone()
            if not p or p['item_type'] != '文書類' or vals['item_type'] != '文書類':
                flash('親には文書類の行（フォルダ）の ID を書いてください', 'error'); return redirect(back)
            if rid and (pid == rid or pid in descendants(cursor, rid)):
                flash('自分自身や自分の中のフォルダを親にはできません', 'error'); return redirect(back)
            vals['category'] = p['category'] or ''
        vals['parent_id'] = pid
        if raw:
            vals['sort_order'] = round(float(raw), 2)
        elif not rid:   # 追加で並び順が空なら，同じ親の末尾に置く
            if pid is None:
                cursor.execute(f"SELECT MAX(sort_order) AS o FROM {ITEMS} WHERE item_type = %s AND category = %s "
                               "AND parent_id IS NULL", (vals['item_type'], vals['category']))
            else:
                cursor.execute(f"SELECT MAX(sort_order) AS o FROM {ITEMS} WHERE parent_id = %s", (pid,))
            o = cursor.fetchone()['o']
            vals['sort_order'] = float(o) + 1 if o is not None else 1.0
        else:
            vals['sort_order'] = None
        for k in ('url', 'note', 'body'):
            if vals[k] == '':
                vals[k] = None
        if rid:
            cursor.execute(f"SELECT id FROM {ITEMS} WHERE id = %s", (rid,))
            if not cursor.fetchone():
                abort(404)
            sets = ', '.join(f'{k} = %s' for k in vals) + ', updated_at = %s'
            cursor.execute(f"UPDATE {ITEMS} SET {sets} WHERE id = %s", list(vals.values()) + [stamp, rid])
            sub = descendants(cursor, rid)         # 分類を変えたら中身も揃える
            for i in range(0, len(sub), 500):
                chunk = sub[i:i + 500]
                cursor.execute(f"UPDATE {ITEMS} SET category = %s, item_type = %s "
                               f"WHERE id IN ({','.join(['%s'] * len(chunk))})",
                               [vals['category'], vals['item_type']] + chunk)
        else:
            cols = list(vals) + ['created_at', 'updated_at']
            cursor.execute(f"INSERT INTO {ITEMS} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                           list(vals.values()) + [stamp, stamp])
            rid = cursor.lastrowid
        conn.commit()
    logger.info('finder item save: id=%s by %s', rid, current_user_id())
    write_log(vals['item_type'], '編集' if f.get('id') else '登録', rid, vals['title'], {'url': vals['url']})
    flash('保存しました', 'success')
    if vals['item_type'] == 'アプリ':
        return redirect(url_for('finder.apps'))
    return redirect(url_for('finder.docs', i=rid))


@bp.route('/i/<int:item_id>/delete', methods=['POST'])
@login_required
def item_delete(item_id):
    """1件を消す．フォルダなら中身もいっしょに消す"""
    require_admin()
    it = item_row(item_id)
    if not it:
        abort(404)
    with items_cursor() as (cursor, conn):
        ids = [item_id] + descendants(cursor, item_id)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            cursor.execute(f"DELETE FROM {ITEMS} WHERE id IN ({','.join(['%s'] * len(chunk))})", chunk)
        conn.commit()
    logger.info('finder item delete: id=%s (+%s) by %s', item_id, len(ids) - 1, current_user_id())
    write_log(it['item_type'], '削除', item_id, it['title'], {'with_children': len(ids) - 1} if len(ids) > 1 else None)
    flash(f'「{it["title"]}」を消しました' + (f'（中身 {len(ids) - 1} 件も）' if len(ids) > 1 else ''), 'success')
    if it['item_type'] == 'アプリ':
        return redirect(url_for('finder.apps'))
    if it['parent_id']:
        return redirect(url_for('finder.docs', i=it['parent_id']))
    return redirect(url_for('finder.docs', c=it['category']))


# ── Drive フォルダの中身を取り込む（admin）★2026-10-01 夜 ──
# サイトの通行証（UNIV_DRIVE_* / DRIVE_*）でフォルダの一覧を取り，子の行として埋め込む．
# 同じ親の下に同じ URL の行があれば題名だけ揃え，無ければ足す．消すことはしない（何度押しても同じ結果）．

def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', s or '')]


def drive_list(folder_id, headers):
    files, token = [], None
    while True:
        params = {'q': f"'{folder_id}' in parents and trashed = false",
                  'fields': 'nextPageToken, files(id, name, mimeType)', 'pageSize': 1000,
                  'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'}
        if token:
            params['pageToken'] = token
        r = requests.get('https://www.googleapis.com/drive/v3/files', params=params, headers=headers, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f'フォルダの一覧を取れません（Drive {r.status_code}）．'
                               'サイトの通行証のアカウントに，このフォルダを見る権限が無い可能性があります')
        d = r.json()
        files += d.get('files', [])
        token = d.get('nextPageToken')
        if not token or len(files) > IMPORT_MAX:
            break
    # フォルダを先に，名前は数字を数として並べる
    files.sort(key=lambda x: (x.get('mimeType') != FOLDER_MIME, natural_key(x.get('name'))))
    return files


GOOGLE_DOC_KINDS = {'application/vnd.google-apps.document': 'document',
                    'application/vnd.google-apps.spreadsheet': 'spreadsheets',
                    'application/vnd.google-apps.presentation': 'presentation'}


def drive_url(f):
    m = f.get('mimeType')
    if m == FOLDER_MIME:
        return f"https://drive.google.com/drive/folders/{f['id']}"
    if m in GOOGLE_DOC_KINDS:
        return f"https://docs.google.com/{GOOGLE_DOC_KINDS[m]}/d/{f['id']}"
    return f"https://drive.google.com/file/d/{f['id']}/view"


IMPORT_SECONDS = 200       # 1回の押下で使う時間の上限（Web ワーカーの時間切れを避ける）


def import_folders(roots, deep, max_rows=IMPORT_MAX):
    """roots（finder_items の行の並び）の Drive フォルダの中身を子の行として埋め込む．
    同じ親の下に同じ URL の行があれば題名だけ揃え，無ければ足す．消すことはしない．
    フォルダ1つごとに commit するので，途中で止まっても読んだ分は残る．
    戻り値：{'folders', 'insert', 'update', 'failed': [題名…], 'stopped': 時間・件数の上限で止めたか}"""
    import time
    t0 = time.time()
    stats = {'folders': 0, 'insert': 0, 'update': 0, 'failed': [], 'stopped': False}
    h = {'Authorization': f'Bearer {drive_access_token()}'}
    queue = [(r['id'], drive_folder_id(r['url']), r['category'], r['title'], 0)
             for r in roots if drive_folder_id(r.get('url'))]
    with items_cursor() as (cursor, conn):
        while queue:
            if time.time() - t0 > IMPORT_SECONDS or stats['insert'] + stats['update'] >= max_rows:
                stats['stopped'] = True
                break
            pid, fid, cat, title, depth = queue.pop(0)
            try:
                files = drive_list(fid, h)
            except (RuntimeError, requests.RequestException) as e:
                logger.warning('finder import folder %s: %s', pid, e)
                stats['failed'].append(title or str(pid))
                continue
            stats['folders'] += 1
            stamp = now_jst()
            cursor.execute(f"SELECT id, url, title FROM {ITEMS} WHERE parent_id = %s", (pid,))
            have = {r['url']: r for r in cursor.fetchall()}
            for n, f in enumerate(files, 1):
                url = drive_url(f)
                if url in have:
                    cid = have[url]['id']
                    if have[url]['title'] != f.get('name'):
                        cursor.execute(f"UPDATE {ITEMS} SET title = %s, updated_at = %s WHERE id = %s",
                                       (f.get('name'), stamp, cid))
                        stats['update'] += 1
                else:
                    cursor.execute(f"INSERT INTO {ITEMS} (item_type, category, title, url, note, sort_order, "
                                   "parent_id, created_at, updated_at) VALUES ('文書類',%s,%s,%s,NULL,%s,%s,%s,%s)",
                                   (cat, f.get('name'), url, n, pid, stamp, stamp))
                    cid = cursor.lastrowid
                    stats['insert'] += 1
                if deep and f.get('mimeType') == FOLDER_MIME and depth + 1 < IMPORT_DEPTH:
                    queue.append((cid, f['id'], cat, f.get('name'), depth + 1))
            conn.commit()
    return stats


def import_message(stats):
    msg = (f"フォルダ {stats['folders']} 個を読み，{stats['insert']} 件を足し，"
           f"{stats['update']} 件の題名を揃えました")
    if stats['failed']:
        msg += f"．読めなかったフォルダ {len(stats['failed'])} 個（" + '，'.join(stats['failed'][:5]) + \
               ('，…' if len(stats['failed']) > 5 else '') + '）'
    if stats['stopped']:
        msg += '．時間の上限で途中までにしました．もう一度押すと続きを読みます'
    return msg


@bp.route('/i/<int:item_id>/import_folder', methods=['POST'])
@login_required
def item_import_folder(item_id):
    """1つのフォルダの中身を取り込む（「下の階層も」で深くたどる）"""
    require_admin()
    it = item_row(item_id)
    if not it or it['item_type'] != '文書類':
        abort(404)
    if not drive_folder_id(it['url']):
        flash('URL が Google Drive のフォルダではありません', 'error')
        return redirect(url_for('finder.docs', i=item_id))
    try:
        stats = import_folders([it], request.form.get('deep') == '1')
    except (RuntimeError, requests.RequestException) as e:
        flash(str(e), 'error')
        return redirect(url_for('finder.docs', i=item_id))
    logger.info('finder import_folder: id=%s %s by %s', item_id, stats, current_user_id())
    write_log('文書類', 'フォルダ展開', item_id, it['title'], stats)
    flash(import_message(stats), 'error' if stats['failed'] and not stats['insert'] else 'success')
    return redirect(url_for('finder.docs', i=item_id))


@bp.route('/docs/import_all', methods=['POST'])
@login_required
def docs_import_all():
    """文書類の Drive フォルダを全部，下の階層まで展開する（★2026-10-02）．
    既定はまだ中身の無いフォルダだけを読む．時間の上限で止まっても，もう一度押せば
    残った空のフォルダから続きになる．「展開済みも読み直す」なら最上位のフォルダから全部たどり直す"""
    require_admin()
    refresh = request.form.get('refresh') == '1'
    with items_cursor() as (cursor, conn):
        cursor.execute(f"SELECT i.id, i.url, i.category, i.title, i.parent_id, "
                       f"(SELECT COUNT(*) FROM {ITEMS} c WHERE c.parent_id = i.id) AS n_children, "
                       f"p.url AS parent_url FROM {ITEMS} i LEFT JOIN {ITEMS} p ON p.id = i.parent_id "
                       "WHERE i.item_type = '文書類' AND i.url LIKE %s ORDER BY i.sort_order, i.id",
                       ('%drive.google.com/drive/%folders/%',))
        rows = [r for r in cursor.fetchall() if drive_folder_id(r['url'])]
    if refresh:   # 親が Drive フォルダでないもの（たどりの起点）だけ．中は深くたどって読み直す
        roots = [r for r in rows if not drive_folder_id(r['parent_url'])]
    else:         # 中身の無いフォルダだけ
        roots = [r for r in rows if not r['n_children']]
    if not roots:
        flash('展開していない Drive フォルダはありません' if not refresh else 'Drive フォルダがありません', 'success')
        return redirect(url_for('finder.docs'))
    try:
        stats = import_folders(roots, True, max_rows=50000)
    except (RuntimeError, requests.RequestException) as e:
        flash(str(e), 'error')
        return redirect(url_for('finder.docs'))
    logger.info('finder import_all: roots=%s refresh=%s %s by %s', len(roots), refresh, stats, current_user_id())
    write_log('文書類', '全部展開', None, '読み直し' if refresh else '未展開のみ', dict(stats, roots=len(roots)))
    flash(f'起点のフォルダ {len(roots)} 個から：' + import_message(stats), 'success')
    return redirect(url_for('finder.docs'))


@bp.route('/apps')
@login_required
def apps():
    with items_cursor() as (cursor, conn):
        cursor.execute(f"SELECT id, category, title, url, note FROM {ITEMS} WHERE item_type = 'アプリ' "
                       "ORDER BY sort_order IS NULL, sort_order, id")
        rows = cursor.fetchall()
    groups = []
    for r in rows:
        if not groups or groups[-1]['name'] != (r['category'] or ''):
            groups.append({'name': r['category'] or '', 'items': []})
        groups[-1]['items'].append(r)
    return render_template('finder/apps.html', groups=groups)


def import_items(rows, report):
    """export_type が finder_items の JSON．id があればその行，無ければ（種類，URL）が同じ行を上書き，
    無ければ追加．書かれていない列は残す．誤りが1件でもあれば何も書かない"""
    parsed = []
    for n, raw in enumerate(rows or [], 1):
        if not isinstance(raw, dict):
            report['errors'].append(f'{n}行目：オブジェクトではありません'); continue
        rid = raw.get('id')
        try:
            rid = int(rid) if rid not in (None, '') else None
        except (TypeError, ValueError):
            report['errors'].append(f'{n}行目：id {rid!r} は整数で書いてください'); continue
        vals = {k: raw[k] for k in ITEM_FIELDS if k in raw}
        report['unknown'] |= set(raw) - set(ITEM_FIELDS) - {'id'}
        if 'item_type' in vals and vals['item_type'] not in ITEM_TYPES:
            report['errors'].append(f'{n}行目：item_type は「文書類」か「アプリ」です'); continue
        if 'parent_id' in vals:
            try:
                vals['parent_id'] = int(vals['parent_id']) if vals['parent_id'] not in (None, '') else None
            except (TypeError, ValueError):
                report['errors'].append(f'{n}行目：parent_id は整数で書いてください'); continue
        if 'sort_order' in vals and vals['sort_order'] not in (None, ''):
            try:
                vals['sort_order'] = round(float(vals['sort_order']), 2)
            except (TypeError, ValueError):
                report['errors'].append(f'{n}行目：sort_order は数で書いてください'); continue
        if rid is None and not (vals.get('item_type') and vals.get('url')):
            report['errors'].append(f'{n}行目：id の無い行には item_type と url が要ります'); continue
        parsed.append((rid, vals))
    if report['errors'] or not parsed:
        return
    stamp = now_jst()
    with items_cursor() as (cursor, conn):
        cursor.execute(f"SELECT id, item_type, url FROM {ITEMS}")
        all_rows = cursor.fetchall()
        ids = {r['id'] for r in all_rows}
        by_key = {(r['item_type'], r['url']): r['id'] for r in all_rows}
        missing = [rid for rid, _ in parsed if rid is not None and rid not in ids]
        if missing:
            report['errors'].append('表に無い id があります：' + '，'.join(map(str, missing[:50])))
            return
        for rid, vals in parsed:
            if rid is None:
                rid = by_key.get((vals['item_type'], vals['url']))
            if rid is None:
                cols = list(vals) + ['created_at', 'updated_at']
                cursor.execute(f"INSERT INTO {ITEMS} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                               list(vals.values()) + [stamp, stamp])
                by_key[(vals['item_type'], vals['url'])] = cursor.lastrowid
                report['n_insert'] += 1
            elif vals:
                sets = ', '.join(f'{k} = %s' for k in vals) + ', updated_at = %s'
                cursor.execute(f"UPDATE {ITEMS} SET {sets} WHERE id = %s", list(vals.values()) + [stamp, rid])
                report['n_update'] += 1
        conn.commit()


@bp.route('/search')
@login_required
def search():
    q = request.args.get('q', '')
    mode = request.args.get('mode', 'and_or')
    if mode not in ('and_or', 'or_and'):
        mode = 'and_or'
    year, committee, where, params = filters_from(request.args)
    groups = parse_query(q)
    results, total = [], 0
    if groups:
        cond, cparams = build_search(groups, mode)
        clause = ' WHERE ' + ' AND '.join(where + [cond])
        allp = params + cparams
        with get_db_cursor(database=DB) as (cursor, conn):
            cursor.execute(f"SELECT COUNT(*) AS n FROM {TABLE}{clause}", allp)
            total = cursor.fetchone()['n']
            cursor.execute(f"SELECT id, fiscal_year, committee, held_on, note, body FROM {TABLE}{clause} "
                           "ORDER BY fiscal_year DESC, committee, held_on, id LIMIT %s",
                           allp + [SEARCH_LIMIT])
            rows = cursor.fetchall()
        terms = [t for g in groups for t in g]
        for r in rows:
            r['snippet'] = snippet(r.pop('body') or '', terms)
            results.append(r)
    _, years, committees = facets()
    return render_template('finder/search.html', q=q, mode=mode, groups=groups, results=results,
                           total=total, limit=SEARCH_LIMIT, years=years, committees=committees,
                           year=year, committee=committee)


# ── JSON 入出力（admin） ──

@bp.route('/io')
@login_required
def io():
    require_admin()
    _, years, committees = facets()
    return render_template('finder/io.html', years=years, committees=committees, report=None)


@bp.route('/export')
@login_required
def export():
    require_admin()
    year, committee, where, params = filters_from(request.args)
    with_body = request.args.get('body', '1') == '1'
    cols = 'id, fiscal_year, committee, held_on, note' + (', body' if with_body else '')
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute(f"SELECT {cols} FROM {TABLE}" + (' WHERE ' + ' AND '.join(where) if where else '') +
                       " ORDER BY fiscal_year, committee, held_on, id", params)
        rows = cursor.fetchall()
    for r in rows:
        r['held_on'] = r['held_on'].isoformat() if r['held_on'] else None
    doc = {'export_type': 'finder_minutes', 'format_version': 1,
           'exported_at': now_jst().strftime('%Y-%m-%d %H:%M:%S'),
           'filter': {'fiscal_year': year or None, 'committee': committee or None, 'with_body': with_body},
           'rows': rows}
    name = 'finder_' + (year or 'all') + ('_' + committee if committee else '') + \
           now_jst().strftime('_%Y%m%d_%H%M%S') + '.json'
    return Response(json.dumps(doc, ensure_ascii=False, indent=1), mimetype='application/json',
                    headers={'Content-Disposition':
                             "attachment; filename*=UTF-8''" + _quote(name)})


def _quote(s):
    from urllib.parse import quote
    return quote(s)


def normalize_row(raw, n):
    """取り込む1行を検査して (id, {列: 値}) にする．書かれていない列は触らない"""
    if not isinstance(raw, dict):
        raise ValueError(f'{n}行目：オブジェクトではありません')
    rid = raw.get('id')
    if rid in ('', None):
        rid = None
    else:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            raise ValueError(f'{n}行目：id {rid!r} は整数で書いてください')
    vals = {}
    for k in FIELDS:
        if k not in raw:
            continue
        v = raw[k]
        try:
            if k == 'fiscal_year':
                v = parse_year(v)
            elif k == 'held_on':
                v = parse_date(v)
            elif v is not None:
                v = str(v)
        except ValueError as e:
            raise ValueError(f'{n}行目：{e}')
        if k == 'committee' and v is not None and len(v) > 200:
            raise ValueError(f'{n}行目：委員会名が長すぎます（200文字まで）')
        vals[k] = v
    unknown = set(raw) - set(FIELDS) - {'id'}
    if rid is None and not vals:
        raise ValueError(f'{n}行目：中身がありません')
    return rid, vals, unknown


def match_key(committee, fiscal_year, held_on, note):
    """id の無い行を表の行に対応づける鍵．
    備考が URL なら（委員会名，URL）を，そうでなければ（委員会名，年度，開催日，備考）を使う"""
    note = (note or '').strip()
    if note.startswith(('http://', 'https://')):
        return ('url', committee or '', note)
    d = held_on.isoformat() if isinstance(held_on, date) else (held_on or '')
    return ('row', committee or '', fiscal_year, d, note)


@bp.route('/import', methods=['POST'])
@login_required
def import_json():
    """JSON の読み込み（★2026-10-01 確認段階を廃止し，冪等に）

    id のある行：その行を上書き（書かれている列だけ）．
    id の無い行：鍵（match_key）が同じ行が表にあれば上書き，なければ追加．
    同じ鍵の行がファイルに複数あるときは，表の同じ鍵の行と古い順に1対1で対応させる．
    書かれていない列（たとえば本文）は表の値を残す．行を消すことはしない．
    誤りが1件でもあれば何も書かない．何度読み込んでも結果は同じになる．"""
    require_admin()
    f = request.files.get('file')
    report = {'n_insert': 0, 'n_update': 0, 'missing': [], 'errors': [], 'unknown': set()}
    try:
        doc = json.loads((f.read() if f else b'').decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as e:
        report['errors'].append(f'JSON として読めません：{e}')
        doc = None
    rows = doc.get('rows') if isinstance(doc, dict) else doc if isinstance(doc, list) else None
    if doc is not None and rows is None:
        report['errors'].append('rows（行の配列）が見つかりません')
    if isinstance(doc, dict) and doc.get('export_type') == 'finder_items':
        import_items(rows, report)
        if not report['errors']:
            write_log('文書類・アプリ', 'JSON読込', None, f.filename if f else None,
                      {'insert': report['n_insert'], 'update': report['n_update']})
        report['unknown'] = sorted(report['unknown'])
        report['target'] = '文書類・アプリ'
        _, years, committees = facets()
        return render_template('finder/io.html', years=years, committees=committees, report=report)
    parsed = []
    for n, raw in enumerate(rows or [], 1):
        try:
            rid, vals, unknown = normalize_row(raw, n)
            report['unknown'] |= unknown
            if rid is None and not vals.get('committee'):
                raise ValueError(f'{n}行目：id の無い行には委員会名（committee）が要ります')
            parsed.append((rid, vals))
        except ValueError as e:
            report['errors'].append(str(e))
    if not report['errors'] and parsed:
        stamp = now_jst()
        with get_db_cursor(database=DB) as (cursor, conn):
            ids = [rid for rid, _ in parsed if rid is not None]
            existing = set()
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                cursor.execute(f"SELECT id FROM {TABLE} WHERE id IN ({','.join(['%s'] * len(chunk))})", chunk)
                existing |= {r['id'] for r in cursor.fetchall()}
            missing = [rid for rid in ids if rid not in existing]
            if missing:
                report['missing'] = missing
                report['errors'].append('表に無い id があります：' + '，'.join(map(str, missing[:50])))
            else:
                # id の無い行の対応先を，同じ委員会の既存行から探す
                comms = sorted({v['committee'] for rid, v in parsed if rid is None})
                pool = {}
                for i in range(0, len(comms), 100):
                    chunk = comms[i:i + 100]
                    cursor.execute(
                        f"SELECT id, committee, fiscal_year, held_on, note FROM {TABLE} "
                        f"WHERE committee IN ({','.join(['%s'] * len(chunk))}) ORDER BY id", chunk)
                    for r in cursor.fetchall():
                        k = match_key(r['committee'], r['fiscal_year'], r['held_on'], r['note'])
                        pool.setdefault(k, []).append(r['id'])
                for rid, vals in parsed:
                    if rid is None:
                        k = match_key(vals.get('committee'), vals.get('fiscal_year'),
                                      vals.get('held_on'), vals.get('note'))
                        hits = pool.get(k)
                        if hits:
                            rid = hits.pop(0)
                    if rid is None:
                        cols = list(vals) + ['created_at', 'updated_at']
                        cursor.execute(f"INSERT INTO {TABLE} ({', '.join(cols)}) "
                                       f"VALUES ({', '.join(['%s'] * len(cols))})",
                                       list(vals.values()) + [stamp, stamp])
                        report['n_insert'] += 1
                    elif vals:
                        sets = ', '.join(f'{k} = %s' for k in vals) + ', updated_at = %s'
                        cursor.execute(f"UPDATE {TABLE} SET {sets} WHERE id = %s",
                                       list(vals.values()) + [stamp, rid])
                        report['n_update'] += 1
                conn.commit()
                logger.info('finder import: insert=%s update=%s by %s', report['n_insert'],
                            report['n_update'], current_user_id())
                write_log('議事録', 'JSON読込', None, f.filename if f else None,
                          {'insert': report['n_insert'], 'update': report['n_update']})
    report['unknown'] = sorted(report['unknown'])
    _, years, committees = facets()
    return render_template('finder/io.html', years=years, committees=committees, report=report)


# ── 更新（★2026-10-02 データ構築を廃止して置き換え） ──
# 議事録・文書類・アプリを1件ずつ登録する入口．ログイン者なら誰でも登録でき，登録はすべてログに残す．
# 議事録と文書類のファイルは Google Drive の保管フォルダ（archive）へ上げ，その URL を行に記録する．
# 保管フォルダ：設定 archive_folder_id（admin が更新画面で指定）．無ければ config の FINDER_ARCHIVE_FOLDER_ID，
# それも無ければ通行証のアカウントのマイドライブに「finder_archive」を作って使う．
# その下に「議事録/<委員会名>」「文書類/<分類>」のフォルダを必要に応じて作る．
# 通行証には書き込みの権限（drive.file 以上）が要る．読み取り専用（drive.readonly）だけでは上げられない．

LOG = 'finder_log'
LOG_DDL = (f"CREATE TABLE IF NOT EXISTS `{LOG}` ("
           "`id` int NOT NULL AUTO_INCREMENT, `at` datetime NOT NULL, "
           "`user_id` int DEFAULT NULL, `user_name` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
           "`kind` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL, "
           "`action` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL, "
           "`target_id` int DEFAULT NULL, `title` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
           "`detail` text COLLATE utf8mb4_unicode_ci, "
           "PRIMARY KEY (`id`), KEY `idx_at` (`at`), KEY `idx_kind` (`kind`, `at`)"
           ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
_log_ready = {'ok': False}
UPLOAD_MAX = 50 * 1024 * 1024
DOC_EXT = {'.pdf': 'application/pdf',
           '.doc': 'application/msword',
           '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
           '.xls': 'application/vnd.ms-excel',
           '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
           '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
           '.webp': 'image/webp'}
IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.webp')


def user_name():
    """ログに残す登録者名．セッションに名前があればそれ，無ければ users 表から引く"""
    for k in ('display_name', 'name', 'full_name', 'username', 'user_name', 'email'):
        v = session.get(k)
        if v:
            return str(v)[:200]
    uid = current_user_id()
    for dbname in (None, DB):
        try:
            ctx = get_db_cursor(database=dbname) if dbname else get_db_cursor()
            with ctx as (cursor, conn):
                cursor.execute("SELECT full_name, email FROM users WHERE id = %s", (uid,))
                r = cursor.fetchone()
            if r:
                return str(r.get('full_name') or r.get('email') or f'user{uid}')[:200]
        except Exception:
            continue
    return f'user{uid}'


def write_log(kind, action, target_id=None, title=None, detail=None):
    """登録・編集・削除の記録．ログが書けなくても本来の処理は止めない"""
    try:
        with get_db_cursor(database=DB) as (cursor, conn):
            if not _log_ready['ok']:
                cursor.execute(LOG_DDL)
                _log_ready['ok'] = True
            if isinstance(detail, (dict, list)):
                detail = json.dumps(detail, ensure_ascii=False, default=str)
            cursor.execute(f"INSERT INTO {LOG} (at, user_id, user_name, kind, action, target_id, title, detail) "
                           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                           (now_jst(), current_user_id() or None, user_name(), kind, action, target_id,
                            (title or '')[:500] or None, detail))
            conn.commit()
    except Exception as e:  # ログの失敗で登録を失敗させない
        logger.warning('finder log failed: %s', e)


def fiscal_year_of(d):
    return d.year if d.month >= 4 else d.year - 1


# ── Drive への書き込み ──

def drive_q(s):
    return s.replace('\\', '\\\\').replace("'", "\\'")


def drive_folder(name, parent, h):
    """parent の下の name フォルダの id（無ければ作る）"""
    r = requests.get('https://www.googleapis.com/drive/v3/files', headers=h, timeout=30, params={
        'q': f"name = '{drive_q(name)}' and mimeType = '{FOLDER_MIME}' and '{parent}' in parents and trashed = false",
        'fields': 'files(id)', 'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'})
    if r.status_code == 200 and r.json().get('files'):
        return r.json()['files'][0]['id']
    r = requests.post('https://www.googleapis.com/drive/v3/files', headers=h, timeout=30,
                      params={'supportsAllDrives': 'true', 'fields': 'id'},
                      json={'name': name, 'mimeType': FOLDER_MIME, 'parents': [parent]})
    if r.status_code not in (200, 201):
        raise RuntimeError(drive_write_error(r, f'フォルダ「{name}」を作れません'))
    return r.json()['id']


def drive_write_error(r, what):
    if r.status_code == 403 and 'insufficient' in r.text.lower():
        return what + '（Drive 403）．サイトの通行証に書き込みの権限がありません．drive.file 以上のスコープで同意し直してください'
    return f'{what}（Drive {r.status_code}）'


def archive_root(h):
    fid = get_setting('archive_folder_id') or getattr(Config, 'FINDER_ARCHIVE_FOLDER_ID', None)
    if fid:
        return fid
    fid = drive_folder('finder_archive', 'root', h)
    put_setting('archive_folder_id', fid)
    return fid


def archive_path(parts, h):
    """保管フォルダの下の parts（例 ['議事録', '執行会議']）のフォルダ id．作ったものは設定に覚えておく"""
    known = get_setting('archive_folders', {}) or {}
    root = archive_root(h)
    key, parent = root, root
    for p in parts:
        key = key + '/' + p
        if key in known:
            parent = known[key]
            continue
        parent = drive_folder(p, parent, h)
        known[key] = parent
    put_setting('archive_folders', known)
    return parent


def drive_upload(data, name, mime, parent, h):
    """resumable 方式でファイルを上げ，{'id','name','mimeType'} を返す"""
    init = requests.post('https://www.googleapis.com/upload/drive/v3/files', timeout=60,
                         params={'uploadType': 'resumable', 'supportsAllDrives': 'true',
                                 'fields': 'id,name,mimeType'},
                         headers=dict(h, **{'Content-Type': 'application/json; charset=UTF-8',
                                            'X-Upload-Content-Type': mime,
                                            'X-Upload-Content-Length': str(len(data))}),
                         json={'name': name, 'parents': [parent]})
    if init.status_code != 200 or 'Location' not in init.headers:
        raise RuntimeError(drive_write_error(init, f'「{name}」を上げられません'))
    r = requests.put(init.headers['Location'], data=data, headers={'Content-Type': mime}, timeout=300)
    if r.status_code not in (200, 201):
        raise RuntimeError(drive_write_error(r, f'「{name}」を上げられません'))
    return r.json()


def clean_name(filename):
    name = re.split(r'[\\/]', filename or '')[-1].strip()
    return name[:200] or 'file'


def read_upload(fs, allowed):
    """(データ, 名前, MIME, 拡張子)．形式・大きさが合わなければ ValueError"""
    name = clean_name(fs.filename)
    ext = ('.' + name.rsplit('.', 1)[-1].lower()) if '.' in name else ''
    if ext not in allowed:
        raise ValueError(f'「{name}」は受け付けない形式です（' + '，'.join(sorted(allowed)) + '）')
    data = fs.read(UPLOAD_MAX + 1)
    if len(data) > UPLOAD_MAX:
        raise ValueError(f'「{name}」が大きすぎます（{UPLOAD_MAX // 1024 // 1024}MB まで）')
    if not data:
        raise ValueError(f'「{name}」が空です')
    return data, name, allowed[ext], ext


def pdf_text(data):
    """PDF のテキスト層を取り出す（pypdf / PyPDF2 があれば）．取れなければ空"""
    import io
    try:
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = '\n'.join((p.extract_text() or '') for p in reader.pages)
        return text.strip()
    except Exception as e:
        logger.info('finder pdf_text: %s', e)
        return ''


def doc_dirs():
    """文書類の置き場の選択肢：[(値, 表示)]．値は 'c:分類' か 'i:フォルダの行 id'"""
    tree, _ = doc_tree()
    out = []

    def walk(nodes, path):
        for n in nodes:
            if n['children'] or url_kind(n['url']) == 'folder':
                p = path + ' / ' + (n['title'] or '')
                out.append((f"i:{n['id']}", p))
                walk(n['children'], p)
    for x in tree:
        out.append((f"c:{x['name']}", x['name'] or '（分類なし）'))
        walk(x['nodes'], x['name'] or '（分類なし）')
    return out


def app_categories():
    with items_cursor() as (cursor, conn):
        cursor.execute(f"SELECT DISTINCT category FROM {ITEMS} WHERE item_type = 'アプリ' AND category IS NOT NULL "
                       "AND category <> '' ORDER BY category")
        return [r['category'] for r in cursor.fetchall()]


@bp.route('/update', methods=['GET'])
@login_required
def update():
    require_update()
    t = request.args.get('t', 'minutes')
    if t not in ('minutes', 'docs', 'apps', 'log') or (t == 'log' and not is_admin()):
        t = 'minutes'
    ctx = {'t': t, 'committees': [x['name'] for x in committee_list(include_hidden=True) if x['name']]}
    if t == 'docs':
        ctx['dirs'] = doc_dirs()
        ctx['sel'] = request.args.get('d', '')
    elif t == 'apps':
        ctx['app_cats'] = app_categories()
    elif t == 'log':
        kind = request.args.get('k', '')
        page = max(request.args.get('p', 1, type=int), 1)
        with get_db_cursor(database=DB) as (cursor, conn):
            cursor.execute(LOG_DDL)
            where, params = '', []
            if kind:
                where, params = ' WHERE kind = %s', [kind]
            cursor.execute(f"SELECT COUNT(*) AS n FROM {LOG}{where}", params)
            total = cursor.fetchone()['n']
            cursor.execute(f"SELECT * FROM {LOG}{where} ORDER BY at DESC, id DESC LIMIT 100 OFFSET %s",
                           params + [(page - 1) * 100])
            ctx.update(logs=cursor.fetchall(), total=total, page=page, kind=kind)
        ctx['archive_id'] = get_setting('archive_folder_id') or getattr(Config, 'FINDER_ARCHIVE_FOLDER_ID', None)
    return render_template('finder/update.html', **ctx)


@bp.route('/update/minutes', methods=['POST'])
@login_required
def update_minutes():
    """議事録の PDF を保管フォルダへ上げ，委員会・開催日とともに1回分として登録する"""
    require_update()
    f = request.form
    back = url_for('finder.update', t='minutes')
    committee = (f.get('committee') or '').strip()[:200]
    try:
        held_on = parse_date((f.get('held_on') or '').strip())
        fy = parse_year((f.get('fiscal_year') or '').strip())
        if not committee:
            raise ValueError('委員会を選ぶか書いてください')
        if not held_on:
            raise ValueError('開催日を書いてください')
        fs = request.files.get('file')
        if not fs or not fs.filename:
            raise ValueError('議事録の PDF を選んでください')
        data, name, mime, ext = read_upload(fs, {'.pdf': 'application/pdf'})
    except ValueError as e:
        flash(str(e), 'error'); return redirect(back)
    fy = fy if fy is not None else fiscal_year_of(held_on)
    try:
        h = {'Authorization': f'Bearer {drive_access_token()}'}
        up = drive_upload(data, name, mime, archive_path(['議事録', committee], h), h)
    except (RuntimeError, requests.RequestException) as e:
        logger.warning('finder upload minutes: %s', e)
        flash(str(e), 'error'); return redirect(back)
    url = drive_url(up)
    body = pdf_text(data) if f.get('extract', '1') == '1' else ''
    stamp = now_jst()
    with get_db_cursor(database=DB) as (cursor, conn):
        cursor.execute(f"SELECT id FROM {TABLE} WHERE committee = %s AND held_on = %s", (committee, held_on))
        same = [r['id'] for r in cursor.fetchall()]
        cursor.execute(f"INSERT INTO {TABLE} (fiscal_year, committee, held_on, body, note, created_at, updated_at) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (fy, committee, held_on, body or None, url, stamp, stamp))
        rid = cursor.lastrowid
        conn.commit()
    write_log('議事録', '登録', rid, f'{committee} {held_on}',
              {'file': name, 'url': url, 'size': len(data), 'body_chars': len(body), 'fiscal_year': fy})
    msg = f'{committee}　{held_on} の議事録を登録しました（本文 {len(body)} 文字を PDF から起こしました）' if body else \
        f'{committee}　{held_on} の議事録を登録しました（本文は PDF から取れませんでした．後で admin が補えます）'
    if same:
        msg += f'．同じ委員会・開催日の回が既にあります（ID {", ".join(map(str, same))}）'
    flash(msg, 'success')
    return redirect(url_for('finder.index', c=committee, m=rid))


@bp.route('/update/docs', methods=['POST'])
@login_required
def update_docs():
    """置き場（分類かフォルダ）を選び，文書ファイルを上げて登録する（複数可）"""
    require_update()
    f = request.form
    d = (f.get('dir') or '').strip()
    new_cat = (f.get('new_category') or '').strip()[:200]
    back = url_for('finder.update', t='docs', d=d)
    if new_cat:
        category, parent = new_cat, None
    elif d.startswith('c:'):
        category, parent = d[2:], None
    elif d.startswith('i:') and d[2:].isdigit():
        parent = item_row(int(d[2:]))
        if not parent or parent['item_type'] != '文書類':
            flash('置き場のフォルダが見つかりません', 'error'); return redirect(back)
        category = parent['category'] or ''
    else:
        flash('置き場を選ぶか，新しい分類の名前を書いてください', 'error'); return redirect(back)
    files = [x for x in request.files.getlist('files') if x and x.filename]
    if not files:
        flash('文書ファイルを選んでください', 'error'); return redirect(back)
    try:
        loaded = [read_upload(x, DOC_EXT) for x in files]
    except ValueError as e:
        flash(str(e), 'error'); return redirect(back)
    title = (f.get('title') or '').strip()[:500]
    try:
        h = {'Authorization': f'Bearer {drive_access_token()}'}
        folder = archive_path(['文書類', category or '分類なし'], h)
        ups = [(drive_upload(data, name, mime, folder, h), name, len(data)) for data, name, mime, ext in loaded]
    except (RuntimeError, requests.RequestException) as e:
        logger.warning('finder upload docs: %s', e)
        flash(str(e), 'error'); return redirect(back)
    stamp = now_jst()
    ids = []
    with items_cursor() as (cursor, conn):
        if parent:
            cursor.execute(f"SELECT MAX(sort_order) AS o FROM {ITEMS} WHERE parent_id = %s", (parent['id'],))
        else:
            cursor.execute(f"SELECT MAX(sort_order) AS o FROM {ITEMS} WHERE item_type = '文書類' AND category = %s "
                           "AND parent_id IS NULL", (category,))
        o = cursor.fetchone()['o']
        o = float(o) if o is not None else 0.0
        for k, (up, name, size) in enumerate(ups, 1):
            t = title if (title and len(ups) == 1) else name
            cursor.execute(f"INSERT INTO {ITEMS} (item_type, category, title, url, note, sort_order, parent_id, "
                           "created_at, updated_at) VALUES ('文書類',%s,%s,%s,NULL,%s,%s,%s,%s)",
                           (category, t, drive_url(up), o + k, parent['id'] if parent else None, stamp, stamp))
            ids.append(cursor.lastrowid)
        conn.commit()
    for i, (up, name, size) in zip(ids, ups):
        write_log('文書類', '登録', i, name, {'url': drive_url(up), 'size': size, 'category': category,
                                              'parent_id': parent['id'] if parent else None})
    flash(f'{len(ids)} 件の文書を登録しました', 'success')
    return redirect(url_for('finder.docs', i=ids[0]))


@bp.route('/update/apps', methods=['POST'])
@login_required
def update_apps():
    """Web アプリをアプリ名と URL で登録する"""
    require_update()
    f = request.form
    back = url_for('finder.update', t='apps')
    title = (f.get('title') or '').strip()[:500]
    url = (f.get('url') or '').strip()
    category = ((f.get('new_category') or '').strip() or (f.get('category') or '').strip())[:200]
    note = (f.get('note') or '').strip() or None
    if not title:
        flash('アプリ名を書いてください', 'error'); return redirect(back)
    if not re.match(r'https?://\S+$', url):
        flash('URL は http:// か https:// で始まる形で書いてください', 'error'); return redirect(back)
    stamp = now_jst()
    with items_cursor() as (cursor, conn):
        cursor.execute(f"SELECT id FROM {ITEMS} WHERE item_type = 'アプリ' AND url = %s", (url,))
        if cursor.fetchone():
            flash('この URL のアプリは既に登録されています', 'error'); return redirect(back)
        cursor.execute(f"SELECT MAX(sort_order) AS o FROM {ITEMS} WHERE item_type = 'アプリ'")
        o = cursor.fetchone()['o']
        cursor.execute(f"INSERT INTO {ITEMS} (item_type, category, title, url, note, sort_order, created_at, updated_at) "
                       "VALUES ('アプリ',%s,%s,%s,%s,%s,%s,%s)",
                       (category or None, title, url, note, (float(o) if o is not None else 0) + 1, stamp, stamp))
        rid = cursor.lastrowid
        conn.commit()
    write_log('アプリ', '登録', rid, title, {'url': url, 'category': category})
    flash(f'アプリ「{title}」を登録しました', 'success')
    return redirect(url_for('finder.apps'))


@bp.route('/update/archive', methods=['POST'])
@login_required
def update_archive():
    """保管フォルダの指定（admin）．Drive フォルダの URL か id"""
    require_admin()
    raw = (request.form.get('archive') or '').strip()
    fid = drive_folder_id(raw) or (raw if re.fullmatch(r'[A-Za-z0-9_-]{10,}', raw) else None)
    if raw and not fid:
        flash('Drive フォルダの URL か id を書いてください', 'error')
    else:
        put_setting('archive_folder_id', fid)
        put_setting('archive_folders', {})
        write_log('設定', '保管フォルダ', None, fid or '（既定に戻す）')
        flash('保管フォルダを設定しました' if fid else '保管フォルダの指定を外しました', 'success')
    return redirect(url_for('finder.update', t='log'))


@bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    return redirect_to_dashboard()
