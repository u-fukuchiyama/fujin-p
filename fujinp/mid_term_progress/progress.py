"""
mid_term_progress - 編集者ダッシュボード（入口）と，年度計画進捗状況報告の画面

入口（/editor）で年度を選び，目的ごとの画面へ進む（その先は年度を固定する）。
  年度計画策定        → /editor/structure?year=Y（structure.py）
  年度計画進捗状況報告 → /editor/progress?year=Y（ここ）

進捗状況報告の画面は，計画番号ごとに
  中期計画 → 年度計画（【同上】はまとめる） → 細目ごとに
    執筆者からの報告（提出：T_ann_plan_requests.author_text．無ければ状態）
    評価室からのコメント（T_ann_plan_requests.comment）
    評価室がまとめた／承認した進捗状況報告（正本：T_ann_plan_details.progress_text）
を並べる。
"""
from flask import render_template, request, jsonify, redirect, url_for

from . import mid_term_progress_bp
from .routes import (login_required, require_upload_perm, _fdb, _s, safe_int, md_to_html,
                     _goal_code, _goal_title, is_same, resolve_same, is_done)
from .workflow import REQ_TABLE, _req_table_ready, _author_cols_ready, STAGE_LABEL, load_active_sets


def _years(cur):
    cur.execute('SELECT DISTINCT fiscal_year FROM T_ann_plan_details ORDER BY fiscal_year')
    return [r['fiscal_year'] for r in cur.fetchall()]


def _state(q):
    """依頼の記録から，一覧の状態を1つ決める。"""
    q = q or {}
    if q.get('editor_status') == 'confirmed':
        return 'confirmed'
    a = q.get('author_status')
    if a in ('done', 'revised'):
        return 'submitted'
    if a == 'writing' or (q.get('author_text') or '').strip():
        return 'writing'
    if q.get('editor_status') == 'requested':
        return 'requested'
    return 'none'


STATE_LABEL = {'none': '未依頼', 'requested': '依頼中（未入力）', 'writing': '執筆中',
               'submitted': '提出済み（確認待ち）', 'confirmed': '確認済み'}


def fallback_badge(q, what):
    """正本が空のときに代わりに出す執筆者の本文につけるバッジ（例：「執筆者の提案・執筆中」）．
    内容は常に優先度のいちばん高いものを出し，低いものを出すときは状態をバッジで示す。"""
    st = _state(q)
    return '%s・%s' % (what, STATE_LABEL.get(st, '') if st not in ('none', 'requested') else '執筆中')


# -----------------------------------------------
# 入口：編集者ダッシュボード
# -----------------------------------------------
@mid_term_progress_bp.route('/editor')
@login_required
@require_upload_perm
def editor():
    with _fdb() as (cur, conn):
        years = _years(cur)
    year = safe_int(request.args.get('year')) or (years[-1] if years else None)
    return render_template('mid_term_progress/editor_home.html', years=years, year=year)


@mid_term_progress_bp.route('/api/editor/summary')
@login_required
@require_upload_perm
def api_editor_summary():
    """年度の概況：細目・担当・対応済みの数と，段階ごとの状態の件数。"""
    year = safe_int(request.args.get('year'))
    if not year:
        return jsonify({'success': False, 'error': '年度を指定してください'}), 400
    with _fdb() as (cur, conn):
        cur.execute('SELECT mid_plan_no, annual_plan_no, detail_no, owner_group, plan_text '
                    'FROM T_ann_plan_details WHERE fiscal_year = %s', (year,))
        rows = cur.fetchall()
        reqs = {}
        if _req_table_ready(cur):
            cur.execute('SELECT * FROM %s WHERE fiscal_year = %%s' % REQ_TABLE, (year,))
            for q in cur.fetchall():
                reqs[(q['mid_plan_no'], q['annual_plan_no'], q['detail_no'], q['stage'])] = q
        hj = None
        try:
            cur.execute("SELECT status, COUNT(*) AS n FROM T_ann_eval_questions WHERE fiscal_year = %s GROUP BY status", (year,))
            hj = {r['status']: r['n'] for r in cur.fetchall()}
        except Exception:
            hj = None
    live = [r for r in rows if not is_done(r.get('plan_text'))]
    assigned = [r for r in live if (r.get('owner_group') or '').strip()]
    stages = {}
    for st in STAGE_LABEL:
        c = {k: 0 for k in STATE_LABEL}
        for r in assigned:
            c[_state(reqs.get((r['mid_plan_no'], r['annual_plan_no'], r['detail_no'], st)))] += 1
        c['recorded'] = len(assigned) - c['none']
        stages[st] = c
    return jsonify({'success': True, 'year': year, 'details': len(live), 'assigned': len(assigned),
                    'unassigned': len(live) - len(assigned), 'done': len(rows) - len(live),
                    'plans': len({(r['mid_plan_no'], r['annual_plan_no']) for r in rows}),
                    'stages': stages, 'state_label': STATE_LABEL, 'active': load_active_sets(), 'hojin': hj})


# -----------------------------------------------
# 年度計画進捗状況報告
# -----------------------------------------------
@mid_term_progress_bp.route('/editor/progress')
@login_required
@require_upload_perm
def progress():
    return _report_page('progress')


@mid_term_progress_bp.route('/editor/result')
@login_required
@require_upload_perm
def result():
    return _report_page('result')


def _report_page(stage):
    year = safe_int(request.args.get('year'))
    if not year:
        return redirect(url_for('mid_term_progress.editor'))
    with _fdb() as (cur, conn):
        ready = _req_table_ready(cur) and _author_cols_ready(cur)
        cur.execute('SELECT DISTINCT owner_group FROM T_ann_plan_details WHERE fiscal_year = %s '
                    "AND owner_group IS NOT NULL AND owner_group <> '' ORDER BY owner_group", (year,))
        owners = [r['owner_group'] for r in cur.fetchall()]
    tpl = 'mid_term_progress/result.html' if stage == 'result' else 'mid_term_progress/progress.html'
    return render_template(tpl, year=year, ready=ready, owners=owners,
                           state_label=STATE_LABEL, stage=stage, stage_label=STAGE_LABEL[stage])


FINAL_COL = {'plan': 'plan_text', 'progress': 'progress_text', 'result': 'result_text'}


def build_blocks(cur, year, stage, visible=None):
    """計画番号ごとのブロック．visible（細目 → bool の関数）を渡すと，見える細目のあるブロックだけにする。
    細目ごとに，計画（【同上】は出どころの計画）・承認済みの進捗状況報告を添える。"""
    col = FINAL_COL[stage]
    cur.execute('SELECT * FROM T_ann_plan_details WHERE fiscal_year = %s '
                'ORDER BY mid_plan_no, annual_plan_no, detail_no', (year,))
    rows = cur.fetchall()
    reqs, preqs = {}, {}
    if _req_table_ready(cur):
        cur.execute('SELECT * FROM %s WHERE fiscal_year = %%s AND stage = %%s' % REQ_TABLE, (year, stage))
        reqs = {(q['mid_plan_no'], q['annual_plan_no'], q['detail_no']): q for q in cur.fetchall()}
        if stage == 'result':        # 業務実績報告では，未承認の進捗状況報告（執筆者の報告）も控えとして見せる
            cur.execute('SELECT * FROM %s WHERE fiscal_year = %%s AND stage = %%s' % REQ_TABLE, (year, 'progress'))
            preqs = {(q['mid_plan_no'], q['annual_plan_no'], q['detail_no']): q for q in cur.fetchall()}
    # 計画の正本（plan_text）が空の細目は，計画策定の段階で執筆者が出した提案を代わりに出す
    plreqs = {}
    if stage != 'plan' and _req_table_ready(cur):
        cur.execute("SELECT * FROM %s WHERE fiscal_year = %%s AND stage = 'plan' "
                    "AND author_text IS NOT NULL AND TRIM(author_text) <> ''" % REQ_TABLE, (year,))
        plreqs = {(q['mid_plan_no'], q['annual_plan_no'], q['detail_no']): q for q in cur.fetchall()}
    cur.execute("""SELECT mid_plan_no, mid_goal_no, goal_chapter, goal_section, goal_item, goal_item_title,
                          goal_section_title, goal_chapter_title, mid_plan_text
                   FROM T_mid_plan_details
                   WHERE detail_no = 1 AND %s BETWEEN COALESCE(valid_from, 0) AND COALESCE(valid_to, 9999)
                   ORDER BY mid_plan_no, sub_term DESC""", (year,))
    heads = {}
    for m in cur.fetchall():
        heads.setdefault(m['mid_plan_no'], m)
    groups = {}
    for r in rows:
        groups.setdefault((r['mid_plan_no'], r['annual_plan_no']), []).append(r)
    blocks = []
    for (mp, ap), rs in sorted(groups.items()):
        m = heads.get(mp) or {}
        src = resolve_same(rs)
        text_of, badge_of = {}, {}
        for r in rs:
            t = (r.get('plan_text') or '').strip()
            pq = plreqs.get((mp, ap, r['detail_no']))
            if not t and pq:
                t, badge_of[r['detail_no']] = pq['author_text'], fallback_badge(pq, '執筆者の提案')
            text_of[r['detail_no']] = t
        details = []
        for r in rs:
            q = reqs.get((mp, ap, r['detail_no'])) or {}
            same = is_same(r.get('plan_text'))
            d = {
                'detail_no': r['detail_no'], 'owner_group': (r.get('owner_group') or '').strip(),
                'done': is_done(r.get('plan_text')), 'same': same,
                'plan_src': src.get(r['detail_no']),
                'plan_html': md_to_html(text_of.get(src.get(r['detail_no'])) or '') if same
                             else md_to_html(text_of.get(r['detail_no']) or ''),
                'plan_badge': badge_of.get(src.get(r['detail_no'])) if same else badge_of.get(r['detail_no']),
                'state': _state(q), 'editor_status': q.get('editor_status'), 'author_status': q.get('author_status'),
                'author_text': q.get('author_text') or '', 'author_html': md_to_html(q.get('author_text') or ''),
                'author_text_at': _s(q.get('author_text_at')), 'author_self_eval': q.get('author_self_eval'),
                'self_eval_note': q.get('author_self_eval_note') or '',
                'self_eval_note_html': md_to_html(q.get('author_self_eval_note') or ''),
                'evidence': q.get('author_evidence') or '',
                'evidence_html': md_to_html(q.get('author_evidence') or ''),
                'comment': q.get('comment') or '', 'comment_html': md_to_html(q.get('comment') or ''),
                'comment_at': _s(q.get('comment_updated_at')),
                'final_text': r.get(col) or '', 'final_html': md_to_html(r.get(col) or ''),
                'self_eval': r.get('self_eval'),
                'progress_html': md_to_html(r.get('progress_text') or ''),
                'progress_author_html': md_to_html((preqs.get((mp, ap, r['detail_no'])) or {}).get('author_text') or ''),
                'updated_at': _s(r.get('updated_at')),
            }
            d['visible'] = True if visible is None else bool(visible(d))
            details.append(d)
        if visible is not None and not any(d['visible'] and not d['done'] for d in details):
            continue
        blocks.append({'mid_plan_no': mp, 'annual_plan_no': ap,
                       'goal': _goal_code(m) if m else '', 'goal_title': _goal_title(m) if m else '',
                       'mid_plan_html': md_to_html(m.get('mid_plan_text') or '') if m else '',
                       'details': details})
    return blocks


@mid_term_progress_bp.route('/api/progress')
@login_required
@require_upload_perm
def api_progress():
    year = safe_int(request.args.get('year'))
    stage = request.args.get('stage') if request.args.get('stage') in ('progress', 'result') else 'progress'
    if not year:
        return jsonify({'success': False, 'error': '年度を指定してください'}), 400
    with _fdb() as (cur, conn):
        blocks = build_blocks(cur, year, stage)
    return jsonify({'success': True, 'year': year, 'stage': stage, 'blocks': blocks})
