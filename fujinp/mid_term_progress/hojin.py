"""
mid_term_progress - 法人評価（評価委員会とのやり取り）と，計画番号ごとの自己評価のとりまとめ

法人評価：市の評価委員会から届いた質問・意見に対して，大学内で連絡を取りながら回答をとりまとめる。
  質問（T_ann_eval_questions）：年度・質問番号・種別（質問／意見）・関係計画番号（複数）・本文・
    責任部門（ユーザグループ．複数）・回答（とりまとめ）・状態
  書き込み（T_ann_eval_posts）：質問ごとの連絡の窓（Slack風の時系列）
  作業は編集担当（中期計画進捗管理者・admin）が中心に行い，責任部門のユーザグループの人は
  自分の部門が関わる質問を見て書き込める。
計画番号ごと（常設表 T_ann_plan_evaluations）：
  自己評価（とりまとめた点・理由・状態）は業務実績報告の画面で，
  評価委員会からの評点とコメントは法人評価の画面で記録する。
"""
import json
from flask import render_template, request, jsonify, session, abort, redirect, url_for

from . import mid_term_progress_bp
from .routes import (login_required, require_upload_perm, can_upload, _fdb, _s, safe_int, get_jst_now,
                     md_to_html, _goal_code, _goal_title, _group_names, _load_columns, is_same, is_done, resolve_same)
from .workflow import REQ_TABLE, _req_table_ready, _my_groups, _jbody

Q_TABLE, P_TABLE, R_TABLE = 'T_ann_eval_questions', 'T_ann_eval_posts', 'T_ann_plan_evaluations'
Q_STATUS = {'received': '受付', 'discussing': '検討中', 'drafting': '回答案作成', 'fixed': '確定'}
SELF_STATUS = {'drafting': 'とりまとめ中', 'approved': '承認済み'}


def _ready(cur, table):
    try:
        cur.execute('SELECT 1 FROM %s LIMIT 0' % table)
        cur.fetchall()
        return True
    except Exception:
        return False


def _uname():
    for k in ('display_name', 'name', 'full_name', 'username', 'user_name', 'email'):
        v = session.get(k)
        if v:
            return str(v)[:100]
    return 'user%s' % session.get('user_id')


def _plan_list(text):
    out = []
    for t in str(text or '').replace('，', ',').replace('、', ',').replace(' ', ',').split(','):
        n = safe_int(t.strip())
        if n and n not in out:
            out.append(n)
    return out


def _q_out(q, posts=0):
    groups = []
    try:
        groups = json.loads(q.get('owner_groups') or '[]')
    except Exception:
        groups = []
    return {'id': q['id'], 'fiscal_year': q['fiscal_year'], 'q_no': q['q_no'], 'kind': q.get('kind') or '質問',
            'plan_nos': _plan_list(q.get('plan_nos')), 'question': q.get('question') or '',
            'question_html': md_to_html(q.get('question') or ''), 'groups': groups,
            'answer': q.get('answer') or '', 'answer_html': md_to_html(q.get('answer') or ''),
            'status': q.get('status') or 'received', 'status_label': Q_STATUS.get(q.get('status') or 'received', ''),
            'updated_at': _s(q.get('updated_at')), 'posts': posts}


def _can_see(q):
    if can_upload():
        return True
    return bool(set(q['groups']) & set(_my_groups()))


def _default_groups(cur, year, plans):
    if not plans:
        return []
    cur.execute('SELECT DISTINCT owner_group, plan_text FROM T_ann_plan_details WHERE fiscal_year = %%s '
                'AND mid_plan_no IN (%s)' % ','.join(['%s'] * len(plans)), (year,) + tuple(plans))
    return sorted({(r.get('owner_group') or '').strip() for r in cur.fetchall()
                   if (r.get('owner_group') or '').strip() and not is_done(r.get('plan_text'))})


# -----------------------------------------------
# 画面
# -----------------------------------------------
@mid_term_progress_bp.route('/hojin')
@login_required
def hojin():
    """法人評価．編集担当は全件を扱い，それ以外の人は自分のグループが責任部門の質問を見て書き込む。"""
    with _fdb() as (cur, conn):
        ready = _ready(cur, Q_TABLE) and _ready(cur, P_TABLE) and _ready(cur, R_TABLE)
        years = []
        if ready:
            cur.execute('SELECT DISTINCT fiscal_year FROM %s ORDER BY fiscal_year' % Q_TABLE)
            years = [r['fiscal_year'] for r in cur.fetchall()]
        cur.execute('SELECT DISTINCT fiscal_year FROM T_ann_plan_details ORDER BY fiscal_year')
        all_years = [r['fiscal_year'] for r in cur.fetchall()]
    editor = can_upload()
    year = safe_int(request.args.get('year')) or ((years or all_years or [None])[-1])
    return render_template('mid_term_progress/hojin.html', year=year, years=sorted(set(years + (all_years if editor else []))),
                           editor=editor, ready=ready, q_status=Q_STATUS,
                           groups=((_load_columns() or _group_names()) if editor else []), my_groups=_my_groups())


# -----------------------------------------------
# 質問
# -----------------------------------------------
@mid_term_progress_bp.route('/api/hojin/list')
@login_required
def api_hojin_list():
    year = safe_int(request.args.get('year'))
    if not year:
        return jsonify({'success': False, 'error': '年度を指定してください'}), 400
    with _fdb() as (cur, conn):
        cur.execute('SELECT q.*, (SELECT COUNT(*) FROM %s p WHERE p.question_id = q.id) AS n_posts FROM %s q '
                    'WHERE q.fiscal_year = %%s ORDER BY q.q_no' % (P_TABLE, Q_TABLE), (year,))
        qs = [_q_out(q, q.get('n_posts') or 0) for q in cur.fetchall()]
    qs = [q for q in qs if _can_see(q)]
    return jsonify({'success': True, 'questions': qs, 'editor': can_upload(), 'my_groups': _my_groups()})


def _plan_refs(cur, year, plans):
    """関係計画番号ごとの参照：中期計画・細目の計画・業務実績（執筆者の報告を優先）・自己評価。"""
    if not plans:
        return []
    ph = ','.join(['%s'] * len(plans))
    cur.execute('SELECT * FROM T_ann_plan_details WHERE fiscal_year = %%s AND mid_plan_no IN (%s) '
                'ORDER BY mid_plan_no, annual_plan_no, detail_no' % ph, (year,) + tuple(plans))
    rows = cur.fetchall()
    reqs = {}
    if _req_table_ready(cur):
        cur.execute("SELECT * FROM %s WHERE fiscal_year = %%s AND stage = 'result' AND mid_plan_no IN (%s)"
                    % (REQ_TABLE, ph), (year,) + tuple(plans))
        reqs = {(q['mid_plan_no'], q['annual_plan_no'], q['detail_no']): q for q in cur.fetchall()}
    rat = {}
    if _ready(cur, R_TABLE):
        cur.execute('SELECT * FROM %s WHERE fiscal_year = %%s AND mid_plan_no IN (%s)' % (R_TABLE, ph),
                    (year,) + tuple(plans))
        rat = {(r['mid_plan_no'], r['annual_plan_no']): r for r in cur.fetchall()}
    cur.execute("""SELECT mid_plan_no, mid_goal_no, goal_chapter, goal_section, goal_item, goal_item_title,
                          goal_section_title, goal_chapter_title, mid_plan_text FROM T_mid_plan_details
                   WHERE detail_no = 1 AND %%s BETWEEN COALESCE(valid_from, 0) AND COALESCE(valid_to, 9999)
                     AND mid_plan_no IN (%s) ORDER BY mid_plan_no, sub_term DESC""" % ph, (year,) + tuple(plans))
    heads = {}
    for m in cur.fetchall():
        heads.setdefault(m['mid_plan_no'], m)
    groups = {}
    for r in rows:
        groups.setdefault((r['mid_plan_no'], r['annual_plan_no']), []).append(r)
    out = []
    for p in plans:
        m = heads.get(p) or {}
        for (mp, ap), rs in sorted(groups.items()):
            if mp != p:
                continue
            src = resolve_same(rs)
            text_of = {r['detail_no']: r.get('plan_text') or '' for r in rs}
            ds = []
            for r in rs:
                q = reqs.get((mp, ap, r['detail_no'])) or {}
                res = (q.get('author_text') or '').strip() or (r.get('result_text') or '')
                plan = text_of.get(src.get(r['detail_no'])) if is_same(r.get('plan_text')) else r.get('plan_text')
                ds.append({'detail_no': r['detail_no'], 'owner_group': (r.get('owner_group') or '').strip(),
                           'done': is_done(r.get('plan_text')), 'same': is_same(r.get('plan_text')),
                           'plan_html': md_to_html(plan or ''), 'result_html': md_to_html(res),
                           'author_self_eval': q.get('author_self_eval'),
                           'self_eval_note_html': md_to_html(q.get('author_self_eval_note') or ''),
                           'evidence_html': md_to_html(q.get('author_evidence') or '')})
            rt = rat.get((mp, ap)) or {}
            out.append({'mid_plan_no': mp, 'annual_plan_no': ap,
                        'goal': _goal_code(m) if m else '', 'goal_title': _goal_title(m) if m else '',
                        'mid_plan_html': md_to_html(m.get('mid_plan_text') or '') if m else '',
                        'self_eval': rt.get('self_eval'), 'self_eval_reason_html': md_to_html(rt.get('self_eval_reason') or ''),
                        'committee_score': rt.get('committee_eval') if rt.get('committee_eval') is not None else '',
                        'committee_comment_html': md_to_html(rt.get('committee_comment') or ''),
                        'details': ds})
    return out


@mid_term_progress_bp.route('/api/hojin/q/<int:qid>')
@login_required
def api_hojin_q(qid):
    with _fdb() as (cur, conn):
        cur.execute('SELECT * FROM %s WHERE id = %%s' % Q_TABLE, (qid,))
        q = cur.fetchone()
        if not q:
            return jsonify({'success': False, 'error': '質問が見つかりません'}), 404
        q = _q_out(q)
        if not _can_see(q):
            abort(403)
        cur.execute('SELECT * FROM %s WHERE question_id = %%s ORDER BY created_at, id' % P_TABLE, (qid,))
        me = session.get('user_id')
        posts = [{'id': p['id'], 'user_name': p.get('user_name') or '', 'body_html': md_to_html(p.get('body') or ''),
                  'created_at': _s(p.get('created_at')), 'mine': p.get('user_id') == me} for p in cur.fetchall()]
        refs = _plan_refs(cur, q['fiscal_year'], q['plan_nos'])
    return jsonify({'success': True, 'question': q, 'posts': posts, 'refs': refs, 'editor': can_upload()})


@mid_term_progress_bp.route('/api/hojin/all')
@login_required
def api_hojin_all():
    """年度の質問を全部，書き込みと関係計画番号の参照つきで返す（スクロール式の画面用）。"""
    year = safe_int(request.args.get('year'))
    if not year:
        return jsonify({'success': False, 'error': '年度を指定してください'}), 400
    me = session.get('user_id')
    with _fdb() as (cur, conn):
        cur.execute('SELECT * FROM %s WHERE fiscal_year = %%s ORDER BY q_no' % Q_TABLE, (year,))
        qs = [q for q in (_q_out(r) for r in cur.fetchall()) if _can_see(q)]
        posts = {}
        if qs:
            ids = [q['id'] for q in qs]
            cur.execute('SELECT * FROM %s WHERE question_id IN (%s) ORDER BY created_at, id'
                        % (P_TABLE, ','.join(['%s'] * len(ids))), tuple(ids))
            for p in cur.fetchall():
                posts.setdefault(p['question_id'], []).append(
                    {'id': p['id'], 'user_name': p.get('user_name') or '', 'body_html': md_to_html(p.get('body') or ''),
                     'created_at': _s(p.get('created_at')), 'mine': p.get('user_id') == me})
        plans = sorted({p for q in qs for p in q['plan_nos']})
        refs = {}
        for r in _plan_refs(cur, year, plans):
            refs.setdefault(r['mid_plan_no'], []).append(r)
    for q in qs:
        q['posts'] = len(posts.get(q['id'], []))
    return jsonify({'success': True, 'editor': can_upload(),
                    'items': [{'question': q, 'posts': posts.get(q['id'], []),
                               'refs': [r for p in q['plan_nos'] for r in refs.get(p, [])]} for q in qs]})


@mid_term_progress_bp.route('/api/hojin/q/save', methods=['POST'])
@login_required
@require_upload_perm
def api_hojin_q_save():
    b = _jbody()
    year, q_no = safe_int(b.get('fiscal_year')), safe_int(b.get('q_no'))
    if not year or not q_no:
        return jsonify({'success': False, 'error': '年度と質問番号を入れてください'}), 400
    plans = _plan_list(b.get('plan_nos'))
    status = b.get('status') if b.get('status') in Q_STATUS else 'received'
    now, uid = get_jst_now(), session.get('user_id')
    with _fdb() as (cur, conn):
        groups = b.get('groups')
        if groups is None:
            groups = _default_groups(cur, year, plans)
        vals = (year, q_no, (b.get('kind') or '質問')[:10], ','.join(str(p) for p in plans),
                (b.get('question') or '').strip() or None, json.dumps([g for g in groups if g], ensure_ascii=False),
                (b.get('answer') or '').replace('\r\n', '\n').strip() or None, status, now, uid)
        qid = safe_int(b.get('id'))
        try:
            if qid:
                cur.execute('UPDATE %s SET fiscal_year=%%s, q_no=%%s, kind=%%s, plan_nos=%%s, question=%%s, owner_groups=%%s, '
                            'answer=%%s, status=%%s, updated_at=%%s, updated_by=%%s WHERE id=%%s' % Q_TABLE, vals + (qid,))
            else:
                cur.execute('INSERT INTO %s (fiscal_year, q_no, kind, plan_nos, question, owner_groups, answer, status, '
                            'updated_at, updated_by, created_at) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)'
                            % Q_TABLE, vals + (now,))
                qid = cur.lastrowid
            conn.commit()
        except Exception as e:
            if 'Duplicate' in str(e):
                return jsonify({'success': False, 'error': '同じ年度にその質問番号がすでにあります'}), 400
            raise
    return jsonify({'success': True, 'id': qid})


@mid_term_progress_bp.route('/api/hojin/q/delete', methods=['POST'])
@login_required
@require_upload_perm
def api_hojin_q_delete():
    qid = safe_int(_jbody().get('id'))
    with _fdb() as (cur, conn):
        cur.execute('DELETE FROM %s WHERE question_id = %%s' % P_TABLE, (qid,))
        cur.execute('DELETE FROM %s WHERE id = %%s' % Q_TABLE, (qid,))
        conn.commit()
    return jsonify({'success': True})


@mid_term_progress_bp.route('/api/hojin/q/import', methods=['POST'])
@login_required
@require_upload_perm
def api_hojin_q_import():
    """貼り付けたタブ区切り（Excel からのコピー）を取り込む：1列目＝質問番号，2列目＝計画番号（複数可），
    3列目＝質問・意見の本文．4列目があれば種別（質問／意見）。同じ質問番号は本文などを上書きする。"""
    b = _jbody()
    year = safe_int(b.get('fiscal_year'))
    if not year:
        return jsonify({'success': False, 'error': '年度を指定してください'}), 400
    lines = [l for l in (b.get('text') or '').replace('\r\n', '\n').split('\n') if l.strip()]
    now, uid = get_jst_now(), session.get('user_id')
    n, skipped = 0, []
    with _fdb() as (cur, conn):
        for i, l in enumerate(lines, 1):
            cols = [c.strip().strip('"') for c in l.split('\t')]
            q_no = safe_int(cols[0]) if cols else None
            if not q_no or len(cols) < 3:
                skipped.append(i)
                continue
            plans = _plan_list(cols[1])
            kind = (cols[3] if len(cols) > 3 and cols[3] else '質問')[:10]
            groups = json.dumps(_default_groups(cur, year, plans), ensure_ascii=False)
            cur.execute('INSERT INTO %s (fiscal_year, q_no, kind, plan_nos, question, owner_groups, status, created_at, '
                        'updated_at, updated_by) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s) '
                        'ON DUPLICATE KEY UPDATE kind=VALUES(kind), plan_nos=VALUES(plan_nos), question=VALUES(question), '
                        'updated_at=VALUES(updated_at), updated_by=VALUES(updated_by)' % Q_TABLE,
                        (year, q_no, kind, ','.join(str(p) for p in plans), cols[2], groups, 'received', now, now, uid))
            n += 1
        conn.commit()
    return jsonify({'success': True, 'imported': n, 'skipped': skipped})


# -----------------------------------------------
# 連絡の窓（書き込み）
# -----------------------------------------------
@mid_term_progress_bp.route('/api/hojin/post', methods=['POST'])
@login_required
def api_hojin_post():
    b = _jbody()
    qid = safe_int(b.get('question_id'))
    body = (b.get('body') or '').replace('\r\n', '\n').strip()
    if not qid or not body:
        return jsonify({'success': False, 'error': '書き込みが空です'}), 400
    with _fdb() as (cur, conn):
        cur.execute('SELECT * FROM %s WHERE id = %%s' % Q_TABLE, (qid,))
        q = cur.fetchone()
        if not q:
            return jsonify({'success': False, 'error': '質問が見つかりません'}), 404
        if not _can_see(_q_out(q)):
            return jsonify({'success': False, 'error': 'この質問の責任部門に属していません'}), 403
        cur.execute('INSERT INTO %s (question_id, user_id, user_name, body, created_at) VALUES (%%s,%%s,%%s,%%s,%%s)'
                    % P_TABLE, (qid, session.get('user_id'), _uname(), body, get_jst_now()))
        conn.commit()
    return jsonify({'success': True})


@mid_term_progress_bp.route('/api/hojin/post/delete', methods=['POST'])
@login_required
def api_hojin_post_delete():
    pid = safe_int(_jbody().get('id'))
    with _fdb() as (cur, conn):
        cur.execute('SELECT user_id FROM %s WHERE id = %%s' % P_TABLE, (pid,))
        p = cur.fetchone()
        if not p:
            return jsonify({'success': False, 'error': '書き込みが見つかりません'}), 404
        if p.get('user_id') != session.get('user_id') and not can_upload():
            return jsonify({'success': False, 'error': '自分の書き込みだけ消せます'}), 403
        cur.execute('DELETE FROM %s WHERE id = %%s' % P_TABLE, (pid,))
        conn.commit()
    return jsonify({'success': True})


# -----------------------------------------------
# 計画番号ごと：自己評価のとりまとめ・評価委員会の評点
# -----------------------------------------------
def _status_col_ready(cur):
    try:
        cur.execute("SHOW COLUMNS FROM %s LIKE 'self_eval_status'" % R_TABLE)
        return bool(cur.fetchall())
    except Exception:
        return False


def _upsert_rating(cur, year, mp, ap, fields):
    now, uid = get_jst_now(), session.get('user_id')
    cols = list(fields.keys())
    cur.execute('INSERT INTO %s (fiscal_year, mid_plan_no, annual_plan_no, %s, created_at, updated_at, updated_by) '
                'VALUES (%%s,%%s,%%s,%s,%%s,%%s,%%s) ON DUPLICATE KEY UPDATE %s, updated_at=VALUES(updated_at), '
                'updated_by=VALUES(updated_by)'
                % (R_TABLE, ', '.join(cols), ','.join(['%s'] * len(cols)), ', '.join('%s=VALUES(%s)' % (c, c) for c in cols)),
                (year, mp, ap) + tuple(fields.values()) + (now, now, uid))


@mid_term_progress_bp.route('/api/ratings')
@login_required
@require_upload_perm
def api_ratings():
    """年度の計画番号ごとの評価（自己評価・評価委員会の評点）．業務実績報告と法人評価の画面で使う。"""
    year = safe_int(request.args.get('year'))
    with _fdb() as (cur, conn):
        if not _ready(cur, R_TABLE):
            return jsonify({'success': True, 'ready': False, 'ratings': []})
        cur.execute('SELECT * FROM %s WHERE fiscal_year = %%s' % R_TABLE, (year,))
        rows = cur.fetchall()
        cur.execute("""SELECT d.mid_plan_no, d.annual_plan_no, MIN(d.detail_no) AS d1 FROM T_ann_plan_details d
                       WHERE d.fiscal_year = %s GROUP BY d.mid_plan_no, d.annual_plan_no ORDER BY d.mid_plan_no""", (year,))
        plans = cur.fetchall()
        cur.execute("""SELECT mid_plan_no, goal_chapter, goal_section, goal_item, goal_item_title, goal_section_title,
                              goal_chapter_title FROM T_mid_plan_details WHERE detail_no = 1
                         AND %s BETWEEN COALESCE(valid_from, 0) AND COALESCE(valid_to, 9999) ORDER BY mid_plan_no, sub_term DESC""", (year,))
        heads = {}
        for m in cur.fetchall():
            heads.setdefault(m['mid_plan_no'], m)
    rt = {(r['mid_plan_no'], r['annual_plan_no']): r for r in rows}
    out = []
    for p in plans:
        r = rt.get((p['mid_plan_no'], p['annual_plan_no'])) or {}
        m = heads.get(p['mid_plan_no']) or {}
        out.append({'mid_plan_no': p['mid_plan_no'], 'annual_plan_no': p['annual_plan_no'],
                    'goal': _goal_code(m) if m else '', 'goal_title': _goal_title(m) if m else '',
                    'self_eval': r.get('self_eval'), 'self_eval_reason': r.get('self_eval_reason') or '',
                    'self_eval_reason_html': md_to_html(r.get('self_eval_reason') or ''),
                    'self_eval_status': r.get('self_eval_status') or 'drafting',
                    'committee_score': r.get('committee_eval') if r.get('committee_eval') is not None else '',
                    'committee_comment': r.get('committee_comment') or '',
                    'committee_comment_html': md_to_html(r.get('committee_comment') or ''),
                    'updated_at': _s(r.get('updated_at'))})
    return jsonify({'success': True, 'ready': True, 'ratings': out, 'self_status': SELF_STATUS})


@mid_term_progress_bp.route('/api/ratings/self', methods=['POST'])
@login_required
@require_upload_perm
def api_ratings_self():
    b = _jbody()
    year, mp, ap = safe_int(b.get('fiscal_year')), safe_int(b.get('mid_plan_no')), safe_int(b.get('annual_plan_no'))
    if not year or not mp or not ap:
        return jsonify({'success': False, 'error': '年度と計画番号を指定してください'}), 400
    se = b.get('self_eval')
    se = None if se in (None, '') else safe_int(se)
    if se is not None and not 1 <= se <= 4:
        return jsonify({'success': False, 'error': '自己評価は1〜4で入れてください'}), 400
    st = b.get('self_eval_status') if b.get('self_eval_status') in SELF_STATUS else 'drafting'
    with _fdb() as (cur, conn):
        f = {'self_eval': se, 'self_eval_reason': (b.get('self_eval_reason') or '').replace('\r\n', '\n').strip() or None}
        if _status_col_ready(cur):
            f['self_eval_status'] = st
        _upsert_rating(cur, year, mp, ap, f)
        conn.commit()
    return jsonify({'success': True})


@mid_term_progress_bp.route('/api/ratings/committee', methods=['POST'])
@login_required
@require_upload_perm
def api_ratings_committee():
    b = _jbody()
    year, mp, ap = safe_int(b.get('fiscal_year')), safe_int(b.get('mid_plan_no')), safe_int(b.get('annual_plan_no'))
    if not year or not mp or not ap:
        return jsonify({'success': False, 'error': '年度と計画番号を指定してください'}), 400
    ce = b.get('committee_score')
    ce = None if ce in (None, '') else safe_int(ce)
    if ce is not None and not 1 <= ce <= 4:
        return jsonify({'success': False, 'error': '評価委員会の評点は1〜4で入れてください'}), 400
    with _fdb() as (cur, conn):
        _upsert_rating(cur, year, mp, ap, {'committee_eval': ce,
                                           'committee_comment': (b.get('committee_comment') or '').replace('\r\n', '\n').strip() or None})
        conn.commit()
    return jsonify({'success': True})
