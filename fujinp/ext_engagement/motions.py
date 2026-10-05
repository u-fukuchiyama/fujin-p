"""
ext_engagement - 委員長提案（motion）

審議中に委員から懸念が出たとき、委員長が「委員長決定」ではなく
「委員長から委員会への提案 → 委員の賛否 → 採択 → 結論宣言」という
手続きで結論に至るための仕組み。

流れ:
  1. 委員長（または代理）が提案を起案する（審議中の案件・未決の提案がないとき）
       - 結論案（承認／条件を付して承認／不承認／継続審議）
       - 提案の趣旨・条件文・理由
       - 審査期限の延長（任意。既存アラート機構に「委員長提案により延長」として記録）
       - Slackチャンネルへ @channel で通知（既定ON）
  2. 委員（COI除外を除く。委員長は除く）が提案に対して
       賛成／修正意見あり／反対 ＋コメント を表明する（上書き可）
  3. 委員長が採択／取り下げ／修正して再提案（差し替え）を行う
       - 採択すると結論宣言パネルに結論案と文面が引き継がれる
  4. 経緯はすべて審議コメント欄の時系列に「委員長提案」カードとして残る

テーブル: ext_engagement_motions / ext_engagement_motion_votes
（sql/ext_engagement_motions.sql）
"""
import logging

from flask import request, jsonify, session
import mysql.connector

from db import DatabaseConfig
from decorators import login_required

from . import ext_engagement_bp
from . import slack_notifier
from . import notify_log
from .routes import (
    get_jst_now, fmt_datetime, parse_datetime, format_case_id,
    is_effective_chair, can_access_review, get_round_committee_user_ids,
    get_vote_summary, write_alert_log, alert_role_of, case_page_url,
    STATUS_LABELS,
)

# ────────────────────────────────────────────
# 定数
# ────────────────────────────────────────────

# 提案の結論案
MOTION_DECISION_LABELS = {
    'approve':      '承認',
    'approve_cond': '承認（条件を文面に明記）',
    'reject':       '不承認',
    'continue':     '継続審議（審査期限の延長のみ）',
}

# 結論案 → 結論宣言時の status
MOTION_DECISION_TO_STATUS = {
    'approve':      'approved',
    'approve_cond': 'approved',
    'reject':       'rejected',
    'continue':     None,
}

# 提案への意見表明の3区分
MOTION_VOTE_LABELS = {
    'agree':    '賛成',
    'amend':    '修正意見あり',
    'disagree': '反対',
    'remark':   '発言',   # 委員長用。賛否には数えず、委員と同じ欄に発言だけを残す
}

MOTION_STATUS_LABELS = {
    'open':       '審議中',
    'adopted':    '採択',
    'superseded': '差替済',
    'withdrawn':  '取下げ',
}

# 審議コメント欄（ext_engagement_comments.comment_type）に残す種別
CT_MOTION            = 'motion'             # 提案の起案
CT_MOTION_ADOPTED    = 'motion_adopted'     # 採択
CT_MOTION_WITHDRAWN  = 'motion_withdrawn'   # 取り下げ
CT_MOTION_SUPERSEDED = 'motion_superseded'  # 差し替え


# ────────────────────────────────────────────
# 取得系
# ────────────────────────────────────────────

def _serialize_motion(m):
    m = dict(m)
    for k in ('created_at', 'updated_at', 'closed_at',
              'old_review_end', 'new_review_end'):
        m[k] = fmt_datetime(m.get(k))
    m['decision_label'] = MOTION_DECISION_LABELS.get(m.get('proposed_decision'), '')
    m['status_label']   = MOTION_STATUS_LABELS.get(m.get('status'), '')
    m['conditions']     = m.get('conditions') or ''
    m['reason']         = m.get('reason') or ''
    m['closing_note']   = m.get('closing_note') or ''
    return m


def get_motions(case_id, round_no=None):
    """
    案件の提案一覧（新しい順）と、各提案への意見表明を返す。
    round_no を指定するとそのラウンドのみ。
    戻り値: [{...motion, votes:[...], vote_summary:{counts,total_voted,eligible,all_voted}}, ...]
    """
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        sql = """
            SELECT m.*, u.full_name AS proposed_by_name,
                   cu.full_name AS closed_by_name
            FROM ext_engagement_motions m
            LEFT JOIN users u  ON m.proposed_by = u.id
            LEFT JOIN users cu ON m.closed_by   = cu.id
            WHERE m.case_id = %s
        """
        params = [case_id]
        if round_no is not None:
            sql += " AND m.round_no = %s"
            params.append(round_no)
        sql += " ORDER BY m.id DESC"
        cursor.execute(sql, params)
        motions = [_serialize_motion(r) for r in cursor.fetchall()]
        if not motions:
            return []
        ids = [m['id'] for m in motions]
        fmt = ','.join(['%s'] * len(ids))
        cursor.execute(f"""
            SELECT v.*, u.full_name
            FROM ext_engagement_motion_votes v
            LEFT JOIN users u ON v.user_id = u.id
            WHERE v.motion_id IN ({fmt})
            ORDER BY v.updated_at ASC
        """, ids)
        by_motion = {}
        for r in cursor.fetchall():
            by_motion.setdefault(r['motion_id'], []).append({
                'user_id':    r['user_id'],
                'full_name':  r.get('full_name') or '',
                'vote':       r['vote'],
                'vote_label': MOTION_VOTE_LABELS.get(r['vote'], r['vote']),
                'comment':    r.get('comment') or '',
                'updated_at': fmt_datetime(r.get('updated_at')),
            })
    except Exception as e:
        logging.error("get_motions error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()

    for m in motions:
        votes = by_motion.get(m['id'], [])
        eligible = get_round_committee_user_ids(case_id, m['round_no'] or 1)
        counts = {'agree': 0, 'amend': 0, 'disagree': 0}
        voted = set()
        # 委員長は投票には参加しないが、委員と同じ欄に「発言」を残せる。
        # 発言（remark）は賛否の集計・進捗のいずれにも数えない。
        for v in votes:
            v['is_chair'] = v['user_id'] not in eligible
            if v['vote'] in counts:
                counts[v['vote']] += 1
            if v['vote'] != 'remark' and not v['is_chair']:
                voted.add(v['user_id'])
        m['votes'] = votes
        m['vote_summary'] = {
            'counts':      counts,
            'total_voted': len(voted),
            'eligible':    len(eligible),
            'all_voted':   bool(eligible) and all(u in voted for u in eligible),
        }
    return motions


def get_open_motion(case_id):
    """未決（open）の提案を1件返す（なければ None）。votes 付き。"""
    for m in get_motions(case_id):
        if m['status'] == 'open':
            return m
    return None


def get_open_motion_ids_map():
    """一覧用：case_id → open な提案の {motion_no, total_voted, eligible} 辞書。"""
    result = {}
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT m.id, m.case_id, m.motion_no, m.round_no,
                   (SELECT COUNT(*) FROM ext_engagement_motion_votes v
                     WHERE v.motion_id = m.id) AS total_voted
            FROM ext_engagement_motions m
            WHERE m.status = 'open'
        """)
        rows = cursor.fetchall()
    except Exception as e:
        logging.error("get_open_motion_ids_map error: %s", e)
        return result
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()
    for r in rows:
        eligible = get_round_committee_user_ids(r['case_id'], r['round_no'] or 1)
        result[r['case_id']] = {
            'motion_id':   r['id'],
            'motion_no':   r['motion_no'],
            'total_voted': r['total_voted'],
            'eligible':    len(eligible),
        }
    return result


def build_default_decision_text(motion):
    """
    採択した提案から、結論宣言の「結論の要旨・所見」欄に入れる既定文を作る。
    委員長が編集できる下書き。
    """
    dec = motion.get('proposed_decision')
    cond = (motion.get('conditions') or '').strip()
    prop = (motion.get('proposal') or '').strip()
    lines = []
    if dec in ('approve', 'approve_cond'):
        lines.append('連携審査委員会は、本件を承認する。')
        if cond:
            lines.append('ただし、申請者は次の事項を履行すること。')
            lines.append(cond)
            lines.append('履行状況は連携審査委員会へ報告するものとする。')
    elif dec == 'reject':
        lines.append('連携審査委員会は、本件を不承認とする。')
        if prop:
            lines.append(prop)
    else:
        if prop:
            lines.append(prop)
    lines.append(f'（委員長提案 第{motion.get("motion_no")}号 を委員会が承認）')
    return '\n'.join(lines)


# ────────────────────────────────────────────
# Slack 本文
# ────────────────────────────────────────────

def _case_ref_lines(case):
    cid = format_case_id(case['fiscal_year'], case['type_key'], case['seq_no'])
    app_no = f"（申請No：{case['application_no']}）" if case.get('application_no') else ''
    return [f'案件番号：{cid}{app_no}',
            f'課題名／事業名：{case.get("title", "")}']


def build_motion_proposed_message(case, motion, chair_name):
    lines = ['<!channel>',
             f'【連携申請審査】委員長提案（第{motion["motion_no"]}号）が出されました。'
             '委員各位は案件ページで賛否をご表明ください。',
             '']
    lines += _case_ref_lines(case)
    lines += ['',
              f'提案者：{chair_name}',
              f'結論案：{MOTION_DECISION_LABELS.get(motion["proposed_decision"], "")}',
              '─────────────',
              motion['proposal']]
    if motion.get('conditions'):
        lines += ['', '【承認に付す条件】', motion['conditions']]
    if motion.get('reason'):
        lines += ['', '【提案理由】', motion['reason']]
    lines.append('─────────────')
    if motion.get('new_review_end'):
        lines.append(f'審査期限：{fmt_datetime(motion["new_review_end"])} まで延長しました。')
    url = case_page_url(case.get('id'))
    lines.append(f'案件ページ：{url}' if url else '案件ページでご確認ください。')
    return '\n'.join(lines)


def build_motion_closed_message(case, motion, chair_name, action, note):
    label = {'adopt': '採択されました', 'withdraw': '取り下げられました',
             'supersede': '修正のうえ再提案されました'}.get(action, '処理されました')
    lines = ['<!channel>',
             f'【連携申請審査】委員長提案（第{motion["motion_no"]}号）が{label}。',
             '']
    lines += _case_ref_lines(case)
    vs = motion.get('vote_summary') or {}
    c  = vs.get('counts') or {}
    lines += ['',
              f'委員の意見：賛成 {c.get("agree", 0)}・修正意見あり {c.get("amend", 0)}'
              f'・反対 {c.get("disagree", 0)}（{vs.get("total_voted", 0)}/{vs.get("eligible", 0)} 名）']
    if note:
        lines += ['─────────────', note, '─────────────']
    lines.append(f'委員長：{chair_name}')
    url = case_page_url(case.get('id'))
    lines.append(f'案件ページ：{url}' if url else '')
    return '\n'.join(l for l in lines if l is not None)


# ────────────────────────────────────────────
# 内部ユーティリティ
# ────────────────────────────────────────────

def _fetch_case_and_user(cursor, case_id, user_id):
    cursor.execute("SELECT * FROM ext_engagement_cases WHERE id=%s", (case_id,))
    case = cursor.fetchone()
    cursor.execute("SELECT full_name FROM users WHERE id=%s", (user_id,))
    u = cursor.fetchone()
    return case, (u['full_name'] if u else 'Unknown')


def _insert_timeline_comment(cursor, case_id, user_id, ctype, content, now):
    cursor.execute("""
        INSERT INTO ext_engagement_comments
            (case_id, user_id, comment_type, content, is_conclusion, created_at)
        VALUES (%s, %s, %s, %s, 0, %s)
    """, (case_id, user_id, ctype, content, now))


def _motion_summary_text(motion):
    """審議コメント欄に残す提案の要約本文。"""
    parts = [f'第{motion["motion_no"]}号　結論案：'
             f'{MOTION_DECISION_LABELS.get(motion["proposed_decision"], "")}',
             motion['proposal']]
    if motion.get('conditions'):
        parts.append(f'【条件】{motion["conditions"]}')
    if motion.get('reason'):
        parts.append(f'【理由】{motion["reason"]}')
    if motion.get('new_review_end'):
        parts.append(f'審査期限を {fmt_datetime(motion["new_review_end"])} に延長')
    return '\n'.join(parts)


def _close_motion_row(cursor, motion_id, new_status, user_id, note, now):
    cursor.execute("""
        UPDATE ext_engagement_motions
           SET status=%s, closing_note=%s, closed_by=%s, closed_at=%s, updated_at=%s
         WHERE id=%s
    """, (new_status, note or None, user_id, now, now, motion_id))


def _refresh_reminder_flag_after_close(cursor, case_id, round_no):
    """
    提案を閉じた後、案件そのものへの意見表明が既に全員そろっているなら
    all_voted リマインドを「送信済み」扱いにしておく（二重DMを防ぐ）。
    """
    try:
        vs = get_vote_summary(case_id, round_no)
        if vs.get('all_voted'):
            cursor.execute("""
                UPDATE ext_engagement_cases
                   SET last_reminder_kind='all_voted', last_reminder_at=%s
                 WHERE id=%s AND (last_reminder_kind IS NULL OR last_reminder_kind <> 'overdue')
            """, (get_jst_now(), case_id))
    except Exception as e:
        logging.warning("_refresh_reminder_flag_after_close: %s", e)


# ────────────────────────────────────────────
# API: 提案の起案（委員長・代理）
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/case/<int:case_id>/motion', methods=['POST'])
@login_required
def api_motion_create(case_id):
    """
    リクエスト: {
      "proposed_decision": "approve|approve_cond|reject|continue",
      "proposal":       "提案の趣旨（必須）",
      "conditions":     "承認に付す条件（任意）",
      "reason":         "提案理由（任意）",
      "new_review_end": "YYYY-MM-DD HH:MM"（任意。指定時は審査期限を変更）,
      "notify":         true|false（既定 true。Slackチャンネルへ通知）,
      "supersedes_id":  int（任意。修正再提案のとき差し替え元の提案ID）
    }
    """
    user_id = session.get('user_id')
    if not is_effective_chair(user_id, case_id):
        return jsonify({'success': False, 'error': '審査委員長（または代理）のみ操作できます'}), 403
    try:
        data = request.json or {}
        proposed_decision = data.get('proposed_decision', 'approve_cond')
        proposal   = (data.get('proposal') or '').strip()
        conditions = (data.get('conditions') or '').strip()
        reason     = (data.get('reason') or '').strip()
        notify     = data.get('notify', True) is not False
        supersedes_id = data.get('supersedes_id')
        new_review_end = parse_datetime(data.get('new_review_end')) \
            if data.get('new_review_end') else None

        if proposed_decision not in MOTION_DECISION_LABELS:
            return jsonify({'success': False, 'error': '結論案の区分が不正です'}), 400
        if not proposal:
            return jsonify({'success': False, 'error': '提案の趣旨を入力してください'}), 400
        if proposed_decision == 'approve_cond' and not conditions:
            return jsonify({'success': False, 'error': '条件を付す承認では条件文を入力してください'}), 400

        now  = get_jst_now()
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        case, chair_name = _fetch_case_and_user(cursor, case_id, user_id)
        if not case:
            return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
        if case['status'] != 'reviewing':
            return jsonify({'success': False, 'error': '審議中の案件にのみ提案できます'}), 400

        round_no = case.get('review_round') or 1

        # 未決の提案があるか
        cursor.execute("""
            SELECT id, motion_no FROM ext_engagement_motions
             WHERE case_id=%s AND status='open' ORDER BY id DESC LIMIT 1
        """, (case_id,))
        open_row = cursor.fetchone()
        if open_row and (not supersedes_id or int(supersedes_id) != open_row['id']):
            return jsonify({'success': False,
                            'error': f'審議中の提案（第{open_row["motion_no"]}号）があります。'
                                     '採択・取り下げ、または「修正して再提案」を行ってください'}), 400

        # 差し替え元を superseded にする
        if supersedes_id and open_row:
            _close_motion_row(cursor, open_row['id'], 'superseded', user_id,
                              '修正して再提案', now)
            _insert_timeline_comment(
                cursor, case_id, user_id, CT_MOTION_SUPERSEDED,
                f'第{open_row["motion_no"]}号 を修正のうえ再提案', now)

        cursor.execute("SELECT COALESCE(MAX(motion_no),0) AS mx FROM ext_engagement_motions WHERE case_id=%s",
                       (case_id,))
        motion_no = (cursor.fetchone()['mx'] or 0) + 1

        cursor.execute("""
            INSERT INTO ext_engagement_motions
                (case_id, round_no, motion_no, proposed_by, proposed_decision,
                 proposal, conditions, reason, old_review_end, new_review_end,
                 status, supersedes_id, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s)
        """, (case_id, round_no, motion_no, user_id, proposed_decision,
              proposal, conditions or None, reason or None,
              case.get('review_end'), new_review_end,
              (open_row['id'] if (supersedes_id and open_row) else None), now, now))
        motion_id = cursor.lastrowid

        # 審査期限の延長（既存アラート機構へ記録）
        if new_review_end and new_review_end != case.get('review_end'):
            enabled = 0 if case.get('alert_enabled') == 0 else 1
            cursor.execute("""
                UPDATE ext_engagement_cases SET
                    review_end=%s, alert_set_by=%s, alert_set_at=%s,
                    last_reminder_kind=NULL, last_reminder_at=NULL, updated_at=%s
                WHERE id=%s
            """, (new_review_end, user_id, now, now, case_id))
            write_alert_log(conn, case_id, enabled, new_review_end, user_id,
                            alert_role_of(user_id, case_id) or 'chair',
                            'deadline', note=f'委員長提案（第{motion_no}号）により審査期限を変更')
        else:
            # 期限据え置きでも、提案への表明が出そろったら委員長へDMできるようにフラグを戻す
            cursor.execute("""
                UPDATE ext_engagement_cases SET
                    last_reminder_kind=NULL, last_reminder_at=NULL, updated_at=%s
                WHERE id=%s
            """, (now, case_id))

        motion = {'id': motion_id, 'motion_no': motion_no,
                  'proposed_decision': proposed_decision, 'proposal': proposal,
                  'conditions': conditions, 'reason': reason,
                  'new_review_end': new_review_end}
        _insert_timeline_comment(cursor, case_id, user_id, CT_MOTION,
                                 _motion_summary_text(motion), now)
        conn.commit()

        # Slack 通知（既定ON）
        slack_ok = None
        if notify:
            try:
                text   = build_motion_proposed_message(case, motion, chair_name)
                result = slack_notifier.notify_motion(text)
                slack_ok = bool(result.get('ok'))
                notify_log.record(
                    conn, case_id, round_no, notify_log.KIND_MOTION_PROPOSED,
                    notify_log.TARGET_CHANNEL, '委員会チャンネル', slack_ok,
                    error=(None if slack_ok else result.get('error')),
                    summary=f'委員長提案（第{motion_no}号）の通知：{chair_name}',
                    created_by=user_id)
            except Exception as se:
                slack_ok = False
                logging.error("api_motion_create: slack error (case %s): %s", case_id, se)

        return jsonify({'success': True, 'motion_id': motion_id, 'motion_no': motion_no,
                        'slack_notified': notify, 'slack_ok': slack_ok})
    except Exception as e:
        logging.error("api_motion_create error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 提案への意見表明（委員）
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/case/<int:case_id>/motion/<int:motion_id>/vote', methods=['POST'])
@login_required
def api_motion_vote(case_id, motion_id):
    """リクエスト: { "vote": "agree|amend|disagree", "comment": "（任意）" }"""
    user_id = session.get('user_id')
    if not can_access_review(user_id, case_id):
        return jsonify({'success': False, 'error': 'アクセス権限がありません（COI対象または委員会外）'}), 403
    try:
        data    = request.json or {}
        vote    = data.get('vote', '')
        comment = (data.get('comment') or '').strip()
        if vote not in MOTION_VOTE_LABELS:
            return jsonify({'success': False, 'error': '意見区分が不正です'}), 400

        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT m.*, c.status AS case_status FROM ext_engagement_motions m
              JOIN ext_engagement_cases c ON c.id = m.case_id
             WHERE m.id=%s AND m.case_id=%s
        """, (motion_id, case_id))
        m = cursor.fetchone()
        if not m:
            return jsonify({'success': False, 'error': '提案が見つかりません'}), 404
        if m['case_status'] != 'reviewing' or m['status'] != 'open':
            return jsonify({'success': False, 'error': '審議中の提案にのみ意見表明できます'}), 403
        eligible = get_round_committee_user_ids(case_id, m['round_no'] or 1)
        is_chair = is_effective_chair(user_id, case_id)
        if user_id in eligible:
            if vote == 'remark':
                return jsonify({'success': False, 'error': '委員は賛成／修正意見あり／反対のいずれかを選んでください'}), 400
        elif is_chair:
            # 委員長は投票には参加せず、発言のみ
            if vote != 'remark':
                return jsonify({'success': False, 'error': '委員長は投票に参加しません（発言のみ）'}), 400
            if not comment:
                return jsonify({'success': False, 'error': '発言内容を入力してください'}), 400
        else:
            return jsonify({'success': False, 'error': '意見表明できるのは審査委員・委員長のみです'}), 403

        now = get_jst_now()
        cursor.execute("""
            INSERT INTO ext_engagement_motion_votes
                (motion_id, user_id, vote, comment, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE vote=%s, comment=%s, updated_at=%s
        """, (motion_id, user_id, vote, comment or None, now, now,
              vote, comment or None, now))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_motion_vote error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 採択／取り下げ（委員長・代理）
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/case/<int:case_id>/motion/<int:motion_id>/close', methods=['POST'])
@login_required
def api_motion_close(case_id, motion_id):
    """
    リクエスト: { "action": "adopt|withdraw", "note": "（任意）", "notify": true|false }
    修正再提案（supersede）は api_motion_create に supersedes_id を付けて行う。
    """
    user_id = session.get('user_id')
    if not is_effective_chair(user_id, case_id):
        return jsonify({'success': False, 'error': '審査委員長（または代理）のみ操作できます'}), 403
    try:
        data   = request.json or {}
        action = data.get('action', '')
        note   = (data.get('note') or '').strip()
        notify = data.get('notify') is True
        if action not in ('adopt', 'withdraw'):
            return jsonify({'success': False, 'error': '操作が不正です'}), 400

        now    = get_jst_now()
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        case, chair_name = _fetch_case_and_user(cursor, case_id, user_id)
        if not case:
            return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
        if case['status'] != 'reviewing':
            return jsonify({'success': False, 'error': '審議中の案件のみ操作できます'}), 400

        motion = None
        for mm in get_motions(case_id):
            if mm['id'] == motion_id:
                motion = mm; break
        if not motion:
            return jsonify({'success': False, 'error': '提案が見つかりません'}), 404
        if motion['status'] != 'open':
            return jsonify({'success': False, 'error': 'この提案は既に処理済みです'}), 400

        new_status = 'adopted' if action == 'adopt' else 'withdrawn'
        _close_motion_row(cursor, motion_id, new_status, user_id, note, now)

        vs = motion['vote_summary']; c = vs['counts']
        tally = (f'賛成 {c["agree"]}・修正意見あり {c["amend"]}・反対 {c["disagree"]}'
                 f'（{vs["total_voted"]}/{vs["eligible"]} 名）')
        if action == 'adopt':
            body = f'第{motion["motion_no"]}号 を採択　{tally}'
            ctype = CT_MOTION_ADOPTED
        else:
            body = f'第{motion["motion_no"]}号 を取り下げ　{tally}'
            ctype = CT_MOTION_WITHDRAWN
        if note:
            body += '\n' + note
        _insert_timeline_comment(cursor, case_id, user_id, ctype, body, now)
        _refresh_reminder_flag_after_close(cursor, case_id, case.get('review_round') or 1)
        conn.commit()

        slack_ok = None
        if notify:
            try:
                text   = build_motion_closed_message(case, motion, chair_name, action, note)
                result = slack_notifier.notify_motion(text)
                slack_ok = bool(result.get('ok'))
                notify_log.record(
                    conn, case_id, case.get('review_round') or 1,
                    notify_log.KIND_MOTION_CLOSED,
                    notify_log.TARGET_CHANNEL, '委員会チャンネル', slack_ok,
                    error=(None if slack_ok else result.get('error')),
                    summary=f'委員長提案（第{motion["motion_no"]}号）'
                            f'{"採択" if action == "adopt" else "取り下げ"}の通知',
                    created_by=user_id)
            except Exception as se:
                slack_ok = False
                logging.error("api_motion_close: slack error (case %s): %s", case_id, se)

        resp = {'success': True, 'status': new_status,
                'slack_notified': notify, 'slack_ok': slack_ok}
        if action == 'adopt':
            resp['conclude_status'] = MOTION_DECISION_TO_STATUS.get(motion['proposed_decision'])
            resp['decision_text']   = build_default_decision_text(motion)
        return jsonify(resp)
    except Exception as e:
        logging.error("api_motion_close error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()
