"""
ethics_review.notifications — 通知一覧（委員長プール・事務局・admin．2026-10-19）

  GET /notifications   Slack_port から送った通知の記録の一覧
                       ?case=E-2026-010  案件で絞る
                       ?kind=判断通知     種類で絞る
                       ?route=channel|dm 宛先の種類で絞る
                       ?failed=1          送れなかったものだけ

記録は slack_notifier._record が通知のたびに ethics_notifications に入れる．
一覧はサーバ側で HTML に描いて返す．
"""
import logging

from flask import render_template, request, session

from decorators import login_required

from . import ethics_review_bp
from . import slack_notifier as sn

LIST_LIMIT = 1000

KIND_ORDER = ['申請通知', '担当委員依頼通知', '審査開始通知', '判断通知', '判断結果の申請者DM', 'アラート発言通知',
              '申請者連絡通知', '書類追加通知', '審査再開通知', '試し送信']
PURPOSE_LABEL = {'business': '業務会議（事務局）向け', 'committee': '委員会向け'}


@ethics_review_bp.route('/notifications')
@login_required
def notifications_list():
    from .routes import can_view_notifications, fmt_datetime
    user_id = session.get('user_id')
    if not can_view_notifications(user_id):
        return ('<h2 style="font-family:sans-serif;padding:40px;color:#991b1b">通知一覧は委員長プール・事務局だけが見られます</h2>'
                '<p style="font-family:sans-serif;padding:0 40px"><a href="/ethics_review/secretariat">体制運営パネルへ戻る</a></p>'), 403

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
        cur.execute("SELECT COUNT(*) AS c FROM ethics_notifications n " + where_sql, params)
        total = cur.fetchone()['c']
        cur.execute(
            "SELECT n.*, c.title AS case_title, u.full_name AS sender_name "
            "FROM ethics_notifications n "
            "LEFT JOIN ethics_cases c ON c.id = n.case_id "
            "LEFT JOIN users u ON u.id = n.sent_by_user_id "
            + where_sql + " ORDER BY n.sent_at DESC, n.id DESC LIMIT %s",
            params + [LIST_LIMIT])
        for r in cur.fetchall():
            if r['route'] == 'dm':
                to = (r.get('recipient_name') or '') + (f"（{r['recipient_email']}）" if r.get('recipient_email') else '')
                to = 'DM：' + (to or '（宛先不明）')
            else:
                to = f"チャンネル {r['ch_no']} 番" + (f"（{r['ch_label']}）" if r.get('ch_label') else '')
                if r.get('purpose') in PURPOSE_LABEL:
                    to += f"［{PURPOSE_LABEL[r['purpose']]}］"
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
                'sender': r.get('sender_name') or '—',
            })
        cur.execute("SELECT DISTINCT kind FROM ethics_notifications")
        found = {r['kind'] for r in cur.fetchall()}
        kinds = [k for k in KIND_ORDER if k in found] + sorted(found - set(KIND_ORDER))
        cur.execute("SELECT DISTINCT case_display_id FROM ethics_notifications "
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

    return render_template('ethics_review/notifications.html',
                           rows=rows, total=total, limit=LIST_LIMIT, error=error,
                           kinds=kinds, cases=cases,
                           f_case=f_case, f_kind=f_kind, f_route=f_route, f_failed=f_failed)
