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

"""オール：表・権限・利用者・記録（Web 画面と MCP の両方が使う共通部分）

権限は3段で，閲覧（view）＜リクエスト（request）＜アクト（act）．上の段は下の段を含む．
段ごとに公開範囲を，文書アーカイブと同じ5区分（一般公開・構成員だけ・非公開・指定グループのみ・構成員＋グループ）で決める．
一般公開は未ログインを含む誰でも（閲覧だけに使える）．構成員は利用者区分 regular，グループはまいぐるのグループ（名前で持つ）．
保存は all_grants の行で表す：一般公開＝public 1行，構成員＝domestic 1行，グループ＝group をグループの数だけ，非公開＝行なし．
ポータルの持ち主は当面 admin に限る（admin はすべてのポータルでアクト）．owner_user_id 列は残すが判定には使わない．
Web 画面と MCP は同じ level_of() で判定するので，人間と AI は同じ規則で同じ中身を見る．
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from flask import g, session

from db import get_db_cursor

logger = logging.getLogger(__name__)

DB = 'default'   # オールの表はすべて default の DB に置く（2026-10-09）
JST = timezone(timedelta(hours=9), 'JST')

KINDS = ('機関', 'グループ', '個人', 'システム')
LEVELS = {'none': 0, 'view': 1, 'request': 2, 'act': 3}
LEVEL_LABEL = {0: 'なし', 1: '閲覧', 2: 'リクエスト', 3: 'アクト'}
LEVEL_KEYS = ('view', 'request', 'act')
LEVEL_KEY_LABEL = {'view': '閲覧', 'request': 'リクエスト', 'act': 'アクト'}
# 公開範囲（文書アーカイブの ACCESS_POLICIES と同じ名前と表示名）
POLICIES = ('private', 'group', 'domestic', 'domestic_group', 'public')
# URL（文書）ごとの公開範囲：inherit はポータルの閲覧の範囲に従う
ITEM_POLICIES = ('inherit',) + ('private', 'group', 'domestic', 'domestic_group', 'public')
POLICY_LABEL = {'inherit': 'ポータルと同じ', 'public': '一般公開', 'domestic': '構成員だけ', 'private': '非公開',
                'group': '指定グループのみ', 'domestic_group': '構成員＋グループ'}
DOMESTIC_CATEGORIES = {'regular'}   # 構成員とみなす利用者区分（文書アーカイブの domestic と同じ）

DDL = [
    "CREATE TABLE IF NOT EXISTS `all_portals` ("
    "`id` int NOT NULL AUTO_INCREMENT, "
    "`kind` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`name` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`description` text COLLATE utf8mb4_unicode_ci, "
    "`owner_user_id` int DEFAULT NULL, "
    "`ai_act` tinyint NOT NULL DEFAULT 0, "
    "`parent_id` int DEFAULT NULL, "
    "`parent_item_id` int DEFAULT NULL, "
    "`sort_order` decimal(10,2) DEFAULT NULL, "
    "`created_at` datetime DEFAULT NULL, `updated_at` datetime DEFAULT NULL, "
    "PRIMARY KEY (`id`), KEY `idx_kind` (`kind`,`sort_order`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
    "CREATE TABLE IF NOT EXISTS `all_grants` ("
    "`id` int NOT NULL AUTO_INCREMENT, `portal_id` int NOT NULL, "
    "`subject_kind` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`subject` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`level` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`created_at` datetime DEFAULT NULL, "
    "PRIMARY KEY (`id`), KEY `idx_portal` (`portal_id`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
    "CREATE TABLE IF NOT EXISTS `all_items` ("
    "`id` int NOT NULL AUTO_INCREMENT, `portal_id` int NOT NULL, "
    "`bundle` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`title` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`url` text COLLATE utf8mb4_unicode_ci, "
    "`body` longtext COLLATE utf8mb4_unicode_ci, "
    "`note` text COLLATE utf8mb4_unicode_ci, "
    "`source` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'text', "
    "`access_policy` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'inherit', "
    "`access_groups` text COLLATE utf8mb4_unicode_ci, "
    "`fetched_at` datetime DEFAULT NULL, "
    "`subtree` tinyint NOT NULL DEFAULT 0, "
    "`sort_order` decimal(10,2) DEFAULT NULL, "
    "`created_by` int DEFAULT NULL, "
    "`created_at` datetime DEFAULT NULL, `updated_at` datetime DEFAULT NULL, "
    "PRIMARY KEY (`id`), KEY `idx_portal` (`portal_id`,`bundle`,`sort_order`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
    "CREATE TABLE IF NOT EXISTS `all_requests` ("
    "`id` int NOT NULL AUTO_INCREMENT, `portal_id` int NOT NULL, "
    "`user_id` int DEFAULT NULL, `user_name` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`via` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`body` text COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`status` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'open', "
    "`response` text COLLATE utf8mb4_unicode_ci, "
    "`handled_by` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`created_at` datetime NOT NULL, `handled_at` datetime DEFAULT NULL, "
    "PRIMARY KEY (`id`), KEY `idx_portal` (`portal_id`,`status`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
    "CREATE TABLE IF NOT EXISTS `all_log` ("
    "`id` int NOT NULL AUTO_INCREMENT, `at` datetime NOT NULL, "
    "`user_id` int DEFAULT NULL, `user_name` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`via` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`action` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL, "
    "`portal_id` int DEFAULT NULL, `target_id` int DEFAULT NULL, "
    "`detail` text COLLATE utf8mb4_unicode_ci, "
    "PRIMARY KEY (`id`), KEY `idx_at` (`at`), KEY `idx_via` (`via`,`at`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
]
_ready = {'ok': False}


def now_jst():
    return datetime.now(JST).replace(tzinfo=None)


def cursor():
    """表が無ければ作ってからカーソルを渡す"""
    if not _ready['ok']:
        with get_db_cursor(database=DB) as (cur, conn):
            for s in DDL:
                cur.execute(s)
            # URL 文書（2026-10-09）の列が無い既存表には足す
            cur.execute("SHOW COLUMNS FROM all_items LIKE 'source'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE all_items ADD COLUMN `source` varchar(10) COLLATE utf8mb4_unicode_ci "
                            "NOT NULL DEFAULT 'text' AFTER `note`, ADD COLUMN `fetched_at` datetime DEFAULT NULL "
                            "AFTER `source`")
            # 子ポータル（2026-10-09）
            cur.execute("SHOW COLUMNS FROM all_portals LIKE 'parent_id'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE all_portals ADD COLUMN `parent_id` int DEFAULT NULL AFTER `ai_act`, "
                            "ADD COLUMN `parent_item_id` int DEFAULT NULL AFTER `parent_id`")
            # URL ごとの公開範囲（2026-10-09）
            cur.execute("SHOW COLUMNS FROM all_items LIKE 'access_policy'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE all_items ADD COLUMN `access_policy` varchar(20) COLLATE utf8mb4_unicode_ci "
                            "NOT NULL DEFAULT 'inherit' AFTER `source`, ADD COLUMN `access_groups` text "
                            "COLLATE utf8mb4_unicode_ci AFTER `access_policy`")
            # 配下も読める（2026-10-10）
            cur.execute("SHOW COLUMNS FROM all_items LIKE 'subtree'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE all_items ADD COLUMN `subtree` tinyint NOT NULL DEFAULT 0 AFTER `fetched_at`")
            conn.commit()
        _ready['ok'] = True
    return get_db_cursor(database=DB)


# ── 利用者 ──

def _users_row(uid):
    for dbname in ('default',):
        try:
            with get_db_cursor(database=dbname) as (cur, conn):
                cur.execute("SELECT * FROM users WHERE id = %s", (uid,))
                return cur.fetchone()
        except Exception:
            continue
    return None


def user_active(uid):
    r = _users_row(uid)
    if not r:
        return False
    if 'is_active' in r and r['is_active'] is not None and not r['is_active']:
        return False
    return not r.get('deleted_at')


def user_info(uid):
    r = _users_row(uid) or {}
    return {'id': uid, 'name': r.get('full_name') or r.get('email') or f'user{uid}',
            'email': r.get('email') or '', 'category': r.get('category') or ''}


def group_names(uid):
    """利用者の所属グループ名．まいぐるの公開 API（名前，無ければ id）を使い，無ければ表を直接読む．
    台帳のルールから作られたグループは user_group_memberships に行を持たないので，API を優先する"""
    try:
        from fujinp.user_groups import utils as ug
        fn = getattr(ug, 'get_user_group_names', None)
        if fn:
            return set(fn(uid) or [])
        fn = getattr(ug, 'get_user_group_ids', None)
        if fn:
            ids = [int(x) for x in (fn(uid) or [])]
            if not ids:
                return set()
            with get_db_cursor(database='default') as (cur, conn):
                cur.execute("SELECT name FROM user_groups WHERE id IN (" + ','.join(['%s'] * len(ids)) + ")", ids)
                return {r['name'] for r in cur.fetchall()}
    except Exception as e:
        logger.warning('all_portal group api: %s', e)
    try:
        with get_db_cursor(database='default') as (cur, conn):
            cur.execute("SELECT g.name FROM user_group_memberships m JOIN user_groups g ON g.id = m.group_id "
                        "WHERE m.user_id = %s AND (m.valid_from IS NULL OR m.valid_from <= %s) "
                        "AND (m.valid_until IS NULL OR m.valid_until >= %s)", (uid, now_jst(), now_jst()))
            return {r['name'] for r in cur.fetchall()}
    except Exception as e:
        logger.warning('all_portal group lookup: %s', e)
        return set()


def all_group_names():
    try:
        with get_db_cursor(database='default') as (cur, conn):
            cur.execute("SELECT name FROM user_groups ORDER BY name")
            return [r['name'] for r in cur.fetchall() if r['name']]
    except Exception:
        return []


class Who:
    """権限判定の主体．Web はセッションから，MCP はトークンの持ち主から作る"""

    def __init__(self, uid, category, via):
        self.uid = int(uid or 0)
        self.category = category or ''
        self.admin = self.category == 'admin'
        self.via = via
        self._groups = None
        self._name = None

    @property
    def groups(self):
        if self._groups is None:
            self._groups = group_names(self.uid) if self.uid else set()
        return self._groups

    @property
    def name(self):
        if self._name is None:
            for k in ('display_name', 'name', 'full_name', 'username', 'user_name', 'email'):
                if self.via == 'web' and session.get(k):
                    self._name = str(session.get(k))[:200]
                    break
            else:
                self._name = user_info(self.uid)['name'][:200]
        return self._name


def web_who():
    if not hasattr(g, 'all_who'):
        g.all_who = Who(session.get('user_id'), session.get('user_category'), 'web')
    return g.all_who


def mcp_who(uid):
    """MCP の主体．利用者区分は users の category で決める（セッションは無い）"""
    return Who(uid, user_info(uid)['category'], 'mcp')


# ── 権限 ──

def grants_of(portal_ids):
    if not portal_ids:
        return {}
    out = {}
    with cursor() as (cur, conn):
        cur.execute("SELECT * FROM all_grants WHERE portal_id IN (" + ','.join(['%s'] * len(portal_ids)) +
                    ") ORDER BY id", list(portal_ids))
        for r in cur.fetchall():
            out.setdefault(r['portal_id'], []).append(r)
    return out


def level_of(portal, who, grants=None, _depth=0):
    """そのポータルでの段（0 なし，1 閲覧，2 リクエスト，3 アクト）．
    子ポータルは，親でアクトならアクト，親の行（URL）が見えるなら親での段（少なくとも閲覧）を受け継ぎ，
    自分の権限の設定と大きいほうを取る"""
    if who.admin:
        return 3
    inherited = 0
    if portal.get('parent_id') and _depth < 20:
        parent = portal_row(portal['parent_id'])
        if parent:
            plv = level_of(parent, who, None, _depth + 1)
            if plv >= 3:
                inherited = 3
            else:
                it = item_row(portal['parent_item_id']) if portal.get('parent_item_id') else None
                if it and item_visible(it, who, plv):
                    inherited = max(plv, 1)
    if grants is None:
        grants = grants_of([portal['id']]).get(portal['id'], [])
    best = 0
    for gr in grants:
        sk = gr['subject_kind']
        hit = (sk == 'public'
               or (sk == 'everyone' and who.uid)          # 旧設定（ログインしている全員）
               or (sk == 'domestic' and who.category in DOMESTIC_CATEGORIES)
               or (sk == 'group' and who.uid and gr['subject'] in who.groups))
        if hit:
            best = max(best, LEVELS.get(gr['level'], 0))
    return max(best, inherited)


def portals_for(who, min_level=1):
    """主体が min_level 以上の段を持つポータル（段 'level' を足して）"""
    with cursor() as (cur, conn):
        cur.execute("SELECT p.*, (SELECT COUNT(*) FROM all_items i WHERE i.portal_id = p.id) AS n_items "
                    "FROM all_portals p WHERE p.parent_id IS NULL ORDER BY FIELD(p.kind, %s, %s, %s, %s), p.sort_order IS NULL, "
                    "p.sort_order, p.id", KINDS)
        rows = cur.fetchall()
    gs = grants_of([r['id'] for r in rows])
    seen = None
    out = []
    for r in rows:
        r['level'] = level_of(r, who, gs.get(r['id'], []))
        if r['level'] < 1 and min_level <= 1:
            # ポータルの閲覧が無くても，見える URL が1つでもあれば並べる
            if seen is None:
                seen = portals_with_visible_items(who)
            if r['id'] in seen:
                r['n_items'] = seen[r['id']]
                out.append(r)
                continue
        if r['level'] >= min_level:
            out.append(r)
    return out


def portal_row(pid):
    with cursor() as (cur, conn):
        cur.execute("SELECT * FROM all_portals WHERE id = %s", (pid,))
        return cur.fetchone()


def item_row(iid):
    with cursor() as (cur, conn):
        cur.execute("SELECT * FROM all_items WHERE id = %s", (iid,))
        return cur.fetchone()


def items_of(pid, with_body=False):
    cols = '*' if with_body else ('id, portal_id, bundle, title, url, note, source, fetched_at, sort_order, '
                                  'access_policy, access_groups, subtree, CHAR_LENGTH(body) AS chars')
    with cursor() as (cur, conn):
        cur.execute(f"SELECT {cols} FROM all_items WHERE portal_id = %s "
                    "ORDER BY sort_order IS NULL, sort_order, id", (pid,))
        rows = cur.fetchall()
    for r in rows:
        r['groups_list'] = item_groups(r)
    return rows


def item_groups(it):
    try:
        v = json.loads(it.get('access_groups') or '[]')
        return [str(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def item_visible(it, who, portal_level):
    """その URL（文書）が主体に見えるか．アクトの人は管理のためにすべて見る"""
    if who.admin or portal_level >= 3:
        return True
    pol = it.get('access_policy') or 'inherit'
    if pol == 'inherit':
        return portal_level >= 1
    if pol == 'public':
        return True
    dom = bool(who.uid) and who.category in DOMESTIC_CATEGORIES
    grp = bool(who.uid) and bool(set(item_groups(it)) & who.groups)
    return ((pol == 'domestic' and dom) or (pol == 'group' and grp)
            or (pol == 'domestic_group' and (dom or grp)))


# ── 配下も読める（2026-10-10） ──
# URL 文書の行に subtree=1 を付けると，その URL を入口として，同じサイトの配下の URL も
# その行と同じ公開範囲で読める（MCP の read_url）．入口の配下とは，入口のパスそのもの（問い合わせ部分は
# 問わない）と，入口のパスに / を付けた接頭辞で始まるパス．サイトの最上位（/）は入口にできない．

def subtree_base(url):
    """入口になる行の URL からパスの基点を返す（/app_share/source/ → /app_share/source）．
    外部の URL・最上位・オール自身は None"""
    from urllib.parse import urlsplit
    from flask import request
    u = (url or '').split('#', 1)[0].strip()
    parts = urlsplit(u)
    if parts.netloc:
        host = request.host if request else ''
        if not host or parts.netloc.lower() != host.lower():
            return None
    path = (parts.path or '').rstrip('/')
    if not path or not path.startswith('/') or path.startswith('/all_portal'):
        return None
    return path


def in_subtree(base, path):
    return path == base or path.startswith(base + '/')


def subtree_entries():
    """subtree=1 の行（全ポータル）"""
    with cursor() as (cur, conn):
        cur.execute("SELECT id, portal_id, bundle, title, url, note, source, access_policy, access_groups "
                    "FROM all_items WHERE subtree = 1 AND source = 'url'")
        rows = cur.fetchall()
    for r in rows:
        r['groups_list'] = item_groups(r)
    return rows


def visible_items(pid, who, portal_level, with_body=False):
    return [r for r in items_of(pid, with_body) if item_visible(r, who, portal_level)]


def children_of(pid):
    """{行の id: 子ポータルの行}"""
    with cursor() as (cur, conn):
        cur.execute("SELECT * FROM all_portals WHERE parent_id = %s", (pid,))
        return {r['parent_item_id']: r for r in cur.fetchall() if r['parent_item_id']}


def ancestors(portal):
    """親から順に [ポータル, …]（自分は含まない）"""
    out, p, n = [], portal, 0
    while p and p.get('parent_id') and n < 20:
        p = portal_row(p['parent_id'])
        if p:
            out.insert(0, p)
        n += 1
    return out


def descendant_ids(pid):
    """自分の下にある子ポータルの id（孫以下も）"""
    with cursor() as (cur, conn):
        cur.execute("SELECT id, parent_id FROM all_portals WHERE parent_id IS NOT NULL")
        rows = cur.fetchall()
    kids = {}
    for r in rows:
        kids.setdefault(r['parent_id'], []).append(r['id'])
    out, stack = [], list(kids.get(pid, []))
    while stack:
        x = stack.pop()
        if x in out:
            continue
        out.append(x)
        stack += kids.get(x, [])
    return out


def portals_with_visible_items(who):
    """ポータルの段を持たない主体にも見える URL があるポータル：{portal_id: 件数}"""
    with cursor() as (cur, conn):
        cur.execute("SELECT id, portal_id, access_policy, access_groups FROM all_items "
                    "WHERE access_policy NOT IN ('inherit', 'private')")
        rows = cur.fetchall()
    out = {}
    for r in rows:
        if item_visible(r, who, 0):
            out[r['portal_id']] = out.get(r['portal_id'], 0) + 1
    return out


def bundles(items):
    """[{'name': 束, 'items': [...]}]（束の名前の無いものは「（束なし）」に）"""
    out = []
    for it in items:
        name = it['bundle'] or ''
        if not out or out[-1]['name'] != name:
            out.append({'name': name, 'items': []})
        out[-1]['items'].append(it)
    return out


def policy_of(grants, level):
    """段ごとの行から公開範囲を読み取る：(policy, [グループ名], 旧設定の有無)"""
    rows = [g for g in grants if g['level'] == level]
    kinds = {g['subject_kind'] for g in rows}
    groups = [g['subject'] for g in rows if g['subject_kind'] == 'group' and g['subject']]
    legacy = bool(kinds & {'everyone', 'user'})
    if 'public' in kinds:
        policy = 'public'
    elif 'domestic' in kinds:
        policy = 'domestic_group' if groups else 'domestic'
    elif groups:
        policy = 'group'
    else:
        policy = 'private'
    return policy, groups, legacy


def policy_rows(policy, groups):
    """公開範囲を all_grants の行 [(subject_kind, subject)] に直す"""
    if policy == 'public':
        return [('public', None)]
    rows = []
    if policy in ('domestic', 'domestic_group'):
        rows.append(('domestic', None))
    if policy in ('group', 'domestic_group'):
        rows += [('group', g) for g in groups]
    return rows


# ── 登録・リクエスト（Web と MCP で同じ関数を使う） ──

def save_item(who, pid, vals, iid=None):
    """vals：bundle・title・url・body・note・sort_order．戻り値は id"""
    stamp = now_jst()
    is_new = not iid
    with cursor() as (cur, conn):
        if iid:
            sets = ', '.join(f'{k} = %s' for k in vals) + ', updated_at = %s'
            cur.execute(f"UPDATE all_items SET {sets} WHERE id = %s AND portal_id = %s",
                        list(vals.values()) + [stamp, iid, pid])
        else:
            if vals.get('sort_order') is None:
                cur.execute("SELECT MAX(sort_order) AS o FROM all_items WHERE portal_id = %s", (pid,))
                o = cur.fetchone()['o']
                vals['sort_order'] = float(o) + 1 if o is not None else 1.0
            cols = list(vals) + ['portal_id', 'created_by', 'created_at', 'updated_at']
            cur.execute(f"INSERT INTO all_items ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                        list(vals.values()) + [pid, who.uid, stamp, stamp])
            iid = cur.lastrowid
        conn.commit()
    write_log(who, '文書の登録' if is_new else '文書の編集', pid, iid, {'title': vals.get('title')})
    return iid


def store_copy(iid, text):
    """URL 文書の写しを置く（アクトの人が保存・読み直しをしたときだけ呼ぶ）"""
    with cursor() as (cur, conn):
        cur.execute("UPDATE all_items SET body = %s, fetched_at = %s WHERE id = %s", (text, now_jst(), iid))
        conn.commit()


def add_request(who, pid, body):
    with cursor() as (cur, conn):
        cur.execute("INSERT INTO all_requests (portal_id, user_id, user_name, via, body, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)", (pid, who.uid, who.name, who.via, body, now_jst()))
        rid = cur.lastrowid
        conn.commit()
    write_log(who, 'リクエスト', pid, rid, {'chars': len(body)})
    return rid


def requests_of(pid, status=None):
    with cursor() as (cur, conn):
        sql, params = "SELECT * FROM all_requests WHERE portal_id = %s", [pid]
        if status:
            sql += " AND status = %s"
            params.append(status)
        cur.execute(sql + " ORDER BY status = 'open' DESC, created_at DESC LIMIT 200", params)
        return cur.fetchall()


# ── 記録 ──

def write_log(who, action, portal_id=None, target_id=None, detail=None):
    """Web と MCP の操作の記録．失敗しても本来の処理は止めない"""
    try:
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        with cursor() as (cur, conn):
            cur.execute("INSERT INTO all_log (at, user_id, user_name, via, action, portal_id, target_id, detail) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (now_jst(), who.uid or None, who.name, who.via, action, portal_id, target_id, detail))
            conn.commit()
    except Exception as e:
        logger.warning('all_portal log failed: %s', e)
