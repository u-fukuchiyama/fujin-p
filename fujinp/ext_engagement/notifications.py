"""
ext_engagement.notifications — 通知一覧（事務局・委員長．2026-10-19）

  GET /notifications   Slack_port から送った通知の記録の一覧
                       ?case=<案件ID>      案件で絞る
                       ?kind=結論通知       種類で絞る
                       ?route=channel|dm   宛先の種類で絞る
                       ?failed=1           送れなかったものだけ

記録は slack_notifier._record が通知のたびに ext_engagement_notifications に入れる．
一覧はサーバ側で HTML に描いて返す．
"""
import logging

from flask import render_template, request, session

from decorators import login_required

from . import ext_engagement_bp
from . import slack_notifier as sn

LIST_LIMIT = 1000

KIND_ORDER = ['案件登録通知DM', '審議開始通知', '審議コメント通知', '委員長提案通知', '結論通知',
              '委員長リマインドDM', '試し送信']


@ext_engagement_bp.route('/notifications')
@login_required
def notifications_list():
    from .routes import can_view_notifications, fmt_datetime, format_case_id
    user_id = session.get('user_id')
    if not can_view_notifications(user_id):
        return ('<h2 style="font-family:sans-serif;padding:40px;color:#991b1b">通知一覧は事務局・委員長だけが見られます</h2>'
                '<p style="font-family:sans-serif;padding:0 40px"><a href="/ext_engagement/">連携申請審査のトップへ戻る</a></p>'), 403

    f_case = request.args.get('case', type=int)
    f_kind = (request.args.get('kind') or '').strip()
    f_route = (request.args.get('route') or '').strip()
    f_failed = request.args.get('failed') == '1'

    where, params = [], []
    if f_case:
        where.append("n.case_id = %s"); params.append(f_case)
    if f_kind:
        where.append("n.kind = %s"); params.append(f_kind)
    if f_route in ('channel', 'dm'):
        where.append("n.route = %s"); params.append(f_route)
    if f_failed:
        where.append("n.ok = 0")
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    def disp(c):
        try:
            return format_case_id(c['fiscal_year'], c['type_key'], c['seq_no'])
        except Exception:
            return f"#{c.get('id')}"

    rows, error, total = [], None, 0
    kinds, cases = [], []
    conn = None
    try:
        conn = sn._db()
        cur = conn.cursor(dictionary=True)
        if not sn._notif_ready['ok']:
            cur.execute(sn.NOTIFICATIONS_DDL)
            sn._notif_ready['ok'] = True
        cur.execute("SELECT COUNT(*) AS c FROM ext_engagement_notifications n " + where_sql, params)
        total = cur.fetchone()['c']
        cur.execute(
            "SELECT n.*, c.title AS case_title, c.fiscal_year, c.type_key, c.seq_no, "
            "u.full_name AS sender_name "
            "FROM ext_engagement_notifications n "
            "LEFT JOIN ext_engagement_cases c ON c.id = n.case_id "
            "LEFT JOIN users u ON u.id = n.sent_by_user_id "
            + where_sql + " ORDER BY n.sent_at DESC, n.id DESC LIMIT %s",
            params + [LIST_LIMIT])
        for r in cur.fetchall():
            if r['route'] == 'dm':
                to = (r.get('recipient_name') or '') + (f"（{r['recipient_email']}）" if r.get('recipient_email') else '')
                to = 'DM：' + (to or '（宛先不明）')
            elif r.get('ch_no') is not None:
                to = f"チャンネル {r['ch_no']} 番" + (f"（{r['ch_label']}）" if r.get('ch_label') else '')
            else:
                to = 'チャンネル（未設定）'
            if r.get('sender_name'):
                sender = r['sender_name']
            elif r['kind'] == '委員長リマインドDM':
                sender = '（自動：期日チェック）'
            else:
                sender = '—'
            rows.append({
                'sent_at': fmt_datetime(r['sent_at']),
                'kind': r['kind'],
                'route': r['route'],
                'to': to,
                'bot': r.get('bot') or '',
                'case_id': r.get('case_id'),
                'case_display_id': disp(r) if r.get('fiscal_year') is not None else (
                    f"#{r['case_id']}（削除済み）" if r.get('case_id') else ''),
                'case_title': r.get('case_title') or '',
                'case_exists': r.get('fiscal_year') is not None,
                'body': r.get('body') or '',
                'ok': bool(r.get('ok')),
                'error': r.get('error') or '',
                'sender': sender,
            })
        cur.execute("SELECT DISTINCT kind FROM ext_engagement_notifications")
        found = {r['kind'] for r in cur.fetchall()}
        kinds = [k for k in KIND_ORDER if k in found] + sorted(found - set(KIND_ORDER))
        cur.execute("SELECT DISTINCT c.id, c.fiscal_year, c.type_key, c.seq_no "
                    "FROM ext_engagement_notifications n JOIN ext_engagement_cases c ON c.id = n.case_id "
                    "ORDER BY c.fiscal_year DESC, c.id DESC")
        cases = [{'id': c['id'], 'label': disp(c)} for c in cur.fetchall()]
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

    return render_template('ext_engagement/notifications.html',
                           rows=rows, total=total, limit=LIST_LIMIT, error=error,
                           kinds=kinds, cases=cases,
                           f_case=f_case, f_kind=f_kind, f_route=f_route, f_failed=f_failed)
