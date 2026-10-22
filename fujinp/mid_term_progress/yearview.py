"""
mid_term_progress - 年度ごとの表示（構成員向け・読み取り専用）

トップページ（事業全貌）の「年度ごと」．編集者画面とほぼ同じ形で，1つの年度を計画番号ごとに並べる。
  計画番号の塊：見出し（中期目標の章節）→ 中期計画 → 細目ごとの行
  行の欄：番号／年度計画／進捗状況報告／業務実績報告／評価室からのコメント
  塊の下：計画番号ごとの自己評価・評価委員会の評価
どの欄を出すかは，編集者ダッシュボードの「構成員に開示」「コメントも表示」の設定（workflow.load_disclose）に従う。
担当（部門）と状態は出さない。
"""
from flask import render_template, request

from . import mid_term_progress_bp
from .routes import (login_required, require_view_perm, can_upload, _fdb, safe_int, md_to_html,
                     _goal_code, _goal_title, is_same, is_done, resolve_same)
from .workflow import load_disclose, disclose_view


def _req_rows(cur, year):
    try:
        cur.execute('SELECT * FROM T_ann_plan_requests WHERE fiscal_year = %s', (year,))
        return {(q['mid_plan_no'], q['annual_plan_no'], q['detail_no'], q['stage']): q for q in cur.fetchall()}
    except Exception:
        return {}


def _terms():
    """表示期間の選択に並べる期：[(期の番号, 表示名)]。"""
    from .routes import term_label
    try:
        with _fdb() as (cur, conn):
            cur.execute('SELECT DISTINCT term FROM T_mid_plan_details ORDER BY term')
            return [(r['term'], term_label(r['term'])) for r in cur.fetchall()]
    except Exception:
        return []


@mid_term_progress_bp.route('/view/year')
@login_required
@require_view_perm
def view_year():
    from .progress import fallback_badge, active_keys
    disc = load_disclose()
    years = sorted(y for y in disc if disclose_view(disc, y)['show']['plan'])
    year = safe_int(request.args.get('year')) or (years[-1] if years else None)
    ctx = {'year': year, 'years': years, 'blocks': [], 'cols': [], 'can_upload': can_upload(), 'pm_terms': _terms()}
    if year not in years:
        return render_template('mid_term_progress/year_view.html', **ctx)
    dv = disclose_view(disc, year)
    show, cmt = dv['show'], dv['comments']
    active = active_keys()
    with _fdb() as (cur, conn):
        cur.execute('SELECT * FROM T_ann_plan_details WHERE fiscal_year = %s '
                    'ORDER BY mid_plan_no, annual_plan_no, detail_no', (year,))
        rows = cur.fetchall()
        reqs = _req_rows(cur, year)
        evals = {}
        try:
            cur.execute('SELECT * FROM T_ann_plan_evaluations WHERE fiscal_year = %s', (year,))
            evals = {(q['mid_plan_no'], q['annual_plan_no']): q for q in cur.fetchall()}
        except Exception:
            evals = {}
        cur.execute("""SELECT mid_plan_no, mid_goal_no, goal_chapter, goal_section, goal_item, goal_item_title,
                              goal_section_title, goal_chapter_title, mid_plan_text
                       FROM T_mid_plan_details
                       WHERE detail_no = 1 AND %s BETWEEN COALESCE(valid_from, 0) AND COALESCE(valid_to, 9999)
                       ORDER BY mid_plan_no, sub_term DESC""", (year,))
        heads = {}
        for m in cur.fetchall():
            heads.setdefault(m['mid_plan_no'], m)

    cols = ['plan'] + (['progress'] if show['progress'] else []) + (['result'] if show['result'] else [])
    any_cmt = any(cmt[s] for s in ('plan', 'progress', 'result'))
    if any_cmt:
        cols.append('cmt')

    def pick(r, col, stage, what):
        """正本が空なら執筆者の本文を出し，状態をバッジで示す。"""
        t = (r.get(col) or '').strip()
        if t:
            return t, None
        q = reqs.get((r['mid_plan_no'], r['annual_plan_no'], r['detail_no'], stage))
        if q and (q.get('author_text') or '').strip():
            return q['author_text'], fallback_badge(q, what, active)
        return '', None

    groups = {}
    for r in rows:
        groups.setdefault((r['mid_plan_no'], r['annual_plan_no']), []).append(r)
    blocks = []
    for (mp, ap), rs in sorted(groups.items()):
        m = heads.get(mp) or {}
        src = resolve_same(rs)
        plan_of = {r['detail_no']: pick(r, 'plan_text', 'plan', '執筆者の提案') for r in rs}
        details = []
        for r in rs:
            dn = r['detail_no']
            d = {'label': '%s-%s' % (ap, dn) if len(rs) > 1 else str(ap), 'done': is_done(r.get('plan_text')),
                 'same': is_same(r.get('plan_text')), 'plan_src': src.get(dn)}
            pt, pb = plan_of.get(src.get(dn)) if d['same'] else plan_of.get(dn)
            d['plan_html'], d['plan_badge'] = md_to_html(pt or ''), pb
            if show['progress'] and not d['done']:
                t, b = pick(r, 'progress_text', 'progress', '執筆者の報告')
                d['prog_html'], d['prog_badge'] = md_to_html(t), b
            if show['result'] and not d['done']:
                t, b = pick(r, 'result_text', 'result', '執筆者の報告')
                d['res_html'], d['res_badge'] = md_to_html(t), b
            cs = []
            for st, lb in (('plan', '年度計画'), ('progress', '進捗状況報告'), ('result', '業務実績報告')):
                q = reqs.get((mp, ap, dn, st))
                if cmt[st] and q and (q.get('comment') or '').strip():
                    cs.append({'label': lb, 'html': md_to_html(q['comment'])})
            d['comments'] = cs
            details.append(d)
        ev = None
        q = evals.get((mp, ap))
        if q:
            ev = {'self_eval': q.get('self_eval') if show['result'] else None,
                  'self_html': md_to_html(q.get('self_eval_reason') or '') if show['result'] else '',
                  'drafting': q.get('self_eval_status') == 'drafting',
                  'committee_eval': q.get('committee_eval') if show['hojin'] else None,
                  'committee_html': md_to_html(q.get('committee_comment') or '') if show['hojin'] else ''}
            if ev['self_eval'] is None and not ev['self_html'] and ev['committee_eval'] is None and not ev['committee_html']:
                ev = None
        blocks.append({'mid_plan_no': mp, 'annual_plan_no': ap, 'goal': _goal_code(m) if m else '',
                       'goal_title': _goal_title(m) if m else '',
                       'mid_plan_html': md_to_html(m.get('mid_plan_text') or '') if m else '',
                       'details': details, 'eval': ev})
    ctx.update({'blocks': blocks, 'cols': cols, 'show': show, 'cmt': cmt})
    return render_template('mid_term_progress/year_view.html', **ctx)
