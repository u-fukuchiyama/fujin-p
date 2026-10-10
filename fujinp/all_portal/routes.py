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

"""オール：人間向けの画面

トップはポータル（機関・グループ・個人・システム）の一覧表で，各ポータルは文書の束を持つ．
ポータル名を押すとポータルが開く．URL の追加はポータルの画面で（アクト以上）行う．
「依頼」の列には，アクトを持つポータル（子ポータルを含む）に届いた未処理のリクエストと，
自分が出したリクエストへの返答（30日以内）を出す．
ポータルの作成と権限の設定は admin だけ．ポータルの持ち主は当面 admin に限る．
ポータルの作成・URL の追加・設定と権限・削除と，Claude との接続（MCP）の案内・管理は，
admin 専用の設定ダッシュボード（/admin）に収める．Claude からの接続（OAuth の許可と MCP）も admin だけ．
子ポータルの編集（URL の追加・並び・写しの更新・リクエストの受付）は，親の行の URL ごとのダッシュボード（/i/<行>）で行う．
親でアクトの人が子ポータルの画面を開くと，ゲストと同じ読むだけの画面になる（2026-10-09）．
行に「配下も読める」（subtree）を付けると，その URL の配下の同じサイトのページを Claude が MCP の read_url で読める（2026-10-10，admin だけが設定）．
"""

import json
import logging
import re
from datetime import timedelta

from flask import abort, flash, redirect, render_template, request, url_for

from auth import redirect_to_dashboard

from . import all_portal_bp as bp
from .core import (KINDS, LEVEL_KEYS, LEVEL_KEY_LABEL, LEVEL_LABEL, POLICIES, POLICY_LABEL, add_request,
                   all_group_names, policy_of, policy_rows,
                   ancestors, children_of, descendant_ids, cursor, grants_of, item_row, item_groups, item_visible, items_of, visible_items, ITEM_POLICIES, level_of, now_jst, portal_row, portals_for,
                   requests_of, save_item, store_copy, subtree_base, user_info, web_who, write_log)
from . import urldoc
from .oauth import connections, csrf_ok, csrf_token, issuer, mcp_url, revoke_family

logger = logging.getLogger(__name__)

OPEN_VIEWS = {'all_portal.index', 'all_portal.portal', 'all_portal.item'}   # 未ログインでも開ける（一般公開のポータルだけが見える）
PUBLIC = {'all_portal.mcp', 'all_portal.oauth_register', 'all_portal.oauth_token', 'all_portal.oauth_revoke',
          'all_portal.wk_as', 'all_portal.wk_oidc', 'all_portal.wk_prm'}
RANK_RE = re.compile(r'-?\d{1,6}(\.\d{1,2})?')


@bp.before_request
def guard():
    """MCP と OAuth の機械向けの口を除き，ログインを求め，POST は CSRF を照合する"""
    if request.endpoint in PUBLIC:
        return None
    if not web_who().uid:
        if request.method == 'GET' and request.endpoint in OPEN_VIEWS:
            return None
        return redirect(url_for('auth.login', next=request.url))
    if request.method == 'POST' and not csrf_ok():
        abort(403)
    return None


def edit_home(p):
    """そのポータルを編集する画面の URL．最上位はポータルの画面，子ポータルは親の行のダッシュボード
    （親でアクトでない人には，子ポータルの画面のまま）"""
    if p.get('parent_id') and p.get('parent_item_id'):
        parent = portal_row(p['parent_id'])
        if parent and level_of(parent, web_who()) >= 3:
            return url_for('all_portal.item', iid=p['parent_item_id'])
    return url_for('all_portal.portal', pid=p['id'])


@bp.context_processor
def inject():
    return {'csrf_token': csrf_token, 'is_admin': web_who().admin, 'LEVEL_LABEL': LEVEL_LABEL,
            'logged_in': bool(web_who().uid), 'POLICY_LABEL': POLICY_LABEL, 'edit_home': edit_home}


def portal_or_404(pid, min_level=1):
    p = portal_row(pid)
    if not p:
        abort(404)
    lv = level_of(p, web_who())
    if lv < 1 and min_level <= 1 and any(item_visible(r, web_who(), 0) for r in items_of(pid)):
        lv = 0      # ポータルの閲覧は無いが，見える URL がある
    elif lv < 1:
        if not web_who().uid:
            return abort(redirect(url_for('auth.login', next=request.url)))
        abort(404)
    if lv < min_level and not (lv == 0 and min_level <= 1):
        abort(403)
    p['level'] = lv
    return p


def require_admin():
    if not web_who().admin:
        abort(403)


def num(raw, what):
    raw = (raw or '').strip().translate(str.maketrans('０１２３４５６７８９．－', '0123456789.-'))
    if not raw:
        return None
    if not RANK_RE.fullmatch(raw):
        raise ValueError(f'{what}は小数点以下2桁までの数で書いてください')
    return round(float(raw), 2)


# ── 入口 ──

@bp.route('/')
def index():
    who = web_who()
    rows = portals_for(who)
    todo = pending_for(who, rows)
    answered = answered_for(who, rows)
    return render_template('all_portal/index.html', rows=rows, todo=todo, answered=answered, kinds=KINDS)


def _parents():
    """{ポータル id: 親の id}（最上位は None）"""
    with cursor() as (cur, conn):
        cur.execute("SELECT id, parent_id FROM all_portals")
        return {r['id']: r['parent_id'] for r in cur.fetchall()}


def _root(pid, parents):
    n = 0
    while parents.get(pid) and n < 20:
        pid, n = parents[pid], n + 1
    return pid


def pending_for(who, rows):
    """最上位ポータルごとの，自分が対応すべき未処理のリクエスト：{最上位 id: {'n': 件数, 'pid': 飛び先}}
    アクトを持つポータル（子ポータルを含む）に届いた open のリクエストを数える"""
    if not who.uid or not rows:
        return {}
    with cursor() as (cur, conn):
        cur.execute("SELECT portal_id, COUNT(*) AS n FROM all_requests WHERE status = 'open' GROUP BY portal_id")
        counts = {r['portal_id']: r['n'] for r in cur.fetchall()}
    if not counts:
        return {}
    parents, tops, out = _parents(), {r['id'] for r in rows}, {}
    for pid, n in counts.items():
        top = _root(pid, parents)
        if top not in tops:
            continue
        p = portal_row(pid)
        if not p or level_of(p, who) < 3:
            continue
        x = out.setdefault(top, {'n': 0, 'pid': pid, 'best': 0, 'url': ''})
        x['n'] += n
        if n > x['best'] or not x['url']:
            x['pid'], x['best'], x['url'] = pid, n, edit_home(p) + '#requests'
    return out


def answered_for(who, rows, days=30):
    """自分が出したリクエストのうち，days 日以内に返答があったもの：{最上位 id: {'n': 件数, 'pid': 飛び先}}"""
    if not who.uid or not rows:
        return {}
    since = now_jst() - timedelta(days=days)
    with cursor() as (cur, conn):
        cur.execute("SELECT portal_id, COUNT(*) AS n FROM all_requests WHERE user_id = %s AND status <> 'open' "
                    "AND handled_at >= %s GROUP BY portal_id", (who.uid, since))
        counts = {r['portal_id']: r['n'] for r in cur.fetchall()}
    if not counts:
        return {}
    parents, tops, out = _parents(), {r['id'] for r in rows}, {}
    for pid, n in counts.items():
        top = _root(pid, parents)
        if top in tops:
            x = out.setdefault(top, {'n': 0, 'pid': pid})
            x['n'] += n
    return out


@bp.route('/admin')
def admin_home():
    """admin 専用の設定ダッシュボード：ポータルの作成，全ポータルの見渡し，Claude 接続への入口"""
    require_admin()
    with cursor() as (cur, conn):
        cur.execute("SELECT p.id, p.kind, p.name, p.parent_id, p.parent_item_id, p.ai_act, "
                    "(SELECT COUNT(*) FROM all_items i WHERE i.portal_id = p.id) AS n_items, "
                    "(SELECT COUNT(*) FROM all_requests r WHERE r.portal_id = p.id AND r.status = 'open') AS n_open "
                    "FROM all_portals p ORDER BY p.id")
        allp = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS n FROM all_items")
        n_items = cur.fetchone()['n']
    stats = {'portals': sum(1 for p in allp if not p['parent_id']),
             'children': sum(1 for p in allp if p['parent_id']),
             'items': n_items,
             'open': sum(p['n_open'] for p in allp),
             'connections': len(connections())}
    for p in allp:
        p['home'] = edit_home(p)
    opens = [p for p in allp if p['n_open']]
    # 親子の木の順（最上位は種類・並び順，子は id 順）に並べ，深さを付ける
    kids = {}
    for p in allp:
        kids.setdefault(p['parent_id'], []).append(p)
    order = {k: i for i, k in enumerate(KINDS)}
    tops = sorted(kids.get(None, []), key=lambda p: (order.get(p['kind'], 99), p['id']))
    tree, stack = [], [(p, 0) for p in reversed(tops)]
    while stack:
        p, d = stack.pop()
        p['depth'] = d
        tree.append(p)
        if d < 20:
            stack += [(c, d + 1) for c in reversed(kids.get(p['id'], []))]
    return render_template('all_portal/admin.html', stats=stats, opens=opens, tree=tree, kinds=KINDS)


@bp.route('/portals/new', methods=['POST'])
def portal_new():
    require_admin()
    f = request.form
    kind = f.get('kind') if f.get('kind') in KINDS else KINDS[0]
    name = (f.get('name') or '').strip()[:200]
    if not name:
        flash('ポータルの名前を書いてください', 'error')
        return redirect(url_for('all_portal.admin_home'))
    owner = None   # 持ち主は当面 admin に限る（列は残す）
    stamp = now_jst()
    with cursor() as (cur, conn):
        cur.execute("INSERT INTO all_portals (kind, name, description, owner_user_id, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (kind, name, (f.get('description') or '').strip() or None, owner, stamp, stamp))
        pid = cur.lastrowid
        conn.commit()
    write_log(web_who(), 'ポータルの作成', pid, None, {'kind': kind, 'name': name})
    flash(f'{kind}ポータル「{name}」を作りました．権限を設定してください', 'success')
    return redirect(url_for('all_portal.portal_settings', pid=pid))


@bp.route('/p/<int:pid>')
def portal(pid):
    p = portal_or_404(pid)
    who = web_who()
    home = edit_home(p)
    # 編集はこの画面か，子ポータルなら親の行のダッシュボードで（親でアクトの人には，ここは読むだけの画面）
    edit = p['level'] >= 3 and home == url_for('all_portal.portal', pid=pid)
    reqs = requests_of(pid) if edit else []
    mine = []
    if who.uid and not edit:
        mine = [r for r in requests_of(pid) if r.get('user_id') == who.uid]
    items = visible_items(pid, who, p['level'] if edit else min(p['level'], 2))
    return render_template('all_portal/portal.html', p=p, items=items, edit=edit, home=home,
                           reqs=reqs, mine=mine, kids=children_of(pid), crumbs=ancestors(p))


@bp.route('/i/<int:iid>/make_portal', methods=['POST'])
def item_make_portal(iid):
    """その行（URL）を子ポータルにする．子ポータルの最初の行は元の URL"""
    it = item_row(iid)
    if not it:
        abort(404)
    p = portal_or_404(it['portal_id'], 3)
    kid = children_of(p['id']).get(iid)
    if kid:
        return redirect(url_for('all_portal.item', iid=iid))
    stamp = now_jst()
    with cursor() as (cur, conn):
        cur.execute("INSERT INTO all_portals (kind, name, description, ai_act, parent_id, parent_item_id, "
                    "created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (p['kind'], it['title'], it.get('note'), p['ai_act'], p['id'], iid, stamp, stamp))
        cid = cur.lastrowid
        conn.commit()
    if it.get('url'):
        save_item(web_who(), cid, {'title': it['title'], 'url': it['url'], 'note': None, 'source': 'url',
                                   'access_policy': 'inherit', 'access_groups': None, 'sort_order': 1,
                                   'body': it.get('body'), 'fetched_at': it.get('fetched_at')})
    write_log(web_who(), '子ポータルの作成', cid, iid, {'name': it['title'], 'parent': p['id']})
    flash(f'「{it["title"]}」をポータルにしました．このページで子ポータルを編集できます．'
          '「リンクを拾う」で元のページのリンクを子ポータルに登録できます', 'success')
    return redirect(url_for('all_portal.item', iid=iid))


@bp.route('/i/<int:iid>/harvest', methods=['GET', 'POST'])
def item_harvest(iid):
    """その行の URL のページにあるリンクを候補に出し，選んだものをこのポータルに登録する"""
    it = item_row(iid)
    if not it or not it.get('url'):
        abort(404)
    p = portal_or_404(it['portal_id'], 3)
    kid = children_of(p['id']).get(iid)
    tp = kid or p          # 行が子ポータルなら，拾ったリンクは子ポータルに入れる
    back = url_for('all_portal.item', iid=iid) if kid else edit_home(p)
    if request.method == 'POST':
        f = request.form
        picks = f.getlist('pick')
        rows = items_of(tp['id'])
        top = max([float(r['sort_order']) for r in rows if r['sort_order'] is not None] or [0])
        n = 0
        for k in picks:
            url = (f.get('url_' + k) or '').strip()
            title = (f.get('title_' + k) or '').strip()[:500] or url
            try:
                url = urldoc.normalize(url)
            except ValueError:
                continue
            n += 1
            save_item(web_who(), tp['id'], {'title': title, 'url': url, 'note': None, 'source': 'url',
                                            'access_policy': 'inherit', 'access_groups': None,
                                            'sort_order': round(top + n, 2)})
        flash(f'{n} 件の URL を「{tp["name"]}」に登録しました．「写しをまとめて更新」で中身を読んでおくと検索できます',
              'success')
        return redirect(back)
    try:
        html, final = urldoc.fetch_html(it['url'], web_who())
        _, frag = urldoc.split_fragment(it['url'])
        links = urldoc.extract_links(html, final, frag)
    except urldoc.FetchError as e:
        flash(f'リンクを取り出せませんでした：{e}', 'error')
        return redirect(back)
    have = {(r['url'] or '').split('#', 1)[0] for r in items_of(tp['id'])}
    for x in links:
        x['have'] = x['url'].split('#', 1)[0] in have
        x['pdf'] = x['url'].lower().split('?', 1)[0].endswith('.pdf')
    return render_template('all_portal/harvest.html', p=p, it=it, tp=tp, back=back, links=links)


@bp.route('/p/<int:pid>/refresh', methods=['POST'])
def portal_refresh(pid):
    """写しの古いものから順に URL を読み直す（1回あたり約60秒まで）"""
    import time
    p = portal_or_404(pid, 3)
    rows = [r for r in items_of(pid) if r.get('source') == 'url' and r.get('url')]
    rows.sort(key=lambda r: (r['fetched_at'] is not None, r['fetched_at'] or 0))
    t0, ok, ng = time.time(), 0, []
    for r in rows:
        if time.time() - t0 > 60:
            break
        try:
            store_copy(r['id'], urldoc.read_text(r['url'], web_who()))
            ok += 1
        except urldoc.FetchError as e:
            ng.append(f"{r['title']}（{e}）")
        except Exception:
            logger.exception('all_portal refresh %s', r['url'])
            ng.append(f"{r['title']}（エラー）")
    rest = len(rows) - ok - len(ng)
    msg = f'{ok} 件の写しを更新しました．'
    if rest > 0:
        msg += f'残り {rest} 件はもう一度押すと続きを読みます．'
    flash(msg, 'success')
    if ng:
        flash('読めなかったもの：' + '，'.join(ng[:10]) + ('…' if len(ng) > 10 else ''), 'error')
    return redirect(edit_home(p))


@bp.route('/p/<int:pid>/order', methods=['POST'])
def portal_order(pid):
    """一覧の順序数をまとめて保存する（renumber があれば今の並びで 1.00，2.00，… に振り直す）"""
    p = portal_or_404(pid, 3)
    rows = items_of(pid)
    stamp = now_jst()
    with cursor() as (cur, conn):
        if request.form.get('renumber'):
            for n, r in enumerate(rows, 1):
                cur.execute("UPDATE all_items SET sort_order = %s WHERE id = %s AND portal_id = %s", (n, r['id'], pid))
            msg = '順序数を 1.00 から振り直しました'
        else:
            try:
                plan = {r['id']: num(request.form.get(f"o_{r['id']}"), '順序数') for r in rows}
            except ValueError as e:
                flash(str(e), 'error')
                return redirect(edit_home(p))
            for iid, o in plan.items():
                cur.execute("UPDATE all_items SET sort_order = %s, updated_at = %s WHERE id = %s AND portal_id = %s",
                            (o, stamp, iid, pid))
            msg = '並びを保存しました'
        conn.commit()
    write_log(web_who(), '並びの変更', pid, None, None)
    flash(msg, 'success')
    return redirect(edit_home(p))


@bp.route('/i/<int:iid>')
def item(iid):
    it = item_row(iid)
    if not it:
        abort(404)
    p = portal_or_404(it['portal_id'])
    if not item_visible(it, web_who(), p['level']):
        if not web_who().uid:
            return redirect(url_for('auth.login', next=request.url))
        abort(404)
    kid = children_of(p['id']).get(iid) if p['level'] >= 3 else None
    kid_items, kid_kids, kid_reqs = [], {}, []
    if kid:
        # この行は子ポータル：子ポータルの編集はここで行う
        kid_items, kid_kids, kid_reqs = items_of(kid['id']), children_of(kid['id']), requests_of(kid['id'])
    crumbs = [{'name': a['name'], 'url': edit_home(a)} for a in ancestors(p) + [p]]
    return render_template('all_portal/item.html', p=p, it=it, kid=kid, crumbs=crumbs,
                           kid_items=kid_items, kid_kids=kid_kids, kid_reqs=kid_reqs)


# ── アクト：文書の登録・編集・削除 ──

@bp.route('/p/<int:pid>/item', methods=['GET', 'POST'])
@bp.route('/i/<int:iid>/edit', methods=['GET', 'POST'])
def item_edit(pid=None, iid=None):
    it = item_row(iid) if iid else None
    if iid and not it:
        abort(404)
    p = portal_or_404(it['portal_id'] if it else pid, 3)
    if request.method == 'POST':
        f = request.form
        try:
            policy = f.get('access_policy') or 'inherit'
            if policy not in ITEM_POLICIES:
                raise ValueError('公開範囲を選んでください')
            known = set(all_group_names())
            groups = [x for x in dict.fromkeys(f.getlist('access_groups')) if x in known]
            if policy in ('group', 'domestic_group') and not groups:
                raise ValueError('グループを1つ以上選んでください')
            vals = {'title': (f.get('title') or '').strip()[:500],
                    'url': urldoc.normalize(f.get('url')),
                    'note': (f.get('note') or '').strip() or None,
                    'source': 'url',
                    'access_policy': policy,
                    'access_groups': json.dumps(groups, ensure_ascii=False) if policy in ('group', 'domestic_group') else None,
                    'sort_order': num(f.get('sort_order'), '順序数')}
            if not vals['title']:
                raise ValueError('題名を書いてください')
            # 配下も読める（入口）：admin だけが変えられる．ほかの人の保存では元の値を残す
            if web_who().admin:
                vals['subtree'] = 1 if f.get('subtree') else 0
                if vals['subtree'] and subtree_base(vals['url']) is None:
                    raise ValueError('「配下も読める」は，このサイトの URL で，最上位（/）とオール自身以外のものにだけ付けられます')
        except ValueError as e:
            flash(str(e), 'error')
            back = dict(it or {}, **request.form.to_dict())
            back['groups_list'] = request.form.getlist('access_groups')
            return render_template('all_portal/item_edit.html', p=p, it=_form_item(back), groups=all_group_names(),
                                   policies=ITEM_POLICIES, home=edit_home(p))
        new_id = save_item(web_who(), p['id'], vals, iid)
        refetch_copy(new_id, vals['url'])
        if iid:
            return redirect(url_for('all_portal.item', iid=iid))
        return redirect(edit_home(p))
    it = dict(it, groups_list=item_groups(it)) if it else {}
    return render_template('all_portal/item_edit.html', p=p, it=_form_item(it),
                           groups=all_group_names(), policies=ITEM_POLICIES, home=edit_home(p))


def _form_item(it):
    """編集画面に渡す行．テンプレートで使う鍵をすべてそろえる（未定義の鍵は StrictUndefined でエラーになる）"""
    base = {'id': None, 'title': '', 'url': '', 'note': '', 'sort_order': None,
            'access_policy': 'inherit', 'groups_list': [], 'subtree': 0}
    base.update({k: v for k, v in (it or {}).items() if v is not None or k in ('sort_order',)})
    return base


def refetch_copy(iid, url):
    """URL 文書をたたいて写しを置き直す．結果は flash で知らせる"""
    try:
        text = urldoc.read_text(url, web_who())
    except urldoc.FetchError as e:
        flash(f'保存しました．ただし URL を読めませんでした：{e}', 'error')
        return False
    except Exception:
        logger.exception('all_portal refetch %s', url)
        flash('保存しました．ただし URL を読む途中でエラーが起きました', 'error')
        return False
    store_copy(iid, text)
    flash(f'保存しました．URL を読んで {len(text):,} 文字の写しを置きました', 'success')
    return True


@bp.route('/i/<int:iid>/refetch', methods=['POST'])
def item_refetch(iid):
    it = item_row(iid)
    if not it or it.get('source') != 'url':
        abort(404)
    p = portal_or_404(it['portal_id'], 3)
    if refetch_copy(iid, it['url']):
        write_log(web_who(), '写しの読み直し', p['id'], iid, {'url': it['url']})
    return redirect(url_for('all_portal.item', iid=iid))


@bp.route('/i/<int:iid>/delete', methods=['POST'])
def item_delete(iid):
    """行をポータルから外す．行が子ポータルになっていれば，子ポータル（中の行・権限・リクエストと，その下の孫ポータル）も一緒に消す"""
    it = item_row(iid)
    if not it:
        abort(404)
    p = portal_or_404(it['portal_id'], 3)
    kid = children_of(p['id']).get(iid)
    ids = ([kid['id']] + descendant_ids(kid['id'])) if kid else []
    with cursor() as (cur, conn):
        for x in ids:
            for t in ('all_items', 'all_grants', 'all_requests'):
                cur.execute(f"DELETE FROM {t} WHERE portal_id = %s", (x,))
            cur.execute("DELETE FROM all_portals WHERE id = %s", (x,))
        cur.execute("DELETE FROM all_items WHERE id = %s", (iid,))
        conn.commit()
    write_log(web_who(), '文書の削除', p['id'], iid, {'title': it['title'], 'with_portals': len(ids)})
    flash(f'「{it["title"]}」を外しました' + (f'（子ポータル {len(ids)} 件も一緒に消しました）' if ids else ''), 'success')
    back = request.form.get('back') or ''
    return redirect(back if back.startswith('/') and not back.startswith('//') else edit_home(p))


# ── リクエスト ──

@bp.route('/p/<int:pid>/request', methods=['POST'])
def request_new(pid):
    p = portal_or_404(pid, 2)
    body = (request.form.get('body') or '').strip()
    if not body:
        flash('リクエストの中身を書いてください', 'error')
    elif len(body) > 5000:
        flash('リクエストは5000文字までです', 'error')
    else:
        add_request(web_who(), p['id'], body)
        flash('リクエストを出しました', 'success')
    return redirect(url_for('all_portal.portal', pid=pid))


@bp.route('/r/<int:rid>/handle', methods=['POST'])
def request_handle(rid):
    with cursor() as (cur, conn):
        cur.execute("SELECT * FROM all_requests WHERE id = %s", (rid,))
        r = cur.fetchone()
    if not r:
        abort(404)
    p = portal_or_404(r['portal_id'], 3)
    status = request.form.get('status')
    if status not in ('done', 'rejected', 'open'):
        abort(400)
    with cursor() as (cur, conn):
        cur.execute("UPDATE all_requests SET status = %s, response = %s, handled_by = %s, handled_at = %s "
                    "WHERE id = %s", (status, (request.form.get('response') or '').strip() or None,
                                      web_who().name, now_jst(), rid))
        conn.commit()
    write_log(web_who(), 'リクエストの受付', p['id'], rid, {'status': status})
    flash('リクエストを更新しました', 'success')
    return redirect(edit_home(p) + '#requests')


# ── ポータルの設定と権限（admin） ──

@bp.route('/p/<int:pid>/settings', methods=['GET', 'POST'])
def portal_settings(pid):
    require_admin()
    p = portal_or_404(pid)
    if request.method == 'POST':
        f = request.form
        try:
            vals = {'kind': f.get('kind') if f.get('kind') in KINDS else p['kind'],
                    'name': (f.get('name') or '').strip()[:200] or p['name'],
                    'description': (f.get('description') or '').strip() or None,
                    'ai_act': 1 if f.get('ai_act') else 0,
                    'sort_order': num(f.get('sort_order'), '並び順')}
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(url_for('all_portal.portal_settings', pid=pid))
        with cursor() as (cur, conn):
            sets = ', '.join(f'{k} = %s' for k in vals) + ', updated_at = %s'
            cur.execute(f"UPDATE all_portals SET {sets} WHERE id = %s", list(vals.values()) + [now_jst(), pid])
            conn.commit()
        write_log(web_who(), 'ポータルの設定', pid, None, vals)
        flash('ポータルの設定を保存しました', 'success')
        return redirect(url_for('all_portal.portal_settings', pid=pid))
    grants = grants_of([pid]).get(pid, [])
    pol = {lv: dict(zip(('policy', 'groups', 'legacy'), policy_of(grants, lv))) for lv in LEVEL_KEYS}
    return render_template('all_portal/settings.html', p=p, home=edit_home(p), kinds=KINDS, pol=pol,
                           groups=all_group_names(),
                           level_keys=LEVEL_KEYS, level_key_label=LEVEL_KEY_LABEL,
                           policies=POLICIES, policy_label=POLICY_LABEL)


@bp.route('/p/<int:pid>/policy', methods=['POST'])
def policy_save(pid):
    """閲覧・リクエスト・アクトの公開範囲をまとめて保存する（この portal の権限の行をすべて置き換える）"""
    require_admin()
    portal_or_404(pid)
    f = request.form
    known = set(all_group_names())
    plan, summary = [], {}
    for lv in LEVEL_KEYS:
        policy = f.get('policy_' + lv, 'private')
        if policy not in POLICIES or (policy == 'public' and lv != 'view'):
            abort(400)
        groups = [g for g in dict.fromkeys(f.getlist('groups_' + lv)) if g in known]
        if policy in ('group', 'domestic_group') and not groups:
            flash(f'{LEVEL_KEY_LABEL[lv]}：グループを1つ以上選んでください', 'error')
            return redirect(url_for('all_portal.portal_settings', pid=pid))
        plan += [(sk, sub, lv) for sk, sub in policy_rows(policy, groups)]
        summary[lv] = {'policy': policy, 'groups': groups if policy in ('group', 'domestic_group') else []}
    stamp = now_jst()
    with cursor() as (cur, conn):
        cur.execute("DELETE FROM all_grants WHERE portal_id = %s", (pid,))
        for sk, sub, lv in plan:
            cur.execute("INSERT INTO all_grants (portal_id, subject_kind, subject, level, created_at) "
                        "VALUES (%s,%s,%s,%s,%s)", (pid, sk, sub, lv, stamp))
        conn.commit()
    write_log(web_who(), '公開範囲の設定', pid, None, summary)
    flash('公開範囲を保存しました', 'success')
    return redirect(url_for('all_portal.portal_settings', pid=pid))


@bp.route('/p/<int:pid>/delete', methods=['POST'])
def portal_delete(pid):
    require_admin()
    p = portal_or_404(pid)
    ids = [pid] + descendant_ids(pid)
    with cursor() as (cur, conn):
        for x in ids:
            for t in ('all_items', 'all_grants', 'all_requests'):
                cur.execute(f"DELETE FROM {t} WHERE portal_id = %s", (x,))
            cur.execute("DELETE FROM all_portals WHERE id = %s", (x,))
        conn.commit()
    write_log(web_who(), 'ポータルの削除', pid, None, {'name': p['name'], 'with_children': len(ids) - 1})
    flash(f'ポータル「{p["name"]}」を消しました' + (f'（下の子ポータル {len(ids) - 1} 件も）' if len(ids) > 1 else ''),
          'success')
    return redirect(url_for('all_portal.admin_home'))


# ── Claude 接続（MCP）の案内と管理 ──

@bp.route('/connect')
def connect():
    require_admin()
    who = web_who()
    logs = []
    if who.admin:
        via = request.args.get('via', '')
        with cursor() as (cur, conn):
            where, params = '', []
            if via in ('web', 'mcp'):
                where, params = ' WHERE via = %s', [via]
            cur.execute(f"SELECT * FROM all_log{where} ORDER BY at DESC, id DESC LIMIT 200", params)
            logs = cur.fetchall()
    from .mcp import TOOLS
    return render_template('all_portal/connect.html', url=mcp_url(), issuer=issuer(), tools=TOOLS,
                           mine=connections(who.uid), everyone=connections() if who.admin else [],
                           logs=logs, via=request.args.get('via', ''),
                           portals=portals_for(who))


@bp.route('/connect/revoke', methods=['POST'])
def connect_revoke():
    who = web_who()
    n = revoke_family(request.form.get('family') or '', None if who.admin else who.uid)
    if n:
        write_log(who, '接続の切断', None, None, None)
    flash('接続を切りました' if n else '該当する接続がありません', 'success' if n else 'error')
    return redirect(url_for('all_portal.connect'))


@bp.route('/return_to_fujin')
def return_to_fujin():
    return redirect_to_dashboard()

