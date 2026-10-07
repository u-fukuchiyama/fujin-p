"""
mid_term_progress - 細目編集（年度計画策定期の細目集の組み立てと部門の割り当て）

年度計画の策定初期に，編集者が年度計画（中期計画番号×年度計画番号）ごとに細目集を組み立てる．
細目の新設・廃止・並べ替え・統合，担当部門の割り当て，計画本文の編集ができる．
参考として，同じ期の過去年度の細目（担当と計画・実績）を並べる．

その年度に進捗報告または業務実績報告の依頼が出たら，細目構造は固定する（本文の書き込みは別）．
書き込み先は正本の T_ann_plan_details．依頼（T_ann_plan_requests）は細目番号に付いて動く．
"""
from flask import render_template, request, jsonify

from . import mid_term_progress_bp
from .routes import (login_required, require_upload_perm, _fdb, _s, safe_int, get_jst_now, md_to_html,
                     _goal_code, _goal_title, _load_columns, _group_names, SAME_MARK, is_same, resolve_same,
                     DONE_TEXT, is_done)
from .workflow import REQ_TABLE, _req_table_ready, _jbody, _now_uid, STAGE_LABEL, _status_label

KEYS = ('fiscal_year', 'mid_plan_no', 'annual_plan_no')


def _owner_choices():
    cols = _load_columns()
    return cols, (cols or _group_names())


def _locked(cur, year):
    """進捗報告・業務実績報告の依頼が1件でも出ていれば，その年度の細目構造は固定。"""
    if not _req_table_ready(cur):
        return False
    cur.execute("SELECT COUNT(*) AS n FROM %s WHERE fiscal_year = %%s AND stage IN ('progress','result') "
                "AND editor_status IS NOT NULL" % REQ_TABLE, (year,))
    return (cur.fetchone()['n'] or 0) > 0


def _term_span(cur, year):
    """year を含む期の最初の年度（過去年度の細目を引く範囲）。"""
    cur.execute("""SELECT term FROM T_mid_plan_details
                   WHERE %s BETWEEN COALESCE(valid_from, 0) AND COALESCE(valid_to, 9999) LIMIT 1""", (year,))
    r = cur.fetchone()
    if not r:
        return year
    cur.execute('SELECT MIN(valid_from) AS lo FROM T_mid_plan_details WHERE term = %s', (r['term'],))
    return cur.fetchone()['lo'] or year


def _plan_key(b):
    k = {c: safe_int(b.get(c)) for c in KEYS}
    return None if any(v is None for v in k.values()) else k


def _details(cur, k):
    cur.execute('SELECT * FROM T_ann_plan_details WHERE fiscal_year=%s AND mid_plan_no=%s AND annual_plan_no=%s '
                'ORDER BY detail_no', tuple(k[c] for c in KEYS))
    return cur.fetchall()


def _guard(cur, year, force=False):
    """細目構造が固定された年度では操作を断る．force=True（編集者が固定を一時解除したとき）は通す。"""
    if force:
        return None
    if _locked(cur, year):
        return jsonify({'success': False,
                        'error': '%d年度は進捗報告・業務実績報告の依頼が出ているため，細目構造は変えられません' % year}), 400
    return None


def _renumber(cur, k, detail_nos_in_order, now, uid):
    """細目番号を 1,2,3… に振り直す（依頼の行も一緒に動かす）。detail_nos_in_order は新しい並びの旧番号。"""
    kv = tuple(k[c] for c in KEYS)
    has_req = _req_table_ready(cur)
    # いったん +10000 の番号へ退避してから振り直す（一意キーの衝突を避ける）
    for old in detail_nos_in_order:
        cur.execute('UPDATE T_ann_plan_details SET detail_no = %s WHERE fiscal_year=%s AND mid_plan_no=%s '
                    'AND annual_plan_no=%s AND detail_no=%s', (old + 10000,) + kv + (old,))
        if has_req:
            cur.execute('UPDATE %s SET detail_no = %%s WHERE fiscal_year=%%s AND mid_plan_no=%%s '
                        'AND annual_plan_no=%%s AND detail_no=%%s' % REQ_TABLE, (old + 10000,) + kv + (old,))
    for i, old in enumerate(detail_nos_in_order, 1):
        cur.execute('UPDATE T_ann_plan_details SET detail_no = %s, updated_at = IF(%s <> %s, %s, updated_at), '
                    'updated_by = IF(%s <> %s, %s, updated_by) WHERE fiscal_year=%s AND mid_plan_no=%s '
                    'AND annual_plan_no=%s AND detail_no=%s',
                    (i, i, old, now, i, old, uid) + kv + (old + 10000,))
        if has_req:
            cur.execute('UPDATE %s SET detail_no = %%s WHERE fiscal_year=%%s AND mid_plan_no=%%s '
                        'AND annual_plan_no=%%s AND detail_no=%%s' % REQ_TABLE, (i,) + kv + (old + 10000,))


# -----------------------------------------------
# 画面
# -----------------------------------------------
@mid_term_progress_bp.route('/editor/structure')
@login_required
@require_upload_perm
def structure():
    with _fdb() as (cur, conn):
        cur.execute('SELECT DISTINCT fiscal_year FROM T_ann_plan_details ORDER BY fiscal_year')
        years = [r['fiscal_year'] for r in cur.fetchall()]
    year = safe_int(request.args.get('year'))
    if not year or year not in years:
        from flask import redirect, url_for
        return redirect(url_for('mid_term_progress.editor', year=year or None))
    cols, choices = _owner_choices()
    return render_template('mid_term_progress/structure.html', years=years, year=year,
                           columns=cols, choices=choices, all_groups=_group_names())


@mid_term_progress_bp.route('/api/structure')
@login_required
@require_upload_perm
def api_structure():
    """年度の細目集を年度計画ごとに。過去年度（同じ期）の細目と計画段階の依頼状態つき。"""
    year = safe_int(request.args.get('year'))
    if not year:
        return jsonify({'success': False, 'error': '年度を指定してください'}), 400
    with _fdb() as (cur, conn):
        lo = _term_span(cur, year)
        cur.execute("""SELECT * FROM T_ann_plan_details WHERE fiscal_year BETWEEN %s AND %s
                       ORDER BY mid_plan_no, annual_plan_no, fiscal_year, detail_no""", (lo, year))
        rows = cur.fetchall()
        reqs = {}
        if _req_table_ready(cur):
            cur.execute("SELECT * FROM %s WHERE fiscal_year = %%s AND stage = 'plan'" % REQ_TABLE, (year,))
            reqs = {(r['mid_plan_no'], r['annual_plan_no'], r['detail_no']): r for r in cur.fetchall()}
        cur.execute("""SELECT mid_plan_no, mid_goal_no, goal_chapter, goal_section, goal_item, goal_item_title,
                              goal_section_title, goal_chapter_title, mid_plan_text
                       FROM T_mid_plan_details
                       WHERE detail_no = 1 AND %s BETWEEN COALESCE(valid_from, 0) AND COALESCE(valid_to, 9999)
                       ORDER BY mid_plan_no, sub_term DESC""", (year,))
        heads = {}
        for m in cur.fetchall():
            heads.setdefault(m['mid_plan_no'], m)
        locked = _locked(cur, year)
        # 段階ごとの依頼の記録（担当のある細目のうち，依頼を出したと記録したもの）
        req_counts = {st: 0 for st in STAGE_LABEL}
        if _req_table_ready(cur):
            cur.execute('SELECT stage, COUNT(*) AS n FROM %s WHERE fiscal_year = %%s AND editor_status IS NOT NULL '
                        'GROUP BY stage' % REQ_TABLE, (year,))
            for r in cur.fetchall():
                req_counts[r['stage']] = r['n']
    assigned = sum(1 for r in rows if r['fiscal_year'] == year and (r.get('owner_group') or '').strip()
                   and not is_done(r.get('plan_text')))
    done_n = sum(1 for r in rows if r['fiscal_year'] == year and is_done(r.get('plan_text')))
    blocks = {}
    for r in rows:
        bk = (r['mid_plan_no'], r['annual_plan_no'])
        b = blocks.get(bk)
        if b is None:
            m = heads.get(r['mid_plan_no']) or {}
            b = blocks[bk] = {'mid_plan_no': r['mid_plan_no'], 'annual_plan_no': r['annual_plan_no'],
                              'goal': _goal_code(m) if m else '', 'goal_title': _goal_title(m) if m else '',
                              'mid_plan_html': md_to_html(m.get('mid_plan_text') or '') if m else '',
                              'history': [], 'current': []}
        if r['fiscal_year'] == year:
            q = reqs.get((r['mid_plan_no'], r['annual_plan_no'], r['detail_no'])) or {}
            b['current'].append({'detail_no': r['detail_no'], 'owner_group': (r.get('owner_group') or '').strip(),
                                 'plan_text': r.get('plan_text') or '',
                                 'plan_html': md_to_html(r.get('plan_text') or ''),
                                 'updated_at': _s(r.get('updated_at')),
                                 'editor_status': q.get('editor_status'), 'author_status': q.get('author_status'),
                                 'has_comment': bool((q.get('comment') or '').strip()),
                                 'done': is_done(r.get('plan_text')),
                                 'proposal': q.get('author_text') or '',
                                 'proposal_html': md_to_html(q.get('author_text') or ''),
                                 'proposal_at': _s(q.get('author_text_at'))})
        else:
            b['history'].append({'year': r['fiscal_year'], 'detail_no': r['detail_no'],
                                 '_same': is_same(r.get('plan_text')),
                                 'owner_group': (r.get('owner_group') or '').strip(),
                                 'plan_html': md_to_html(r.get('plan_text') or ''),
                                 'result_html': md_to_html(r.get('result_text') or '')})
    # 【同上】の解決（今年度・過去年度とも，年度ごとの細目の並びで）
    texts = {(r['fiscal_year'], r['mid_plan_no'], r['annual_plan_no'], r['detail_no']): r.get('plan_text') or ''
             for r in rows}
    groups = {}
    for r in rows:
        groups.setdefault((r['fiscal_year'], r['mid_plan_no'], r['annual_plan_no']), []).append(r)
    src_of = {}
    for gk, rs in groups.items():
        for dn, src in resolve_same(rs).items():
            src_of[gk + (dn,)] = src
    for b in blocks.values():
        for d in b['current']:
            if is_same(d['plan_text']):
                src = src_of.get((year, b['mid_plan_no'], b['annual_plan_no'], d['detail_no']))
                d['same'] = True
                d['same_src'] = src
                d['plan_html'] = md_to_html(texts.get((year, b['mid_plan_no'], b['annual_plan_no'], src), '')) if src else ''
        for x in b['history']:
            if x.get('_same'):
                src = src_of.get((x['year'], b['mid_plan_no'], b['annual_plan_no'], x['detail_no']))
                x['same_src'] = src
                x['plan_html'] = md_to_html(texts.get((x['year'], b['mid_plan_no'], b['annual_plan_no'], src), '')) if src else ''
            x.pop('_same', None)
    # 計画の正本が空の細目は，執筆者の提案を代わりに見せる（状態はバッジで示す．編集欄は正本のまま）
    from .progress import fallback_badge, active_keys
    active = active_keys()
    for b in blocks.values():
        cur_by = {d['detail_no']: d for d in b['current']}
        for d in b['current']:
            if d['done']:
                continue
            base = cur_by.get(d.get('same_src')) if d.get('same') else d
            if base and not (base['plan_text'] or '').strip() and base.get('proposal'):
                q = reqs.get((b['mid_plan_no'], b['annual_plan_no'], base['detail_no'])) or {}
                d['plan_html'] = base['proposal_html']
                d['plan_badge'] = fallback_badge(q, '執筆者の提案', active)
    # 今年度に行のある年度計画だけ（過去にしか無いものは廃止済みとして末尾に参考表示）
    out = sorted(blocks.values(), key=lambda b: (not b['current'], b['mid_plan_no'], b['annual_plan_no']))
    return jsonify({'success': True, 'year': year, 'from_year': lo, 'locked': locked, 'blocks': out,
                    'assigned': assigned, 'done': done_n, 'req_counts': req_counts,
                    'stages': [{'stage': st, 'label': lbl} for st, lbl in STAGE_LABEL.items()]})


# -----------------------------------------------
# 構造の操作（どれも細目構造が固定されていないときだけ）
# -----------------------------------------------
def _check_owner(owner):
    cols, _ = _owner_choices()
    if owner and cols and owner not in cols:
        return '「%s」は部門の並びにありません．先に部門の並びに加えてください' % owner
    if owner and len(owner) > 100:
        return '担当グループ名が長すぎます（100字まで）'
    return None


@mid_term_progress_bp.route('/api/structure/add', methods=['POST'])
@login_required
@require_upload_perm
def api_structure_add():
    """細目を新設する（末尾に）。担当と計画本文は指定があれば入れる。年度計画そのものが無ければ作る。"""
    b = _jbody()
    k = _plan_key(b)
    if not k:
        return jsonify({'success': False, 'error': '年度計画を指定してください'}), 400
    owner = (b.get('owner_group') or '').strip() or None
    text = (b.get('plan_text') or '').strip() or None
    err = _check_owner(owner)
    if err:
        return jsonify({'success': False, 'error': err}), 400
    now, uid = _now_uid()
    with _fdb() as (cur, conn):
        g = _guard(cur, k['fiscal_year'], bool(b.get('force')))
        if g:
            return g
        ds = _details(cur, k)
        nxt = (max(d['detail_no'] for d in ds) if ds else 0) + 1
        cur.execute('INSERT INTO T_ann_plan_details (fiscal_year, mid_plan_no, annual_plan_no, detail_no, owner_group, '
                    'plan_text, created_at, updated_at, updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (k['fiscal_year'], k['mid_plan_no'], k['annual_plan_no'], nxt, owner, text, now, now, uid))
        if not ds:
            cur.execute("""INSERT IGNORE INTO T_ann_plan_evaluations (fiscal_year, mid_plan_no, annual_plan_no,
                           created_at, updated_at, updated_by) VALUES (%s,%s,%s,%s,%s,%s)""",
                        (k['fiscal_year'], k['mid_plan_no'], k['annual_plan_no'], now, now, uid))
        conn.commit()
    return jsonify({'success': True, 'detail_no': nxt})


@mid_term_progress_bp.route('/api/structure/owner', methods=['POST'])
@login_required
@require_upload_perm
def api_structure_owner():
    """細目の担当部門を決める（空で解除）。計画段階の依頼が出ていれば，その控えも付け替える。"""
    b = _jbody()
    k, d = _plan_key(b), safe_int(b.get('detail_no'))
    owner = (b.get('owner_group') or '').strip() or None
    if not k or not d:
        return jsonify({'success': False, 'error': '細目を指定してください'}), 400
    err = _check_owner(owner)
    if err:
        return jsonify({'success': False, 'error': err}), 400
    now, uid = _now_uid()
    with _fdb() as (cur, conn):
        g = _guard(cur, k['fiscal_year'], bool(b.get('force')))
        if g:
            return g
        kv = tuple(k[c] for c in KEYS) + (d,)
        cur.execute('UPDATE T_ann_plan_details SET owner_group=%s, updated_at=%s, updated_by=%s WHERE fiscal_year=%s '
                    'AND mid_plan_no=%s AND annual_plan_no=%s AND detail_no=%s', (owner, now, uid) + kv)
        if _req_table_ready(cur):
            cur.execute('UPDATE %s SET owner_group=%%s, updated_at=%%s WHERE fiscal_year=%%s AND mid_plan_no=%%s '
                        'AND annual_plan_no=%%s AND detail_no=%%s AND editor_status IS NOT NULL' % REQ_TABLE,
                        (owner, now) + kv)
        conn.commit()
    return jsonify({'success': True})


@mid_term_progress_bp.route('/api/structure/delete', methods=['POST'])
@login_required
@require_upload_perm
def api_structure_delete():
    """細目を廃止する（本文・依頼・コメントも消える）。残りの細目番号は詰める。最後の1つは消せない。"""
    b = _jbody()
    k, d = _plan_key(b), safe_int(b.get('detail_no'))
    if not k or not d:
        return jsonify({'success': False, 'error': '細目を指定してください'}), 400
    now, uid = _now_uid()
    with _fdb() as (cur, conn):
        g = _guard(cur, k['fiscal_year'], bool(b.get('force')))
        if g:
            return g
        ds = _details(cur, k)
        if d not in [x['detail_no'] for x in ds]:
            return jsonify({'success': False, 'error': '細目が見つかりません'}), 404
        if len(ds) <= 1:
            return jsonify({'success': False, 'error': '年度計画の最後の細目は廃止できません．担当を外して空にしてください'}), 400
        kv = tuple(k[c] for c in KEYS) + (d,)
        cur.execute('DELETE FROM T_ann_plan_details WHERE fiscal_year=%s AND mid_plan_no=%s AND annual_plan_no=%s '
                    'AND detail_no=%s', kv)
        if _req_table_ready(cur):
            cur.execute('DELETE FROM %s WHERE fiscal_year=%%s AND mid_plan_no=%%s AND annual_plan_no=%%s '
                        'AND detail_no=%%s' % REQ_TABLE, kv)
        _renumber(cur, k, [x['detail_no'] for x in ds if x['detail_no'] != d], now, uid)
        conn.commit()
    return jsonify({'success': True})


@mid_term_progress_bp.route('/api/structure/move', methods=['POST'])
@login_required
@require_upload_perm
def api_structure_move():
    """細目を1つ上（dir=-1）／下（dir=1）へ動かす。"""
    b = _jbody()
    k, d, step = _plan_key(b), safe_int(b.get('detail_no')), safe_int(b.get('dir'))
    if not k or not d or step not in (-1, 1):
        return jsonify({'success': False, 'error': '細目と向きを指定してください'}), 400
    now, uid = _now_uid()
    with _fdb() as (cur, conn):
        g = _guard(cur, k['fiscal_year'], bool(b.get('force')))
        if g:
            return g
        order = [x['detail_no'] for x in _details(cur, k)]
        if d not in order:
            return jsonify({'success': False, 'error': '細目が見つかりません'}), 404
        i = order.index(d)
        j = i + step
        if not 0 <= j < len(order):
            return jsonify({'success': True, 'moved': False})
        order[i], order[j] = order[j], order[i]
        _renumber(cur, k, order, now, uid)
        conn.commit()
    return jsonify({'success': True, 'moved': True})


@mid_term_progress_bp.route('/api/structure/merge', methods=['POST'])
@login_required
@require_upload_perm
def api_structure_merge():
    """細目を1つ上の細目に統合する：本文を後ろに足し，コメントも足して，この細目を廃止する。"""
    b = _jbody()
    k, d = _plan_key(b), safe_int(b.get('detail_no'))
    if not k or not d:
        return jsonify({'success': False, 'error': '細目を指定してください'}), 400
    now, uid = _now_uid()
    with _fdb() as (cur, conn):
        g = _guard(cur, k['fiscal_year'], bool(b.get('force')))
        if g:
            return g
        ds = _details(cur, k)
        order = [x['detail_no'] for x in ds]
        if d not in order or order.index(d) == 0:
            return jsonify({'success': False, 'error': '先頭の細目は上に統合できません'}), 400
        src = ds[order.index(d)]
        dst = ds[order.index(d) - 1]

        def cat(a, b):
            a, b = (a or '').strip(), (b or '').strip()
            return (a + '\n' + b).strip() or None

        kv_dst = tuple(k[c] for c in KEYS) + (dst['detail_no'],)
        kv_src = tuple(k[c] for c in KEYS) + (d,)
        # 計画：統合される側が【同上】なら足さない．受ける側が【同上】で統合される側に計画があれば，それを受ける側の計画にする
        if is_same(src.get('plan_text')):
            plan = dst.get('plan_text')
        elif is_same(dst.get('plan_text')):
            plan = (src.get('plan_text') or '').strip() or dst.get('plan_text')
        else:
            plan = cat(dst.get('plan_text'), src.get('plan_text'))
        cur.execute('UPDATE T_ann_plan_details SET plan_text=%s, progress_text=%s, result_text=%s, owner_group=%s, '
                    'updated_at=%s, updated_by=%s WHERE fiscal_year=%s AND mid_plan_no=%s AND annual_plan_no=%s '
                    'AND detail_no=%s',
                    (plan, cat(dst.get('progress_text'), src.get('progress_text')),
                     cat(dst.get('result_text'), src.get('result_text')),
                     (dst.get('owner_group') or '').strip() or (src.get('owner_group') or '').strip() or None,
                     now, uid) + kv_dst)
        if _req_table_ready(cur):
            cur.execute('SELECT stage, comment FROM %s WHERE fiscal_year=%%s AND mid_plan_no=%%s AND annual_plan_no=%%s '
                        'AND detail_no=%%s' % REQ_TABLE, kv_src)
            for q in cur.fetchall():
                if (q.get('comment') or '').strip():
                    cur.execute('SELECT comment FROM %s WHERE fiscal_year=%%s AND mid_plan_no=%%s AND annual_plan_no=%%s '
                                'AND detail_no=%%s AND stage=%%s' % REQ_TABLE, kv_dst + (q['stage'],))
                    r = cur.fetchone()
                    if r is None:
                        cur.execute('INSERT INTO %s (fiscal_year, mid_plan_no, annual_plan_no, detail_no, stage, comment, '
                                    'comment_updated_at, comment_updated_by, created_at, updated_at) '
                                    'VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)' % REQ_TABLE,
                                    kv_dst + (q['stage'], q['comment'], now, uid, now, now))
                    else:
                        cur.execute('UPDATE %s SET comment=%%s, comment_updated_at=%%s, comment_updated_by=%%s, updated_at=%%s '
                                    'WHERE fiscal_year=%%s AND mid_plan_no=%%s AND annual_plan_no=%%s AND detail_no=%%s '
                                    'AND stage=%%s' % REQ_TABLE,
                                    (cat(r.get('comment'), q['comment']), now, uid, now) + kv_dst + (q['stage'],))
            cur.execute('DELETE FROM %s WHERE fiscal_year=%%s AND mid_plan_no=%%s AND annual_plan_no=%%s '
                        'AND detail_no=%%s' % REQ_TABLE, kv_src)
        cur.execute('DELETE FROM T_ann_plan_details WHERE fiscal_year=%s AND mid_plan_no=%s AND annual_plan_no=%s '
                    'AND detail_no=%s', kv_src)
        _renumber(cur, k, [x for x in order if x != d], now, uid)
        conn.commit()
    return jsonify({'success': True})


@mid_term_progress_bp.route('/api/structure/same', methods=['POST'])
@login_required
@require_upload_perm
def api_structure_same():
    """細目を「同上」にする（on=true：計画本文を【同上】にする）／外す（on=false：計画本文を空にする）。
    先頭の細目は同上にできない。自分の計画本文があるときは，同上にすると消える（画面で確かめてから呼ぶ）。"""
    b = _jbody()
    k, d = _plan_key(b), safe_int(b.get('detail_no'))
    on = bool(b.get('on'))
    if not k or not d:
        return jsonify({'success': False, 'error': '細目を指定してください'}), 400
    now, uid = _now_uid()
    with _fdb() as (cur, conn):
        g = _guard(cur, k['fiscal_year'], bool(b.get('force')))
        if g:
            return g
        order = [x['detail_no'] for x in _details(cur, k)]
        if d not in order:
            return jsonify({'success': False, 'error': '細目が見つかりません'}), 404
        if on and order.index(d) == 0:
            return jsonify({'success': False, 'error': '先頭の細目は同上にできません'}), 400
        cur.execute('UPDATE T_ann_plan_details SET plan_text=%s, updated_at=%s, updated_by=%s WHERE fiscal_year=%s '
                    'AND mid_plan_no=%s AND annual_plan_no=%s AND detail_no=%s',
                    (SAME_MARK if on else None, now, uid) + tuple(k[c] for c in KEYS) + (d,))
        conn.commit()
    return jsonify({'success': True})


@mid_term_progress_bp.route('/api/structure/done', methods=['POST'])
@login_required
@require_upload_perm
def api_structure_done():
    """細目を「対応済み」にする（on=true：計画・進捗報告・業務実績報告に「・対応済み」を入れ，依頼の記録を消す）／
    外す（on=false：対応済みの文言の入った欄だけを空にする）。"""
    b = _jbody()
    k, d = _plan_key(b), safe_int(b.get('detail_no'))
    on = bool(b.get('on'))
    if not k or not d:
        return jsonify({'success': False, 'error': '細目を指定してください'}), 400
    now, uid = _now_uid()
    kv = tuple(k[c] for c in KEYS) + (d,)
    with _fdb() as (cur, conn):
        g = _guard(cur, k['fiscal_year'], bool(b.get('force')))
        if g:
            return g
        cur.execute('SELECT plan_text, progress_text, result_text FROM T_ann_plan_details WHERE fiscal_year=%s '
                    'AND mid_plan_no=%s AND annual_plan_no=%s AND detail_no=%s', kv)
        r = cur.fetchone()
        if not r:
            return jsonify({'success': False, 'error': '細目が見つかりません'}), 404
        if on:
            vals = (DONE_TEXT, DONE_TEXT, DONE_TEXT)
        else:
            vals = tuple(None if is_done(r.get(c)) else r.get(c) for c in ('plan_text', 'progress_text', 'result_text'))
        cur.execute('UPDATE T_ann_plan_details SET plan_text=%s, progress_text=%s, result_text=%s, updated_at=%s, '
                    'updated_by=%s WHERE fiscal_year=%s AND mid_plan_no=%s AND annual_plan_no=%s AND detail_no=%s',
                    vals + (now, uid) + kv)
        if on and _req_table_ready(cur):          # 依頼は不要になる（コメントのある記録は残して依頼だけ外す）
            cur.execute("DELETE FROM %s WHERE fiscal_year=%%s AND mid_plan_no=%%s AND annual_plan_no=%%s "
                        "AND detail_no=%%s AND (comment IS NULL OR comment = '')" % REQ_TABLE, kv)
            cur.execute('UPDATE %s SET editor_status=NULL, updated_at=%%s WHERE fiscal_year=%%s AND mid_plan_no=%%s '
                        'AND annual_plan_no=%%s AND detail_no=%%s' % REQ_TABLE, (now,) + kv)
        conn.commit()
    return jsonify({'success': True})
