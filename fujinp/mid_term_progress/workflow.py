"""
mid_term_progress - 執筆依頼のワークフロー（依頼の記録・状態・コメントと，執筆者用ダッシュボード）

依頼の単位は「年度 × 中期計画番号 × 年度計画番号 × 細目 × 段階」．
段階は plan（計画）／progress（進捗報告）／result（業務実績報告）の3つ．
依頼の状態と評価室からのコメントは T_ann_plan_requests（nishida$fujinp）に置き，
本文は常設表 T_ann_plan_details を読む（ここでは書かない）．

状態は2系統を並べて持つ．
  編集者側 editor_status：requested（依頼中）／confirmed（確認済み）
  執筆者側 author_status：writing（執筆中）／done（執筆完了）／revised（改訂済み）
"""
import logging
from flask import render_template, request, jsonify, session, abort, url_for

from . import mid_term_progress_bp
from .routes import (login_required, require_upload_perm, can_upload, _fdb, _s, safe_int,
                     get_jst_now, md_to_html, _get_user_group_names, _goal_code, _goal_title,
                     SAME_MARK, is_same, resolve_same, DONE_TEXT, is_done, DONE_SQL)

REQ_TABLE = 'T_ann_plan_requests'
STAGES = [('plan', '計画'), ('progress', '進捗報告'), ('result', '業務実績報告')]
STAGE_LABEL = dict(STAGES)
EDITOR_STATUS = {'requested': '依頼中', 'confirmed': '確認済み'}
AUTHOR_STATUS = {'writing': '執筆中', 'done': '執筆完了', 'revised': '改訂済み'}
TEXT_COL = {'plan': 'plan_text', 'progress': 'progress_text', 'result': 'result_text'}
KEY = ('fiscal_year', 'mid_plan_no', 'annual_plan_no', 'detail_no')


def _key_from(b):
    k = {c: safe_int(b.get(c)) for c in KEY}
    if any(v is None for v in k.values()):
        return None
    return k


def _stage_from(b):
    st = (b.get('stage') or '').strip()
    return st if st in STAGE_LABEL else None


def _jbody():
    return request.get_json(silent=True) or {}


def _req_table_ready(cur):
    try:
        cur.execute('SELECT 1 FROM %s LIMIT 0' % REQ_TABLE)
        cur.fetchall()
        return True
    except Exception:
        return False


def _author_cols_ready(cur):
    """T_ann_plan_requests に執筆者の提出の列（author_text）があるか。"""
    try:
        cur.execute("SHOW COLUMNS FROM %s LIKE 'author_text'" % REQ_TABLE)
        return bool(cur.fetchall())
    except Exception:
        return False


def _extra_cols_ready(cur):
    """業務実績報告の追加欄（自己評価へのコメント・エビデンス）の列があるか。"""
    try:
        cur.execute("SHOW COLUMNS FROM %s LIKE 'author_evidence'" % REQ_TABLE)
        return bool(cur.fetchall())
    except Exception:
        return False


REPORT_STAGES = ('progress', 'result')   # 執筆者の提出と編集者の承認版を分ける段階

# 執筆者に開いている依頼（年度×段階の組）．編集者ダッシュボードで選ぶ．サイトで1本持つ
import os as _os, json as _json
_ACTIVE_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'data', 'active_sets.json')


def load_active_sets():
    try:
        with open(_ACTIVE_FILE, encoding='utf-8') as f:
            v = _json.load(f)
        out = []
        for x in v.get('sets', []):
            y, st = safe_int(x.get('year')), x.get('stage')
            if y and st in STAGE_LABEL and (y, st) not in [(a['year'], a['stage']) for a in out]:
                out.append({'year': y, 'stage': st})
        return sorted(out, key=lambda a: (a['year'], list(STAGE_LABEL).index(a['stage'])))
    except Exception:
        return []


def save_active_sets(sets):
    _os.makedirs(_os.path.dirname(_ACTIVE_FILE), exist_ok=True)
    with open(_ACTIVE_FILE, 'w', encoding='utf-8') as f:
        _json.dump({'sets': sets}, f, ensure_ascii=False, indent=1)


def is_active(year, stage):
    return any(a['year'] == year and a['stage'] == stage for a in load_active_sets())


def _now_uid():
    return get_jst_now(), session.get('user_id')


def _my_groups():
    return [g for g in _get_user_group_names(session.get('user_id')) if g]


def _is_owner_member(owner):
    return bool(owner) and owner in _my_groups()


def _mid_heads(cur, year):
    """その年度に有効な中期計画（細目1）の見出し。mid_plan_no → dict"""
    cur.execute("""SELECT term, sub_term, mid_plan_no, mid_goal_no, goal_chapter, goal_section, goal_item,
                          goal_item_title, goal_section_title, goal_chapter_title, mid_plan_text
                   FROM T_mid_plan_details
                   WHERE detail_no = 1 AND %s BETWEEN COALESCE(valid_from, 0) AND COALESCE(valid_to, 9999)
                   ORDER BY mid_plan_no, sub_term DESC""", (year,))
    out = {}
    for m in cur.fetchall():
        out.setdefault(m['mid_plan_no'], m)
    return out


def _status_label(r):
    return {'editor': EDITOR_STATUS.get(r.get('editor_status') or '', ''),
            'author': AUTHOR_STATUS.get(r.get('author_status') or '', '')}


def _req_out(r):
    d = {k: _s(r.get(k)) for k in ('stage', 'editor_status', 'author_status', 'owner_group',
                                  'requested_at', 'editor_updated_at', 'author_updated_at', 'comment_updated_at')}
    d.update({'stage_label': STAGE_LABEL.get(r.get('stage'), ''), 'labels': _status_label(r),
              'has_comment': bool((r.get('comment') or '').strip())})
    return d


# -----------------------------------------------
# 年度のリスト（依頼のある年度＋本文表の年度）
# -----------------------------------------------
def _years(cur):
    cur.execute('SELECT DISTINCT fiscal_year FROM T_ann_plan_details ORDER BY fiscal_year')
    return [r['fiscal_year'] for r in cur.fetchall()]


# -----------------------------------------------
# トップページ用：自分のグループへの未完了の依頼の数
# -----------------------------------------------
def open_request_count():
    """トップページ用：執筆者に開いている依頼のうち，自分のグループが担当で，まだ提出していない細目の数。"""
    groups = _my_groups()
    sets = load_active_sets()
    if not groups or not sets:
        return 0
    try:
        with _fdb() as (cur, conn):
            if not _req_table_ready(cur):
                return 0
            n = 0
            for a in sets:
                cur.execute("""SELECT COUNT(*) AS n FROM T_ann_plan_details d
                               LEFT JOIN %s q ON q.fiscal_year = d.fiscal_year AND q.mid_plan_no = d.mid_plan_no
                                AND q.annual_plan_no = d.annual_plan_no AND q.detail_no = d.detail_no AND q.stage = %%s
                               WHERE d.fiscal_year = %%s AND NOT %s AND d.owner_group IN (%s)
                                 AND (q.author_status IS NULL OR q.author_status = 'writing')
                                 AND (q.editor_status IS NULL OR q.editor_status <> 'confirmed')"""
                            % (REQ_TABLE, DONE_SQL % 'd.plan_text', ','.join(['%s'] * len(groups))),
                            (a['stage'], a['year']) + tuple(groups))
                n += cur.fetchone()['n'] or 0
            return n
    except Exception as e:
        logging.warning('open_request_count: %s', e)
        return 0


def _upsert_request(cur, k, stage, fields):
    """依頼の行を作る（無ければ）→ fields で更新。"""
    now, uid = _now_uid()
    cur.execute("""INSERT IGNORE INTO %s (fiscal_year, mid_plan_no, annual_plan_no, detail_no, stage,
                                         created_at, updated_at) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s)""" % REQ_TABLE,
                (k['fiscal_year'], k['mid_plan_no'], k['annual_plan_no'], k['detail_no'], stage, now, now))
    if fields:
        sets = ', '.join('%s = %%s' % c for c in fields) + ', updated_at = %s'
        cur.execute('UPDATE %s SET %s WHERE fiscal_year=%%s AND mid_plan_no=%%s AND annual_plan_no=%%s '
                    'AND detail_no=%%s AND stage=%%s' % (REQ_TABLE, sets),
                    tuple(fields.values()) + (now, k['fiscal_year'], k['mid_plan_no'], k['annual_plan_no'],
                                              k['detail_no'], stage))


def _owner_of(cur, k):
    cur.execute('SELECT owner_group FROM T_ann_plan_details WHERE fiscal_year=%s AND mid_plan_no=%s '
                'AND annual_plan_no=%s AND detail_no=%s', tuple(k[c] for c in KEY))
    r = cur.fetchone()
    if r is None:
        return None, False
    return (r.get('owner_group') or '').strip(), True


def request_all(cur, year, stage, now, uid, group=''):
    """担当のある細目（対応済みを除く）すべてを，指定段階について依頼中にする（依頼の無い行だけ）。"""
    args = [year]
    cond = "owner_group IS NOT NULL AND owner_group <> ''"
    if group:
        cond = 'owner_group = %s'
        args.append(group)
    cur.execute("""SELECT fiscal_year, mid_plan_no, annual_plan_no, detail_no, owner_group
                   FROM T_ann_plan_details WHERE fiscal_year = %%s AND %s AND NOT %s"""
                % (cond, DONE_SQL % 'plan_text'), tuple(args))
    rows = cur.fetchall()
    cnt_sql = ("SELECT COUNT(*) AS n FROM %s WHERE fiscal_year = %%s AND stage = %%s "
               "AND editor_status IS NOT NULL" % REQ_TABLE)
    cur.execute(cnt_sql, (year, stage))
    before = cur.fetchone()['n']
    for r in rows:
        cur.execute("""INSERT INTO %s (fiscal_year, mid_plan_no, annual_plan_no, detail_no, stage, owner_group,
                                       editor_status, requested_at, requested_by, editor_updated_at,
                                       editor_updated_by, created_at, updated_at)
                       VALUES (%%s,%%s,%%s,%%s,%%s,%%s,'requested',%%s,%%s,%%s,%%s,%%s,%%s)
                       ON DUPLICATE KEY UPDATE
                         owner_group = IF(editor_status IS NULL, VALUES(owner_group), owner_group),
                         requested_at = IF(editor_status IS NULL, VALUES(requested_at), requested_at),
                         requested_by = IF(editor_status IS NULL, VALUES(requested_by), requested_by),
                         editor_status = IF(editor_status IS NULL, 'requested', editor_status)""" % REQ_TABLE,
                    (r['fiscal_year'], r['mid_plan_no'], r['annual_plan_no'], r['detail_no'], stage,
                     r['owner_group'], now, uid, now, uid, now, now))
    cur.execute(cnt_sql, (year, stage))
    return cur.fetchone()['n'] - before, len(rows)


@mid_term_progress_bp.route('/api/editor/active', methods=['GET', 'POST'])
@login_required
@require_upload_perm
def api_editor_active():
    """執筆者に開く依頼（年度×段階）の一覧と切り替え。開くときは，担当のある細目をその段階について依頼中にする。"""
    if request.method == 'GET':
        return jsonify({'success': True, 'sets': load_active_sets()})
    b = _jbody()
    year, stage = safe_int(b.get('year')), _stage_from(b)
    on = bool(b.get('on'))
    if not year or not stage:
        return jsonify({'success': False, 'error': '年度と段階を指定してください'}), 400
    sets = [a for a in load_active_sets() if not (a['year'] == year and a['stage'] == stage)]
    n = 0
    if on:
        sets.append({'year': year, 'stage': stage})
        now, uid = _now_uid()
        with _fdb() as (cur, conn):
            if _req_table_ready(cur):
                n, _ = request_all(cur, year, stage, now, uid)
                conn.commit()
    save_active_sets(sets)
    return jsonify({'success': True, 'sets': load_active_sets(), 'requested': n})


@mid_term_progress_bp.route('/api/editor/request_all', methods=['POST'])
@login_required
@require_upload_perm
def api_editor_request_all():
    """担当の決まっている細目すべてに，指定段階の依頼を出す（依頼の無い行だけ）。"""
    b = _jbody()
    year, stage = safe_int(b.get('year')), _stage_from(b)
    group = (b.get('group') or '').strip()
    if not year or not stage:
        return jsonify({'success': False, 'error': '年度と段階を指定してください'}), 400
    now, uid = _now_uid()
    with _fdb() as (cur, conn):
        n, targets = request_all(cur, year, stage, now, uid, group)
        conn.commit()
    return jsonify({'success': True, 'requested': n, 'targets': targets})


@mid_term_progress_bp.route('/api/editor/status', methods=['POST'])
@login_required
@require_upload_perm
def api_editor_status():
    """編集者側の状態を決める：requested（依頼中）／confirmed（確認済み）／cancel（依頼の取り消し）。"""
    b = _jbody()
    k, stage = _key_from(b), _stage_from(b)
    st = (b.get('status') or '').strip()
    if not k or not stage or st not in ('requested', 'confirmed', 'cancel'):
        return jsonify({'success': False, 'error': '細目・段階・状態を指定してください'}), 400
    now, uid = _now_uid()
    with _fdb() as (cur, conn):
        owner, found = _owner_of(cur, k)
        if not found:
            return jsonify({'success': False, 'error': '細目が見つかりません'}), 404
        if st == 'cancel':
            cur.execute('SELECT author_status, comment FROM %s WHERE fiscal_year=%%s AND mid_plan_no=%%s '
                        'AND annual_plan_no=%%s AND detail_no=%%s AND stage=%%s' % REQ_TABLE,
                        tuple(k[c] for c in KEY) + (stage,))
            q = cur.fetchone()
            if q and (q.get('author_status') or (q.get('comment') or '').strip()):
                _upsert_request(cur, k, stage, {'editor_status': None, 'editor_updated_at': now,
                                                'editor_updated_by': uid})
            elif q:
                cur.execute('DELETE FROM %s WHERE fiscal_year=%%s AND mid_plan_no=%%s AND annual_plan_no=%%s '
                            'AND detail_no=%%s AND stage=%%s' % REQ_TABLE, tuple(k[c] for c in KEY) + (stage,))
        else:
            f = {'editor_status': st, 'editor_updated_at': now, 'editor_updated_by': uid}
            if st == 'requested':
                if not owner:
                    return jsonify({'success': False, 'error': '担当グループが決まっていません'}), 400
                cur.execute('SELECT editor_status FROM %s WHERE fiscal_year=%%s AND mid_plan_no=%%s '
                            'AND annual_plan_no=%%s AND detail_no=%%s AND stage=%%s' % REQ_TABLE,
                            tuple(k[c] for c in KEY) + (stage,))
                q = cur.fetchone()
                if not q or not q.get('editor_status'):
                    f.update({'owner_group': owner, 'requested_at': now, 'requested_by': uid})
            _upsert_request(cur, k, stage, f)
        conn.commit()
    return jsonify({'success': True})



@mid_term_progress_bp.route('/api/editor/comment', methods=['POST'])
@login_required
@require_upload_perm
def api_editor_comment():
    """評価室からのコメントを書く（段階ごと）。"""
    b = _jbody()
    k, stage = _key_from(b), _stage_from(b)
    if not k or not stage:
        return jsonify({'success': False, 'error': '細目と段階を指定してください'}), 400
    comment = (b.get('comment') or '').strip() or None
    now, uid = _now_uid()
    with _fdb() as (cur, conn):
        _, found = _owner_of(cur, k)
        if not found:
            return jsonify({'success': False, 'error': '細目が見つかりません'}), 404
        _upsert_request(cur, k, stage, {'comment': comment, 'comment_updated_at': now, 'comment_updated_by': uid})
        conn.commit()
    return jsonify({'success': True, 'comment_html': md_to_html(comment or '')})


# -----------------------------------------------
# 2. 執筆者の状態（画面は authorview.py）
# -----------------------------------------------
@mid_term_progress_bp.route('/api/author/status', methods=['POST'])
@login_required
def api_author_status():
    """執筆者側の状態を決める：writing／done／revised（空で取り消し）。担当グループの人だけ。"""
    b = _jbody()
    k, stage = _key_from(b), _stage_from(b)
    st = (b.get('status') or '').strip() or None
    if not k or not stage or (st is not None and st not in AUTHOR_STATUS):
        return jsonify({'success': False, 'error': '細目・段階・状態を指定してください'}), 400
    now, uid = _now_uid()
    with _fdb() as (cur, conn):
        owner, found = _owner_of(cur, k)
        if not found:
            return jsonify({'success': False, 'error': '細目が見つかりません'}), 404
        if not _is_owner_member(owner) and not can_upload():
            return jsonify({'success': False, 'error': 'この細目の担当グループに属していません'}), 403
        if not is_active(k['fiscal_year'], stage):
            return jsonify({'success': False, 'error': 'この年度・段階は，いま執筆の受付をしていません'}), 400
        cur.execute('SELECT editor_status FROM %s WHERE fiscal_year=%%s AND mid_plan_no=%%s AND annual_plan_no=%%s '
                    'AND detail_no=%%s AND stage=%%s' % REQ_TABLE, tuple(k[c] for c in KEY) + (stage,))
        q = cur.fetchone() or {}
        _upsert_request(cur, k, stage, {'author_status': st, 'author_updated_at': now, 'author_updated_by': uid})
        conn.commit()
    return jsonify({'success': True})


# -----------------------------------------------
# 3. 細目の詳細（両ダッシュボード共通）
# -----------------------------------------------
@mid_term_progress_bp.route('/api/item')
@login_required
def api_item():
    """細目に関係する情報：昨年度の計画・報告，今年度の計画・進捗・報告と評価室からのコメント，状態。"""
    k = _key_from(request.args)
    if not k:
        return jsonify({'success': False, 'error': '細目を指定してください'}), 400
    y = k['fiscal_year']
    with _fdb() as (cur, conn):
        cur.execute('SELECT * FROM T_ann_plan_details WHERE fiscal_year=%s AND mid_plan_no=%s AND annual_plan_no=%s '
                    'AND detail_no=%s', tuple(k[c] for c in KEY))
        d = cur.fetchone()
        if not d:
            return jsonify({'success': False, 'error': '細目が見つかりません'}), 404
        owner = (d.get('owner_group') or '').strip()
        editor_ok = can_upload()
        if not editor_ok and not _is_owner_member(owner):
            abort(403)
        # 昨年度：同じ中期計画番号・年度計画番号の細目．担当が同じもの→同じ細目番号→全部の順で選ぶ
        cur.execute('SELECT * FROM T_ann_plan_details WHERE fiscal_year=%s AND mid_plan_no=%s AND annual_plan_no=%s '
                    'ORDER BY detail_no', (y - 1, k['mid_plan_no'], k['annual_plan_no']))
        prev_all = cur.fetchall()
        prev = [p for p in prev_all if owner and (p.get('owner_group') or '').strip() == owner] \
            or [p for p in prev_all if p['detail_no'] == k['detail_no']] or prev_all
        prev_how = ('同じ担当' if prev and owner and (prev[0].get('owner_group') or '').strip() == owner
                    else ('同じ細目番号' if len(prev) == 1 and prev[0]['detail_no'] == k['detail_no'] else '年度計画全体'))
        reqs = {}
        if _req_table_ready(cur):
            cur.execute('SELECT * FROM %s WHERE fiscal_year=%%s AND mid_plan_no=%%s AND annual_plan_no=%%s '
                        'AND detail_no=%%s' % REQ_TABLE, tuple(k[c] for c in KEY))
            reqs = {r['stage']: r for r in cur.fetchall()}
        cur.execute('SELECT detail_no, plan_text FROM T_ann_plan_details WHERE fiscal_year=%s AND mid_plan_no=%s '
                    'AND annual_plan_no=%s ORDER BY detail_no', (y, k['mid_plan_no'], k['annual_plan_no']))
        siblings = cur.fetchall()
        heads = _mid_heads(cur, y)
    m = heads.get(k['mid_plan_no']) or {}

    member = _is_owner_member(owner)
    # 【同上】の細目は，出どころの細目の計画を展開して見せる（計画はここでは書き換えない）
    same = is_same(d.get('plan_text'))
    same_src = resolve_same(siblings).get(k['detail_no']) if same else None
    src_text = next((x.get('plan_text') or '' for x in siblings if x['detail_no'] == same_src), '') if same_src else ''
    prev_src = resolve_same(prev_all)
    prev_text = {x['detail_no']: x.get('plan_text') or '' for x in prev_all}

    def sect(stage):
        q = reqs.get(stage) or {}
        requested = q.get('editor_status') == 'requested'
        report = stage in REPORT_STAGES
        return {'stage': stage, 'label': STAGE_LABEL[stage], 'report': report,
                'text': d.get(TEXT_COL[stage]) or '',
                'text_html': md_to_html(d.get(TEXT_COL[stage]) or ''),
                # 編集者：正本（承認版）を書く．計画段階は担当グループの人も正本を書く（これまでどおり）
                'can_edit': editor_ok or (not report and member and requested),
                # 執筆者：進捗報告・業務実績報告の提出を書く
                'can_submit': report and member and requested,
                'author_text': q.get('author_text') or '',
                'author_text_html': md_to_html(q.get('author_text') or ''),
                'author_self_eval': q.get('author_self_eval'),
                'author_text_at': _s(q.get('author_text_at')),
                'comment': q.get('comment') or '', 'comment_html': md_to_html(q.get('comment') or ''),
                'editor_status': q.get('editor_status'), 'author_status': q.get('author_status'),
                'labels': _status_label(q),
                'requested_at': _s(q.get('requested_at')), 'author_updated_at': _s(q.get('author_updated_at')),
                'comment_updated_at': _s(q.get('comment_updated_at'))}

    done = is_done(d.get('plan_text'))
    pl = sect('plan')
    if same:
        pl.update({'same': True, 'same_src': same_src, 'text_html': md_to_html(src_text), 'can_edit': False})
    res = sect('result')
    res['self_eval'] = d.get('self_eval')
    stages = [pl, sect('progress'), res]
    if done:                                   # 対応済みの細目は書き込みの対象にしない
        for x in stages:
            x['can_edit'] = False
            x['can_submit'] = False
    return jsonify({
        'success': True, 'editor': editor_ok, 'member': member,
        'row_updated_at': _s(d.get('updated_at')),
        'key': k, 'owner_group': owner,
        'goal': _goal_code(m) if m else '', 'goal_title': _goal_title(m) if m else '',
        'mid_plan_html': md_to_html(m.get('mid_plan_text') or '') if m else '',
        'prev': {'year': y - 1, 'how': prev_how if prev else '',
                 'rows': [{'detail_no': p['detail_no'], 'owner_group': (p.get('owner_group') or '').strip(),
                           'same_src': prev_src.get(p['detail_no']) if is_same(p.get('plan_text')) else None,
                           'plan_html': md_to_html(prev_text.get(prev_src.get(p['detail_no'])) or '')
                                        if is_same(p.get('plan_text')) else md_to_html(p.get('plan_text') or ''),
                           'result_html': md_to_html(p.get('result_text') or '')} for p in prev]},
        'done': done,
        'stages': stages,
    })


# -----------------------------------------------
# 4. 本文の書き込み（正本＝T_ann_plan_details へ直接）
# -----------------------------------------------
def _write_submission(cur, conn, k, stage, b, owner, member, now, uid, editor_ok=False):
    """執筆者の提出（T_ann_plan_requests.author_text）を書く。
    執筆者に開いている依頼（年度×段階）についてだけ．執筆者は担当グループの細目だけ，編集者は全細目。"""
    if not _author_cols_ready(cur):
        return jsonify({'success': False, 'error': '提出を置く列がまだありません（管理者にお知らせください）'}), 400
    if not is_active(k['fiscal_year'], stage):
        return jsonify({'success': False, 'error': 'この年度・段階は，いま執筆の受付をしていません'}), 400
    if not member and not editor_ok:
        return jsonify({'success': False, 'error': 'この細目の担当グループに属していません'}), 403
    cur.execute('SELECT plan_text FROM T_ann_plan_details WHERE fiscal_year=%s AND mid_plan_no=%s '
                'AND annual_plan_no=%s AND detail_no=%s', tuple(k[c] for c in KEY))
    if is_done((cur.fetchone() or {}).get('plan_text')):
        return jsonify({'success': False, 'error': 'この細目は「対応済み」です．報告は不要です'}), 400
    cur.execute('SELECT editor_status, author_status FROM %s WHERE fiscal_year=%%s AND mid_plan_no=%%s '
                'AND annual_plan_no=%%s AND detail_no=%%s AND stage=%%s' % REQ_TABLE,
                tuple(k[c] for c in KEY) + (stage,))
    q = cur.fetchone() or {}
    text = (b.get('text') or '').replace('\r\n', '\n').strip() or None
    f = {'author_text': text, 'author_text_at': now, 'author_text_by': uid}
    if stage == 'result' and 'self_eval' in b:
        se = b.get('self_eval')
        se = None if se in (None, '') else safe_int(se)
        if se is not None and not 1 <= se <= 4:
            return jsonify({'success': False, 'error': '自己評価は1〜4で入れてください'}), 400
        f['author_self_eval'] = se
    if stage == 'result' and _extra_cols_ready(cur):
        for key, col in (('self_eval_note', 'author_self_eval_note'), ('evidence', 'author_evidence')):
            if key in b:
                f[col] = (b.get(key) or '').replace('\r\n', '\n').strip() or None
    if not q.get('author_status'):
        f.update({'author_status': 'writing', 'author_updated_at': now, 'author_updated_by': uid})
    _upsert_request(cur, k, stage, f)
    conn.commit()
    return jsonify({'success': True, 'updated_at': _s(now), 'target': 'author'})


@mid_term_progress_bp.route('/api/item/text', methods=['POST'])
@login_required
def api_item_text():
    """段階の本文（業務実績報告は自己評価も）を T_ann_plan_details に書く。
    書けるのは編集者，または依頼中の段階について担当グループの人。
    base_updated_at（読み込んだときの行の updated_at）と食い違えば書かずに 409 を返す。"""
    b = _jbody()
    k, stage = _key_from(b), _stage_from(b)
    if not k or not stage:
        return jsonify({'success': False, 'error': '細目と段階を指定してください'}), 400
    text = (b.get('text') or '').replace('\r\n', '\n').strip() or None
    fields = {TEXT_COL[stage]: text}
    if stage == 'result' and 'self_eval' in b:
        se = b.get('self_eval')
        if se in (None, ''):
            fields['self_eval'] = None
        else:
            se = safe_int(se)
            if se is None or not 1 <= se <= 4:
                return jsonify({'success': False, 'error': '自己評価は1〜4で入れてください'}), 400
            fields['self_eval'] = se
    base = b.get('base_updated_at') or None
    now, uid = _now_uid()
    with _fdb() as (cur, conn):
        owner, found = _owner_of(cur, k)
        if not found:
            return jsonify({'success': False, 'error': '細目が見つかりません'}), 404
        editor_ok = can_upload()
        member = _is_owner_member(owner)
        # 執筆者（と執筆者ダッシュボードの編集者）は「提出」を書く．正本は編集者が編集者の画面で書く
        if b.get('target') == 'author' or not editor_ok:
            return _write_submission(cur, conn, k, stage, b, owner, member, now, uid, editor_ok)
        q = None
        if _req_table_ready(cur):
            cur.execute('SELECT editor_status, author_status FROM %s WHERE fiscal_year=%%s AND mid_plan_no=%%s '
                        'AND annual_plan_no=%%s AND detail_no=%%s AND stage=%%s' % REQ_TABLE,
                        tuple(k[c] for c in KEY) + (stage,))
            q = cur.fetchone()
        if not editor_ok:
            if not member:
                return jsonify({'success': False, 'error': 'この細目の担当グループに属していません'}), 403
            if not q or q.get('editor_status') != 'requested':
                return jsonify({'success': False, 'error': '依頼中の段階だけ書き込めます'}), 400
        cur.execute('SELECT plan_text FROM T_ann_plan_details WHERE fiscal_year=%s AND mid_plan_no=%s '
                    'AND annual_plan_no=%s AND detail_no=%s', tuple(k[c] for c in KEY))
        cp = cur.fetchone() or {}
        if is_done(cp.get('plan_text')):
            return jsonify({'success': False,
                            'error': 'この細目は「対応済み」です．報告は不要です（編集者ダッシュボードで対応済みを外すと書けます）'}), 400
        if stage == 'plan':
            if is_same(cp.get('plan_text')):
                return jsonify({'success': False,
                                'error': 'この細目は「同上」です．計画は上の細目で書きます（同上を外すと自分の計画を書けます）'}), 400
        sets = ', '.join('%s = %%s' % c for c in fields)
        cur.execute('UPDATE T_ann_plan_details SET %s, updated_at = %%s, updated_by = %%s '
                    'WHERE fiscal_year=%%s AND mid_plan_no=%%s AND annual_plan_no=%%s AND detail_no=%%s '
                    'AND updated_at <=> %%s' % sets,
                    tuple(fields.values()) + (now, uid) + tuple(k[c] for c in KEY) + (base,))
        if cur.rowcount == 0:
            conn.rollback()
            return jsonify({'success': False, 'conflict': True,
                            'error': '読み込んだ後に，ほかの人がこの細目を更新しました．開き直してから書いてください'}), 409
        # 担当グループの人が書いたら，執筆者側の状態が空なら「執筆中」にする
        if member and q and q.get('editor_status') == 'requested' and not q.get('author_status'):
            _upsert_request(cur, k, stage, {'author_status': 'writing', 'author_updated_at': now,
                                            'author_updated_by': uid})
        conn.commit()
    return jsonify({'success': True, 'updated_at': _s(now)})
