"""
mid_term_progress - 執筆者ダッシュボード

編集者ダッシュボードで「執筆者に開く」にした依頼（年度×段階の組．ふつうは高々2つ）だけを並べ，
どれを入力するかをラジオボタンで選ぶ。
  執筆者：担当グループの細目だけを見て，入力できる
  編集者：担当にかかわらず全細目を見て，入力できる（依頼内容の確認と入力の手伝い）
入力は「執筆者からの報告」（T_ann_plan_requests.author_text）．正本は編集者が編集者の画面で書く。
"""
from flask import render_template, request, jsonify

from . import mid_term_progress_bp
from .routes import login_required, can_upload, _fdb, safe_int
from .workflow import (STAGE_LABEL, load_active_sets, is_active, _my_groups, _req_table_ready,
                       _author_cols_ready)
from .progress import build_blocks, STATE_LABEL


def _sets_labeled():
    return [dict(a, label='%d年度 %s' % (a['year'], {'plan': '年度計画策定', 'progress': '年度計画進捗状況報告',
                                                     'result': '業務実績報告'}[a['stage']]))
            for a in load_active_sets()]


@mid_term_progress_bp.route('/author')
@login_required
def author():
    sets = _sets_labeled()
    editor = can_upload()
    groups = _my_groups()
    pick = request.args.get('set') or ''
    cur_set = next((a for a in sets if '%d-%s' % (a['year'], a['stage']) == pick), sets[-1] if sets else None)
    with _fdb() as (cur, conn):
        ready = _req_table_ready(cur) and _author_cols_ready(cur)
    return render_template('mid_term_progress/author.html', sets=sets, cur_set=cur_set, editor=editor,
                           has_groups=bool(groups), ready=ready, state_label=STATE_LABEL,
                           stage_label=STAGE_LABEL)


@mid_term_progress_bp.route('/api/author/items')
@login_required
def api_author_items():
    year = safe_int(request.args.get('year'))
    stage = request.args.get('stage')
    if not year or stage not in STAGE_LABEL or not is_active(year, stage):
        return jsonify({'success': False, 'error': 'この年度・段階は，いま執筆の受付をしていません'}), 400
    editor = can_upload()
    groups = set(_my_groups())
    visible = None if editor else (lambda d: d['owner_group'] in groups)
    with _fdb() as (cur, conn):
        blocks = build_blocks(cur, year, stage, visible=visible)
    if not editor:
        # 執筆者には，担当でない細目の報告・コメントは渡さない（計画の並びは見せる）
        for b in blocks:
            for d in b['details']:
                if not d['visible']:
                    for f in ('author_text', 'author_html', 'comment', 'comment_html', 'final_text', 'final_html'):
                        d[f] = ''
    owners = sorted({d['owner_group'] for b in blocks for d in b['details']
                     if d['owner_group'] and (editor or d['visible'])})
    return jsonify({'success': True, 'year': year, 'stage': stage, 'editor': editor, 'blocks': blocks,
                    'owners': owners})
