"""
kitare_deliberation.notifications — 通知一覧（業務会議・admin．2026-10-19）

  GET /notifications   Slack_port から送った通知の記録の一覧
                       ?case=K-2026-012  案件で絞る
                       ?kind=議決通知     種類で絞る
                       ?route=channel|dm 宛先の種類で絞る
                       ?failed=1          送れなかったものだけ

記録は slack_notifier._record が通知のたびに kitare_notifications に入れる．
一覧はサーバ側で HTML に描いて返す．
"""
import logging

from flask import render_template, request, session

from decorators import login_required

from . import kitare_deliberation_bp
from . import slack_notifier as sn

LIST_LIMIT = 1000

KIND_ORDER = ['申請通知', '申請通知DM', '審議開始通知', '議決通知', '締切通知', '重要発言通知', '試し送信']


@kitare_deliberation_bp.route('/notifications')
@login_required
def notifications_list():
    from .routes import is_business, _deny, fmt_datetime
    user_id = session.get('user_id')
    if not is_business(user_id):
        return _deny('通知一覧は業務会議だけが見られます')

    f_case = (request.args.get('case') or '').strip()
    f_kind = (request.args.get('kind') or '').strip()
    f_route = (request.args.get('route') or '').strip()
    f_failed = request.args.get('failed') == '1'

    where, params = [], []
    if f_case:
        where.append("n.case_display_id = %s"); params.append(f_case)
    if f_kind:
        where.append("n.kind = %s"); params.append(f_kind)
    if f_route in ('channel', 'dm'):
        where.append("n.route = %s"); params.append(f_route)
    if f_failed:
        where.append("n.ok = 0")
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    rows, error, total = [], None, 0
    kinds, cases = [], []
    conn = None
    try:
        conn = sn._db()
        cur = conn.cursor(dictionary=True)
        if not sn._notif_ready['ok']:
            cur.execute(sn.NOTIFICATIONS_DDL)
            sn._notif_ready['ok'] = True
        cur.execute("SELECT COUNT(*) AS c FROM kitare_notifications n " + where_sql, params)
        total = cur.fetchone()['c']
        cur.execute(
            "SELECT n.*, c.title AS case_title, u.full_name AS sender_name "
            "FROM kitare_notifications n "
            "LEFT JOIN kitare_cases c ON c.id = n.case_id "
            "LEFT JOIN users u ON u.id = n.sent_by_user_id "
            + where_sql + " ORDER BY n.sent_at DESC, n.id DESC LIMIT %s",
            params + [LIST_LIMIT])
        for r in cur.fetchall():
            if r['route'] == 'dm':
                to = (r.get('recipient_name') or '') + (f"（{r['recipient_email']}）" if r.get('recipient_email') else '')
                to = 'DM：' + (to or '（宛先不明）')
            else:
                to = f"チャンネル {r['ch_no']} 番" + (f"（{r['ch_label']}）" if r.get('ch_label') else '')
            rows.append({
                'sent_at': fmt_datetime(r['sent_at']),
                'kind': r['kind'],
                'route': r['route'],
                'to': to,
                'bot': r.get('bot') or '',
                'case_id': r.get('case_id'),
                'case_display_id': r.get('case_display_id') or '',
                'case_title': r.get('case_title') or '',
                'body': r.get('body') or '',
                'ok': bool(r.get('ok')),
                'error': r.get('error') or '',
                'sender': r.get('sender_name') or ('（自動：締切の点検）' if r['kind'] == '締切通知' else '—'),
            })
        cur.execute("SELECT DISTINCT kind FROM kitare_notifications")
        found = {r['kind'] for r in cur.fetchall()}
        kinds = [k for k in KIND_ORDER if k in found] + sorted(found - set(KIND_ORDER))
        cur.execute("SELECT DISTINCT case_display_id FROM kitare_notifications "
                    "WHERE case_display_id IS NOT NULL ORDER BY case_display_id DESC")
        cases = [r['case_display_id'] for r in cur.fetchall()]
        cur.close()
    except Exception as e:
        logging.error("notifications_list error: %s", e)
        error = f'通知の記録を読めません: {e}'
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return render_template('kitare_deliberation/notifications.html',
                           rows=rows, total=total, limit=LIST_LIMIT, error=error,
                           kinds=kinds, cases=cases,
                           f_case=f_case, f_kind=f_kind, f_route=f_route, f_failed=f_failed)
