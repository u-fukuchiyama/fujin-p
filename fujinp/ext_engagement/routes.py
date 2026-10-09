"""
ext_engagement - ルート定義
External Engagement Review Workflow

ステータス遷移:
  draft      : 登録直後。管理者がCOI確認・委員選定を行う
  reviewing  : 審議送出後。委員がコメント投稿、委員長が結論宣言
  approved   : 承認
  rejected   : 不承認
  withdrawn  : 取り下げ（管理者操作。draft/reviewing どちらからも可）

アクセス制御:
  連携審査_管理者     : 案件登録・編集・COI指定・代理委員長指名・審議送出・取り下げ・委員変更
  連携審査_審査委員会 : reviewing の閲覧・意見表明（COI除く）
  連携審査_審査委員長 : 結論宣言（COI除く）
  regular以上        : 結論後の閲覧
"""
import datetime
import logging
import csv
import io
from pytz import timezone

from flask import (
    render_template, request, jsonify, session, Response, url_for, redirect
)
import mysql.connector

from config import Config
from db import DatabaseConfig, Tables
from decorators import login_required
from auth import redirect_to_dashboard
from ..user_groups.utils import user_is_in_group, get_group_member_ids

from . import ext_engagement_bp

# ────────────────────────────────────────────
# 定数
# ────────────────────────────────────────────

JST = timezone('Asia/Tokyo')

GROUP_ADMIN     = '連携審査_管理者'
GROUP_COMMITTEE = '連携審査_審査委員会'
GROUP_CHAIR     = '連携審査_審査委員長'

# index/Stats の閲覧を許可するグループ
GROUPS_CAN_VIEW_INDEX = [
    GROUP_ADMIN,
    GROUP_COMMITTEE,
    GROUP_CHAIR,
    '執行会議（2026年度）',
]

TYPE_LABELS = {
    'contract_research':      '受託研究',
    'contract_project':       '受託事業',
    'collaborative_research': '共同研究',
    'collaborative_project':  '共同事業',
}

TYPE_SHORT = {
    'contract_research':      '受託研究',
    'contract_project':       '受託事業',
    'collaborative_research': '共同研究',
    'collaborative_project':  '共同事業',
}

STATUS_LABELS = {
    'draft':     '下書き',
    'reviewing': '審議中',
    'approved':  '承認',
    'rejected':  '不承認',
    'withdrawn': '取り下げ',
}

# 委員の意見表明（投票）の3区分。
# 「承認」という票決ではなく、委員の意見の表明である点に注意。
#   approve : 問題なし。委員会として承認でよい
#   abstain : 意見を控える（白票）
#   reject  : 不承認とすべき
VOTE_LABELS = {
    'approve': '承認でよい',
    'abstain': '意見を控える',
    'reject':  '不承認',
}

# 委員長へのリマインド種別（ext_engagement_cases.last_reminder_kind に記録）
#   all_voted : 期日前に対象委員全員の意見が出そろった（ケース①）
#   overdue   : 審査期限を過ぎても未決（ケース②）
REMINDER_KINDS = ('all_voted', 'overdue')

ORG_LABEL = {
    'contract_research':      '申請組織',
    'contract_project':       '申請組織',
    'collaborative_research': '共同機関',
    'collaborative_project':  '共同機関',
}

PERIOD_LABEL = {
    'contract_research':      '委託期間',
    'contract_project':       '委託期間',
    'collaborative_research': '契約期間',
    'collaborative_project':  '契約期間',
}

REP_LABEL = {
    'contract_research':      '受託研究の代表者',
    'contract_project':       '受託事業の代表者',
    'collaborative_research': '共同研究の代表者',
    'collaborative_project':  '共同事業の代表者',
}

CONTENT_LABEL = {
    'contract_research':      '研究内容',
    'contract_project':       '事業内容',
    'collaborative_research': '研究内容',
    'collaborative_project':  '事業内容',
}

# ────────────────────────────────────────────
# ユーティリティ
# ────────────────────────────────────────────

def get_jst_now():
    return datetime.datetime.now(JST).replace(tzinfo=None)


def get_current_fiscal_year():
    now = get_jst_now()
    return now.year if now.month >= 4 else now.year - 1


def get_user_category(user_id):
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT category FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return row['category'] if row else None
    except Exception as e:
        logging.error("get_user_category error: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()

def is_engagement_admin(user_id):
    if get_user_category(user_id) == 'admin':
        return True
    return user_is_in_group(user_id, GROUP_ADMIN)


def is_committee_member(user_id):
    if is_engagement_admin(user_id):
        return True
    return user_is_in_group(user_id, GROUP_COMMITTEE)


def can_view_index(user_id):
    """
    ext_engagement の index/Stats を閲覧できるか。
    admin もしくは GROUPS_CAN_VIEW_INDEX のいずれかに所属していれば True。
    """
    if get_user_category(user_id) == 'admin':
        return True
    for g in GROUPS_CAN_VIEW_INDEX:
        if user_is_in_group(user_id, g):
            return True
    return False

def get_coi_user_ids(case_id):
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT user_id FROM ext_engagement_coi "
            "WHERE case_id = %s AND excluded_roles = 'committee'",
            (case_id,))
        return [r['user_id'] for r in cursor.fetchall()]
    except Exception as e:
        logging.error("get_coi_user_ids error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def is_chair_excluded(user_id, case_id):
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT excluded_roles FROM ext_engagement_coi "
            "WHERE case_id = %s AND user_id = %s",
            (case_id, user_id))
        row = cursor.fetchone()
        if not row:
            return False
        return row['excluded_roles'] in ('committee', 'chair_only')
    except Exception as e:
        logging.error("is_chair_excluded error: %s", e)
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def is_coi(user_id, case_id):
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT excluded_roles FROM ext_engagement_coi "
            "WHERE case_id = %s AND user_id = %s",
            (case_id, user_id))
        row = cursor.fetchone()
        if not row:
            return False
        return row['excluded_roles'] == 'committee'
    except Exception as e:
        logging.error("is_coi error: %s", e)
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def can_access_review(user_id, case_id):
    # 審査委員会メンバー（COI除く）は従来どおり閲覧・発言できる。
    if is_committee_member(user_id) and not is_coi(user_id, case_id):
        return True
    # 委員長は審議中案件を閲覧し、結論宣言の前に発言できる必要がある。
    # 委員会構成員から外れていても（判定する立場として別グループ）、
    # 当該案件でCOI除外されていなければ reviewing にアクセスできる。
    if user_is_in_group(user_id, GROUP_CHAIR) and not is_chair_excluded(user_id, case_id):
        return True
    return False


def can_view_concluded(user_id):
    return get_user_category(user_id) in ('admin', 'regular')


def is_effective_chair(user_id, case_id):
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT acting_chair_user_id FROM ext_engagement_cases WHERE id = %s",
            (case_id,))
        row = cursor.fetchone()
        if not row:
            return False
        acting_chair = row.get('acting_chair_user_id')
    except Exception as e:
        logging.error("is_effective_chair error: %s", e)
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()

    if acting_chair:
        return user_id == acting_chair
    # COI除外された者は admin であっても議長ではない（③のケースを塞ぐ）。
    # 情報閲覧の admin 特権は別判定（is_engagement_admin系）なので影響しない。
    if is_chair_excluded(user_id, case_id):
        return False
    if user_is_in_group(user_id, GROUP_CHAIR):
        return True
    return get_user_category(user_id) == 'admin'


def get_next_seq_no(conn, fiscal_year, type_key):
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT MAX(seq_no) AS max_seq FROM ext_engagement_cases "
        "WHERE fiscal_year = %s AND type_key = %s",
        (fiscal_year, type_key))
    row = cursor.fetchone()
    return (row['max_seq'] or 0) + 1


def format_case_id(fiscal_year, type_key, seq_no):
    return f"{fiscal_year}-{TYPE_SHORT.get(type_key, type_key)}-{seq_no}"


def to_wareki(d):
    if d is None:
        return ''
    if isinstance(d, str):
        try:
            d = datetime.datetime.strptime(d[:10], '%Y-%m-%d').date()
        except Exception:
            return d
    if isinstance(d, datetime.datetime):
        d = d.date()
    if d >= datetime.date(2019, 5, 1):
        year_str = f"令和{d.year - 2018}年"
    elif d >= datetime.date(1989, 1, 8):
        year_str = f"平成{d.year - 1988}年"
    else:
        year_str = f"{d.year}年"
    wd = ['月', '火', '水', '木', '金', '土', '日'][d.weekday()]
    return f"{year_str}{d.month}月{d.day}日（{wd}）"


def fmt_date(d):
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d')
    return str(d)


def fmt_datetime(d):
    """datetime → 'YYYY-MM-DD HH:MM' 文字列。None は空文字。"""
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d %H:%M')
    return str(d)


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def parse_datetime(s):
    """
    'YYYY-MM-DD HH:MM' または 'YYYY-MM-DDTHH:MM'（HTML datetime-local）
    の文字列を naive datetime に変換。日付のみ 'YYYY-MM-DD' なら
    その日の 23:59 とみなす。失敗時は None。
    審査期限（review_end）のパースに使う。
    """
    if not s:
        return None
    s = s.strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    # 日付のみ → その日の 23:59（期限当日いっぱいの意図）
    try:
        d = datetime.datetime.strptime(s[:10], '%Y-%m-%d').date()
        return datetime.datetime(d.year, d.month, d.day, 23, 59, 0)
    except Exception:
        return None


def serialize_case(c):
    # 日付カラム（review_end を除く）は YYYY-MM-DD で文字列化
    for key in ['period_start', 'period_end', 'review_start',
                'approved_date', 'created_at', 'updated_at']:
        if c.get(key):
            c[key] = fmt_date(c[key])
    # review_end は審査期限＝アラート基準日時。DATETIME として扱う。
    #   review_end       : 'YYYY-MM-DD HH:MM'（表示・編集フォーム用）
    #   review_end_date  : 'YYYY-MM-DD'（日付のみ参照したいとき用）
    c['review_end_date'] = fmt_date(c.get('review_end'))
    c['review_end']      = fmt_datetime(c.get('review_end'))
    tk = c.get('type_key', '')
    c['type_label']            = TYPE_LABELS.get(tk, tk)
    c['status_label']          = STATUS_LABELS.get(c.get('status', ''), '')
    c['case_display_id']       = format_case_id(c.get('fiscal_year',''), tk, c.get('seq_no', 0))
    c['org_label']             = ORG_LABEL.get(tk, '申請組織')
    c['period_label']          = PERIOD_LABEL.get(tk, '期間')
    c['rep_label']             = REP_LABEL.get(tk, '代表者')
    c['content_label']         = CONTENT_LABEL.get(tk, '内容')
    c['application_no']        = c.get('application_no') or ''
    c['pre_consultation']      = c.get('pre_consultation') or ''
    c['preferred_rep_user_id'] = c.get('preferred_rep_user_id')
    c['review_round']          = c.get('review_round') or 1
    # 直接経費・間接経費（新フィールド。Noneは0として扱わず、そのままNoneで返す）
    c['direct_fee']   = c.get('direct_fee')    # int or None
    c['indirect_fee'] = c.get('indirect_fee')  # int or None
    # 総額：両方揃っていれば合算、それ以外はNone
    if c['direct_fee'] is not None and c['indirect_fee'] is not None:
        c['total_fee'] = c['direct_fee'] + c['indirect_fee']
    else:
        c['total_fee'] = None
    # 期日アラート（DBは0/1。Noneのときは既定ON扱い）
    c['alert_enabled'] = 0 if c.get('alert_enabled') == 0 else 1
    c['alert_set_by']  = c.get('alert_set_by')
    c['alert_set_at']  = fmt_datetime(c.get('alert_set_at'))
    # Slack通知状態
    c['slack_notified_at']  = fmt_datetime(c.get('slack_notified_at'))
    c['slack_notify_error'] = c.get('slack_notify_error') or ''
    return c


def serialize_comment(cm):
    if cm.get('created_at'):
        cm['created_at'] = cm['created_at'].strftime('%Y-%m-%d %H:%M')
    return cm


def get_committee_snapshots(case_id):
    """全ラウンドの確定委員スナップショットを取得"""
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT s.*, u.full_name
            FROM ext_engagement_committee_snapshot s
            LEFT JOIN users u ON s.user_id = u.id
            WHERE s.case_id = %s
            ORDER BY s.round_no ASC, s.role DESC
        """, (case_id,))
        rows = cursor.fetchall()
        for r in rows:
            if r.get('confirmed_at'):
                r['confirmed_at'] = r['confirmed_at'].strftime('%Y-%m-%d %H:%M')
        return rows
    except Exception as e:
        logging.error("get_committee_snapshots error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# 委員の意見表明（投票）ユーティリティ
# ────────────────────────────────────────────

def get_round_committee_user_ids(case_id, round_no):
    """
    指定ラウンドで「意見表明の対象となる委員」のuser_id集合を返す。
    委員スナップショットのうち role='member' かつ COIで委員除外でない者。
    委員長は判定する立場なので意見表明の母数には含めない。
    """
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT user_id FROM ext_engagement_committee_snapshot
            WHERE case_id = %s AND round_no = %s
              AND role = 'member'
              AND (coi_excluded IS NULL OR coi_excluded <> 'committee')
        """, (case_id, round_no))
        return [r['user_id'] for r in cursor.fetchall()]
    except Exception as e:
        logging.error("get_round_committee_user_ids error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def get_vote_summary(case_id, round_no):
    """
    指定ラウンドの意見表明の全体像を返す。
      counts        : {'approve': n, 'abstain': n, 'reject': n}
      total_voted   : 表明済み委員数
      eligible      : 対象委員数（COI除外を除く）
      all_voted     : 対象委員全員が表明済みか
      votes         : [{user_id, full_name, vote, vote_label, comment, updated_at}, ...]
    """
    summary = {
        'counts':      {'approve': 0, 'abstain': 0, 'reject': 0},
        'total_voted': 0,
        'eligible':    0,
        'all_voted':   False,
        'votes':       [],
    }
    eligible_ids = get_round_committee_user_ids(case_id, round_no)
    summary['eligible'] = len(eligible_ids)
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT v.user_id, v.vote, v.comment, v.updated_at, u.full_name
            FROM ext_engagement_votes v
            LEFT JOIN users u ON v.user_id = u.id
            WHERE v.case_id = %s AND v.round_no = %s
            ORDER BY v.updated_at ASC
        """, (case_id, round_no))
        rows = cursor.fetchall()
    except Exception as e:
        logging.error("get_vote_summary error: %s", e)
        return summary
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()

    voted_ids = set()
    for r in rows:
        v = r['vote']
        if v in summary['counts']:
            summary['counts'][v] += 1
        voted_ids.add(r['user_id'])
        summary['votes'].append({
            'user_id':    r['user_id'],
            'full_name':  r.get('full_name') or '',
            'vote':       v,
            'vote_label': VOTE_LABELS.get(v, v),
            'comment':    r.get('comment') or '',
            'updated_at': r['updated_at'].strftime('%Y-%m-%d %H:%M') if r.get('updated_at') else '',
        })
    summary['total_voted'] = len(voted_ids)
    # 対象委員が1名以上いて、対象委員全員が表明済みなら all_voted
    if eligible_ids:
        summary['all_voted'] = all(uid in voted_ids for uid in eligible_ids)
    return summary


# ────────────────────────────────────────────
# Slack 通知ユーティリティ
#
#   Slack との通信は ext_engagement/slack_notifier.py に集約する
#   （ethics_review と同じ方式）。ここではその関数を呼ぶだけ。
#   - チャンネル投稿：結論宣言の通知
#   - DM：委員長リマインド（メールアドレス照合方式）
#   slack_notifier 側が例外を投げず {'ok': bool, ...} を返す設計なので、
#   ここでも (bool, message) に変換して呼び出し元へ渡す。
# ────────────────────────────────────────────

from . import slack_notifier
from . import notify_log


def post_to_engagement_channel(text):
    """
    連携審査委員会のプライベートチャンネルへ投稿する。
    slack_notifier.notify_conclusion を呼ぶだけの薄いラッパ。
    成功可否を (bool, message) で返す。呼び出し側は失敗しても処理を継続してよい。
    """
    result = slack_notifier.notify_conclusion(text)
    if result.get('ok'):
        return True, result
    return False, result.get('error', 'Slack投稿に失敗しました')


def send_dm_by_email(email, text, log_label='委員長リマインドDM'):
    """
    メールアドレスから Slack ユーザーを引き当ててDMを送る。
    slack_notifier.send_dm_by_email を呼ぶだけの薄いラッパ。
    users.slack_user_id カラムは参照しない（メアド照合方式）。
    成功可否を (bool, message) で返す。
    """
    result = slack_notifier.send_dm_by_email(email, text, log_label=log_label)
    if result.get('ok'):
        return True, result
    return False, result.get('error', 'DM送信に失敗しました')


def build_conclusion_message(case):
    """
    結論宣言時にSlackチャンネルへ投稿する本文を生成する。
    @channel（<!channel>）を冒頭に付け、事務局・委員へ通知が届くようにする。
    case は ext_engagement_cases の1行（dict）。
    """
    type_label      = TYPE_LABELS.get(case['type_key'], '')
    case_display_id = format_case_id(case['fiscal_year'], case['type_key'], case['seq_no'])
    status_label    = STATUS_LABELS.get(case.get('status', ''), '')
    app_no          = f"（申請No：{case['application_no']}）" if case.get('application_no') else ''
    period_str = ''
    if case.get('period_start'):
        period_str = f"{to_wareki(case.get('period_start'))}～{to_wareki(case.get('period_end'))}"
    rep_str = '　'.join(filter(None, [case.get('faculty_dept'), case.get('representative_name')]))

    lines = [
        '<!channel>',
        f'【連携申請審査 結論】{case_display_id}{app_no}',
        f'種別：{type_label}',
        f'課題名／事業名：{case.get("title", "")}',
        f'{ORG_LABEL.get(case["type_key"], "申請組織")}：{case.get("org_name", "")}',
    ]
    if period_str:
        lines.append(f'{PERIOD_LABEL.get(case["type_key"], "期間")}：{period_str}')
    if rep_str:
        lines.append(f'{REP_LABEL.get(case["type_key"], "代表者")}：{rep_str}')
    lines.append(f'審査結果：★{status_label}★')
    lines.append(f'承認日：{to_wareki(case.get("approved_date"))}')
    if case.get('decision'):
        lines.append(f'所見：{case["decision"]}')
    url = case_page_url(case.get('id'))          # ← 追加
    if url:                                       # ← 追加
        lines.append(f'案件ページ：{url}')        # ← 追加
    chair = getattr(Config, 'EXT_ENGAGEMENT_CHAIR_NAME', '')
    lines.append(f'（連携審査委員長・{chair}）' if chair else '（連携審査委員長）')
    return '\n'.join(lines)


def _case_summary_lines(case):
    """
    案件の基本情報を Slack 本文用の行リストにする。
    案件登録通知・審議開始宣言通知で共通に使う。
    """
    type_label      = TYPE_LABELS.get(case['type_key'], '')
    case_display_id = format_case_id(case['fiscal_year'], case['type_key'], case['seq_no'])
    app_no = f"（申請No：{case['application_no']}）" if case.get('application_no') else ''
    period_str = ''
    if case.get('period_start'):
        period_str = f"{to_wareki(case.get('period_start'))}～{to_wareki(case.get('period_end'))}"
    rep_str = '　'.join(filter(None, [case.get('faculty_dept'),
                                      case.get('representative_name')]))
    lines = [
        f'案件番号：{case_display_id}{app_no}',
        f'種別：{type_label}',
        f'課題名／事業名：{case.get("title", "")}',
        f'{ORG_LABEL.get(case["type_key"], "申請組織")}：{case.get("org_name", "")}',
    ]
    if period_str:
        lines.append(f'{PERIOD_LABEL.get(case["type_key"], "期間")}：{period_str}')
    if rep_str:
        lines.append(f'{REP_LABEL.get(case["type_key"], "代表者")}：{rep_str}')
    return lines

def case_page_url(case_id):
    """
    案件詳細ページの絶対URLを返す。
    通常はリクエストコンテキスト内で request.host から組み立てる。
    リクエスト外では config.py の BASE_URL を使う
    （check_deadlines.py の case_app_url と同じ流儀）。
    """
    try:
        path = url_for('ext_engagement.detail', case_id=case_id)
        return f"https://{request.host}{path}"
    except Exception:
        pass
    base = getattr(Config, 'BASE_URL', '').rstrip('/')
    if base:
        return f"{base}/ext_engagement/case/{case_id}"
    return None

def build_case_registered_dm(case):
    """
    事務局が新しい案件を登録したとき、委員長へ送るDM本文を生成する。
    case は ext_engagement_cases の1行（dict）。
    """
    lines = ['【連携申請審査】新しい審査案件が登録されました。', '']
    lines += _case_summary_lines(case)
    url = case_page_url(case.get('id'))          # ← 追加
    if url:                                       # ← 追加
        lines.append(f'案件ページ：{url}')        # ← 追加
    lines += [
        '',
        '事務局による委員候補の準備が整いしだい、審議開始を宣言できます。',
        '案件の内容をご確認ください。',
    ]
    return '\n'.join(lines)


def build_review_started_message(case, chair_message='', is_rereview=False):
    """
    審議開始宣言のとき、連携審査委員会チャンネルへ投稿する本文を生成する。
    冒頭に <!channel> を付け、委員全員へ通知が届くようにする。

    Args:
        case         : ext_engagement_cases の1行（dict）
        chair_message: 宣言者が添えるメッセージ（任意）
        is_rereview  : True なら委員構成を変更しての再宣言。
                       文面を「委員構成を変更して審議を再開」にする。
    """
    if is_rereview:
        head = '【連携申請審査】委員構成を変更し、審議を再開します。'
    else:
        head = '【連携申請審査】審議を開始します。'

    lines = ['<!channel>', head, '']
    lines += _case_summary_lines(case)
    if case.get('review_end'):
        lines.append(f'審査期限：{fmt_datetime(case.get("review_end"))}')
    url = case_page_url(case.get('id'))          # ← 追加
    if url:                                       # ← 追加
        lines.append(f'案件ページ：{url}')        # ← 追加
    if chair_message:
        lines += ['', '─────────────', chair_message]
    lines += [
        '',
        '審査委員の皆さまは、案件ページから内容のご確認と意見表明をお願いします。',
    ]
    return '\n'.join(lines)


def build_review_comment_message(case, author_name, comment_type, content):
    """
    審議コメントの投稿を委員会チャンネルへ通知する本文を生成する。
    冒頭に <!channel> を付け、委員全員へ通知が届くようにする。

    通知には、どの案件かのリファレンス（案件番号・課題名）、
    投稿した委員名、コメント本文を載せる。

    Args:
        case         : ext_engagement_cases の1行（dict）
        author_name  : 投稿者（委員）の氏名
        comment_type : 'opinion'（意見）/ 'question'（質問）/ 'answer'（回答）
        content      : コメント本文
    """
    type_label = {
        'opinion':  '意見',
        'question': '質問',
        'answer':   '回答',
    }.get(comment_type, 'コメント')
    case_display_id = format_case_id(case['fiscal_year'],
                                     case['type_key'], case['seq_no'])
    app_no = f"（申請No：{case['application_no']}）" if case.get('application_no') else ''

    lines = [
        '<!channel>',
        f'【連携申請審査】審議コメント（{type_label}）が投稿されました。',
        '',
        f'案件番号：{case_display_id}{app_no}',
        f'課題名／事業名：{case.get("title", "")}',
        '',
        f'委員：{author_name}',
        '─────────────',
        content,
        '─────────────',
    ]
    url = case_page_url(case.get('id'))
    lines.append(f'案件ページでご確認ください：{url}' if url
                 else '案件ページでご確認ください。')
    return '\n'.join(lines)


def get_chair_emails(case_id):
    """
    案件の「実質的な委員長」のメールアドレス一覧を返す。
    acting_chair_user_id があればその1名、なければ委員長プール全員。
    案件登録通知のDM宛先として使う（メアド照合方式）。
    """
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        # acting_chair の有無を確認
        cursor.execute(
            "SELECT acting_chair_user_id FROM ext_engagement_cases WHERE id=%s",
            (case_id,))
        row = cursor.fetchone()
        acting = row.get('acting_chair_user_id') if row else None
        if acting:
            chair_ids = [acting]
        else:
            chair_ids = get_group_member_ids(GROUP_CHAIR)
        if not chair_ids:
            return []
        fmt = ','.join(['%s'] * len(chair_ids))
        cursor.execute(
            f"SELECT email FROM users WHERE id IN ({fmt})", chair_ids)
        return [r['email'] for r in cursor.fetchall() if r.get('email')]
    except Exception as e:
        logging.error("get_chair_emails error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def record_slack_result(case_id, ok, detail):
    """
    Slack投稿の結果を ext_engagement_cases に記録する。
    slack_notified_at / slack_notify_error の2カラムを更新。
    """
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        if ok:
            cursor.execute("""
                UPDATE ext_engagement_cases
                SET slack_notified_at = %s, slack_notify_error = NULL
                WHERE id = %s
            """, (get_jst_now(), case_id))
        else:
            cursor.execute("""
                UPDATE ext_engagement_cases
                SET slack_notify_error = %s
                WHERE id = %s
            """, (str(detail)[:255], case_id))
        conn.commit()
    except Exception as e:
        logging.error("record_slack_result error: %s", e)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# 期日アラート設定のユーティリティ
# ────────────────────────────────────────────

def alert_role_of(user_id, case_id):
    """
    アラートを設定した人の立場を返す。chair（委員長）優先、次に secretariat（事務局）。
    どちらでもなければ 'other'。ログの changed_role に記録する。
    """
    if is_effective_chair(user_id, case_id):
        return 'chair'
    if is_engagement_admin(user_id):
        return 'secretariat'
    return 'other'


def write_alert_log(conn, case_id, enabled, review_end, user_id,
                    role, action, note=None):
    """
    アラート設定の操作を ext_engagement_alert_log に1件追記する。
    conn は呼び出し側のコネクションを使い回す（commitは呼び出し側）。
      enabled    : 設定後のアラート状態（0/1）
      review_end : 設定後の審査期限（datetime または None）
      action     : 'send_to_review' / 'toggle' / 'deadline'
    """
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ext_engagement_alert_log
            (case_id, enabled, review_end, changed_by, changed_role,
             action, note, changed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (case_id, 1 if enabled else 0, review_end, user_id,
          role, action, note, get_jst_now()))


def update_alert_setter(conn, case_id, user_id):
    """
    ext_engagement_cases の alert_set_by / alert_set_at を更新する。
    「いつ・誰が最後にアラートを設定したか」を案件画面で示すための最新値。
    """
    cur = conn.cursor()
    cur.execute("""
        UPDATE ext_engagement_cases
        SET alert_set_by = %s, alert_set_at = %s
        WHERE id = %s
    """, (user_id, get_jst_now(), case_id))


def get_alert_logs(case_id):
    """指定案件のアラート設定履歴を新しい順に返す（画面表示用）。"""
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT l.*, u.full_name AS changed_by_name
            FROM ext_engagement_alert_log l
            LEFT JOIN users u ON l.changed_by = u.id
            WHERE l.case_id = %s
            ORDER BY l.changed_at DESC, l.id DESC
        """, (case_id,))
        rows = cursor.fetchall()
        for r in rows:
            r['changed_at'] = fmt_datetime(r.get('changed_at'))
            r['review_end'] = fmt_datetime(r.get('review_end'))
            r['enabled']    = 1 if r.get('enabled') else 0
        return rows
    except Exception as e:
        logging.error("get_alert_logs error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# ページルート
# ────────────────────────────────────────────

@ext_engagement_bp.route('/')
@login_required
def index():
    user_id = session.get('user_id')
    if not can_view_index(user_id):
        return "アクセス権限がありません", 403
    return render_template(
        'ext_engagement/index.html',
        current_user_id=user_id,
        is_admin=is_engagement_admin(user_id),
        # 委員長プールに属しているか（案件横断の判定）。
        # 下書きタブの表示可否に使う。委員長は審議開始を宣言できるため
        # 下書き案件にアクセスできる必要がある。
        is_chair=user_is_in_group(user_id, GROUP_CHAIR),
        # 年度報告：事務局は管理者用，regular 以上はゲスト用（承認案件のみ）
        can_view_report=(is_engagement_admin(user_id) or can_view_concluded(user_id)),
        can_view_stats=True,   # ここまで来られた人は Stats 表示可
        current_fy=get_current_fiscal_year(),
        type_labels=TYPE_LABELS,
        status_labels=STATUS_LABELS,
    )


@ext_engagement_bp.route('/case/<int:case_id>')
@login_required
def detail(case_id):
    user_id = session.get('user_id')
    return render_template(
        'ext_engagement/detail.html',
        case_id=case_id,
        current_user_id=user_id,
        is_admin=is_engagement_admin(user_id),
        is_chair=is_effective_chair(user_id, case_id),
        is_committee=is_committee_member(user_id),
        type_labels=TYPE_LABELS,
        status_labels=STATUS_LABELS,
    )


@ext_engagement_bp.route('/report')
@login_required
def report():
    """年度なしの URL は今年度の年度報告（/report/<年度>）へ転送する。"""
    return redirect(url_for('ext_engagement.report_year',
                            fiscal_year=get_current_fiscal_year()))


@ext_engagement_bp.route('/report/<int:fiscal_year>')
@login_required
def report_year(fiscal_year):
    user_id = session.get('user_id')
    # 連携審査_管理者は管理者用の年度報告，regular 以上はゲスト用の年度報告を見る
    is_admin_v = is_engagement_admin(user_id)
    if not (is_admin_v or can_view_concluded(user_id)):
        return "アクセス権限がありません", 403
    current_fy = get_current_fiscal_year()
    # 年度の選択肢：今年度から6年分。URL の年度がその外なら加える
    fy_options = list(range(current_fy, current_fy - 6, -1))
    if fiscal_year not in fy_options:
        fy_options = sorted(set(fy_options + [fiscal_year]), reverse=True)
    return render_template(
        'ext_engagement/report.html',
        current_user_id=user_id,
        is_admin=is_admin_v,
        can_view_index=can_view_index(user_id),
        current_fy=current_fy,
        fiscal_year=fiscal_year,
        fy_options=fy_options,
        type_labels=TYPE_LABELS,
        status_labels=STATUS_LABELS,
    )


@ext_engagement_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()


# ────────────────────────────────────────────
# API: 案件
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/cases', methods=['GET'])
@login_required
def api_cases():
    try:
        user_id     = session.get('user_id')
        fiscal_year = request.args.get('fiscal_year', type=int)
        type_key    = request.args.get('type_key', '')
        status      = request.args.get('status', '')

        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        where  = ['1=1']; params = []
        if fiscal_year: where.append('fiscal_year = %s'); params.append(fiscal_year)
        if type_key:    where.append('type_key = %s');    params.append(type_key)
        if status:      where.append('status = %s');      params.append(status)

        cursor.execute(
            f"SELECT * FROM ext_engagement_cases "
            f"WHERE {' AND '.join(where)} ORDER BY fiscal_year DESC, type_key, seq_no DESC",
            params)
        all_cases = cursor.fetchall()

        visible = []
        for c in all_cases:
            if c['status'] == 'draft':
                # 下書きは事務局（管理者）と委員長が見られる。
                # 委員長は審議開始を宣言できるため、下書き案件に
                # アクセスできる必要がある。
                if is_engagement_admin(user_id) or \
                   user_is_in_group(user_id, GROUP_CHAIR):
                    visible.append(c)
            elif c['status'] == 'reviewing':
                if can_access_review(user_id, c['id']):
                    visible.append(c)
            else:
                if can_view_concluded(user_id):
                    visible.append(c)

        open_map = motions.get_open_motion_ids_map()
        out = []
        for c in visible:
            sc = serialize_case(c)
            sc['open_motion'] = open_map.get(c['id'])
            out.append(sc)
        return jsonify({'success': True, 'cases': out})

    except Exception as e:
        logging.error("api_cases error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ext_engagement_bp.route('/api/case/<int:case_id>', methods=['GET'])
@login_required
def api_case_get(case_id):
    try:
        user_id = session.get('user_id')
        conn    = mysql.connector.connect(**DatabaseConfig.default())
        cursor  = conn.cursor(dictionary=True)

        cursor.execute("SELECT * FROM ext_engagement_cases WHERE id = %s", (case_id,))
        case = cursor.fetchone()
        if not case:
            return jsonify({'success': False, 'error': '案件が見つかりません'}), 404

        status = case['status']
        if status == 'draft':
            # 下書きは事務局（管理者）と委員長がアクセスできる。
            # 委員長は審議開始を宣言できるため。
            if not (is_engagement_admin(user_id) or
                    user_is_in_group(user_id, GROUP_CHAIR)):
                return jsonify({'success': False, 'error': 'アクセス権限がありません'}), 403
        elif status == 'reviewing':
            if not can_access_review(user_id, case_id):
                return jsonify({'success': False, 'error': 'アクセス権限がありません'}), 403
        else:
            if not can_view_concluded(user_id):
                return jsonify({'success': False, 'error': 'アクセス権限がありません'}), 403

        case = serialize_case(case)

        cursor.execute("""
            SELECT c.*, u.full_name AS author_name
            FROM ext_engagement_comments c
            LEFT JOIN users u ON c.user_id = u.id
            WHERE c.case_id = %s ORDER BY c.created_at ASC
        """, (case_id,))
        comments = [serialize_comment(cm) for cm in cursor.fetchall()]

        coi_info = []
        if is_engagement_admin(user_id):
            cursor.execute("""
                SELECT ec.user_id, u.full_name, ec.reason, ec.excluded_roles, ec.created_at
                FROM ext_engagement_coi ec
                LEFT JOIN users u ON ec.user_id = u.id
                WHERE ec.case_id = %s
            """, (case_id,))
            coi_info = cursor.fetchall()
            for r in coi_info:
                if r.get('created_at'):
                    r['created_at'] = r['created_at'].strftime('%Y-%m-%d %H:%M')

        acting_chair_info = None
        if case.get('acting_chair_user_id'):
            cursor.execute(
                "SELECT id, full_name FROM users WHERE id = %s",
                (case['acting_chair_user_id'],))
            acting_chair_info = cursor.fetchone()

        preferred_rep_info = None
        if case.get('preferred_rep_user_id'):
            cursor.execute(
                "SELECT id, full_name FROM users WHERE id = %s",
                (case['preferred_rep_user_id'],))
            preferred_rep_info = cursor.fetchone()

        snapshots = get_committee_snapshots(case_id)

        # ── 意見表明（投票）の全体像（現ラウンド） ──
        round_no     = case.get('review_round') or 1
        vote_summary = get_vote_summary(case_id, round_no)
        # 閲覧者自身の票
        my_vote = None
        for v in vote_summary['votes']:
            if v['user_id'] == user_id:
                my_vote = {'vote': v['vote'], 'comment': v['comment']}
                break

        # ── アラート設定情報（委員長・事務局のみに見せる運用情報） ──
        is_admin_v = is_engagement_admin(user_id)
        is_chair_v = is_effective_chair(user_id, case_id)
        alert_logs       = []
        alert_set_by_name = ''
        if is_admin_v or is_chair_v:
            alert_logs = get_alert_logs(case_id)
            if case.get('alert_set_by'):
                cursor.execute(
                    "SELECT full_name FROM users WHERE id = %s",
                    (case['alert_set_by'],))
                arow = cursor.fetchone()
                alert_set_by_name = arow['full_name'] if arow else ''

        # ── 委員長提案（motion）：現ラウンドの提案一覧・未決提案・採択済み提案 ──
        motions_all    = motions.get_motions(case_id)
        motions_round  = [m for m in motions_all if (m.get('round_no') or 1) == round_no]
        open_motion    = next((m for m in motions_all if m['status'] == 'open'), None)
        adopted_motion = next((m for m in motions_round if m['status'] == 'adopted'), None)
        my_motion_vote = None
        if open_motion:
            for mv in open_motion.get('votes', []):
                if mv['user_id'] == user_id:
                    my_motion_vote = {'vote': mv['vote'], 'comment': mv['comment']}
                    break
        adopted_decision_text = (motions.build_default_decision_text(adopted_motion)
                                 if adopted_motion else '')

        return jsonify({
            'success': True,
            'case': case,
            'comments': comments,
            'motions': motions_all,
            'open_motion': open_motion,
            'adopted_motion': adopted_motion,
            'adopted_decision_text': adopted_decision_text,
            'my_motion_vote': my_motion_vote,
            'motion_labels': {
                'decision': motions.MOTION_DECISION_LABELS,
                'vote':     motions.MOTION_VOTE_LABELS,
                'status':   motions.MOTION_STATUS_LABELS,
                'to_status': motions.MOTION_DECISION_TO_STATUS,
            },
            'coi_info': coi_info,
            'acting_chair_info': acting_chair_info,
            'preferred_rep_info': preferred_rep_info,
            'snapshots': snapshots,
            'vote_summary': vote_summary,
            'my_vote': my_vote,
            'vote_labels': VOTE_LABELS,
            'alert_logs': alert_logs,
            'alert_set_by_name': alert_set_by_name,
            'viewer': {
                'is_admin':     is_admin_v,
                'is_chair':     is_chair_v,
                'is_committee': is_committee_member(user_id),
                'is_coi':       is_coi(user_id, case_id),
            }
        })

    except Exception as e:
        logging.error("api_case_get error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ext_engagement_bp.route('/api/case/new', methods=['POST'])
@login_required
def api_case_new():
    user_id = session.get('user_id')
    if not is_engagement_admin(user_id):
        return jsonify({'success': False, 'error': '連携審査_管理者のみ登録できます'}), 403
    try:
        data        = request.json
        now         = get_jst_now()
        type_key    = data.get('type_key', '').strip()
        fiscal_year = data.get('fiscal_year', get_current_fiscal_year())
        title       = data.get('title', '').strip()
        org_name    = data.get('org_name', '').strip()

        if not type_key or not title or not org_name:
            return jsonify({'success': False, 'error': '種別・課題名・申請組織は必須です'}), 400
        if type_key not in TYPE_LABELS:
            return jsonify({'success': False, 'error': '種別が不正です'}), 400

        preferred_rep_user_id = data.get('preferred_rep_user_id')
        if preferred_rep_user_id is not None:
            try:
                preferred_rep_user_id = int(preferred_rep_user_id)
            except (ValueError, TypeError):
                preferred_rep_user_id = None

        conn   = mysql.connector.connect(**DatabaseConfig.default())
        seq_no = get_next_seq_no(conn, fiscal_year, type_key)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO ext_engagement_cases (
                fiscal_year, type_key, seq_no, application_no,
                title, org_name, content, is_new_application,
                period_start, period_end, research_fee,
                direct_fee, indirect_fee,
                representative_name, faculty_dept, preferred_rep_user_id,
                doc_url, pre_consultation,
                review_start, review_end,
                status, review_round,
                created_by, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                'draft', 1,
                %s, %s, %s
            )
        """, (
            fiscal_year, type_key, seq_no,
            data.get('application_no', '').strip() or None,
            title, org_name, data.get('content', ''),
            1 if data.get('is_new_application', True) else 0,
            parse_date(data.get('period_start')),
            parse_date(data.get('period_end')),
            data.get('research_fee', ''),
            data.get('direct_fee') if data.get('direct_fee') != '' else None,
            data.get('indirect_fee') if data.get('indirect_fee') != '' else None,
            data.get('representative_name', ''),
            data.get('faculty_dept', ''),
            preferred_rep_user_id,
            data.get('doc_url', ''),
            data.get('pre_consultation', '').strip() or None,
            parse_date(data.get('review_start')),
            parse_datetime(data.get('review_end')),
            user_id, now, now,
        ))
        conn.commit()
        new_case_id = cursor.lastrowid

        # ── 委員長へ案件登録をDMで通知 ──
        # 委員長プール全員のメールアドレスへ送る（メアド照合方式）。
        # 送信失敗は案件登録の成否に影響させない。
        try:
            cur2 = conn.cursor(dictionary=True)
            cur2.execute("SELECT * FROM ext_engagement_cases WHERE id=%s",
                         (new_case_id,))
            new_case = cur2.fetchone()
            cur2.close()
            if new_case:
                dm_text = build_case_registered_dm(new_case)
                chair_emails = get_chair_emails(new_case_id)
                if not chair_emails:
                    logging.warning("api_case_new: 案件 %s — 委員長のメール"
                                    "アドレスが取得できず登録通知を送れません",
                                    new_case_id)
                for email in chair_emails:
                    ok, res = send_dm_by_email(
                        email, dm_text, log_label='案件登録通知DM')
                    notify_log.record(
                        conn, new_case_id, new_case.get('review_round', 1),
                        notify_log.KIND_CASE_REGISTERED, notify_log.TARGET_DM,
                        f'委員長（{email}）', ok,
                        error=(None if ok else res),
                        summary='新しい審査案件が登録されました',
                        created_by=user_id)
                    if ok:
                        logging.info("api_case_new: 案件 %s 登録を委員長(%s)へ通知",
                                     new_case_id, email)
                    else:
                        logging.warning("api_case_new: 案件 %s 登録通知の送信"
                                        "失敗(%s): %s", new_case_id, email, res)
        except Exception as ne:
            logging.error("api_case_new: 登録通知で例外（案件 %s）: %s",
                          new_case_id, ne)

        return jsonify({
            'success': True,
            'id': new_case_id,
            'case_display_id': format_case_id(fiscal_year, type_key, seq_no),
            'seq_no': seq_no,
        })
    except Exception as e:
        logging.error("api_case_new error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ext_engagement_bp.route('/api/case/<int:case_id>/update', methods=['POST'])
@login_required
def api_case_update(case_id):
    user_id = session.get('user_id')
    if not is_engagement_admin(user_id):
        return jsonify({'success': False, 'error': '連携審査_管理者のみ編集できます'}), 403
    try:
        data = request.json
        now  = get_jst_now()

        preferred_rep_user_id = data.get('preferred_rep_user_id')
        if preferred_rep_user_id is not None:
            try:
                preferred_rep_user_id = int(preferred_rep_user_id)
            except (ValueError, TypeError):
                preferred_rep_user_id = None

        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 変更前の review_end を控えておく（期限変更ならリマインド記録をリセット）
        cursor.execute(
            "SELECT review_end FROM ext_engagement_cases WHERE id=%s", (case_id,))
        before = cursor.fetchone()
        old_review_end = before['review_end'] if before else None
        new_review_end = parse_datetime(data.get('review_end'))

        cursor.execute("""
            UPDATE ext_engagement_cases SET
                application_no=%s,
                title=%s, org_name=%s, content=%s, is_new_application=%s,
                period_start=%s, period_end=%s, research_fee=%s,
                direct_fee=%s, indirect_fee=%s,
                representative_name=%s, faculty_dept=%s,
                preferred_rep_user_id=%s,
                doc_url=%s, pre_consultation=%s,
                review_start=%s, review_end=%s,
                updated_at=%s
            WHERE id=%s
        """, (
            data.get('application_no', '').strip() or None,
            data.get('title', ''), data.get('org_name', ''),
            data.get('content', ''),
            1 if data.get('is_new_application', True) else 0,
            parse_date(data.get('period_start')),
            parse_date(data.get('period_end')),
            data.get('research_fee', ''),
            data.get('direct_fee') if data.get('direct_fee') != '' else None,
            data.get('indirect_fee') if data.get('indirect_fee') != '' else None,
            data.get('representative_name', ''),
            data.get('faculty_dept', ''),
            preferred_rep_user_id,
            data.get('doc_url', ''),
            data.get('pre_consultation', '').strip() or None,
            parse_date(data.get('review_start')),
            new_review_end,
            now, case_id,
        ))

        # 審査期限が変わった場合は、リマインド記録をクリアして
        # 新しい期限で再びアラートが効くようにする（期間延長への対応）。
        # 併せてアラート設定履歴に「期限変更」として記録する。
        if old_review_end != new_review_end:
            cursor.execute("""
                UPDATE ext_engagement_cases
                SET last_reminder_kind = NULL, last_reminder_at = NULL
                WHERE id = %s
            """, (case_id,))
            # 現在のアラートON/OFF状態を取得してログに残す
            cursor.execute(
                "SELECT alert_enabled FROM ext_engagement_cases WHERE id=%s",
                (case_id,))
            arow = cursor.fetchone()
            cur_enabled = 0 if (arow and arow.get('alert_enabled') == 0) else 1
            write_alert_log(
                conn, case_id, cur_enabled, new_review_end, user_id,
                alert_role_of(user_id, case_id), 'deadline',
                note='案件編集により審査期限を変更')
            update_alert_setter(conn, case_id, user_id)

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_case_update error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ext_engagement_bp.route('/api/case/<int:case_id>/send_to_review', methods=['POST'])
@login_required
def api_send_to_review(case_id):
    """
    審議開始宣言（draft → reviewing）。委員確定スナップショットを記録する。
    実行できるのは委員長（または代理）と事務局（管理者）の両方。

    宣言時に：
      - 期日アラートのON/OFFと審査期限（アラート基準日時）を設定・記録する
      - 宣言者が添えるメッセージを受け取る（任意）
      - 委員会チャンネルへの通知を行う（opt out。既定で通知する）
        初回の宣言（review_round=1）   → 「審議を開始します」
        委員変更後の再宣言（round>=2）→ 「委員構成を変更し、審議を再開します」

    リクエスト: {
        "alert_enabled":  true|false,           # 既定 true（opt out）
        "review_end":     "YYYY-MM-DD HH:MM",   # 審査期限（アラート基準日時）
        "chair_message":  "（任意）宣言に添えるメッセージ",
        "notify_committee": true|false          # 既定 true（opt out）
    }
    """
    user_id = session.get('user_id')
    # 委員長（または代理）と事務局のどちらでも審議開始を宣言できる
    if not (is_effective_chair(user_id, case_id) or is_engagement_admin(user_id)):
        return jsonify({'success': False,
                        'error': '委員長または事務局のみ審議開始を宣言できます'}), 403
    try:
        data          = request.json or {}
        # アラートは既定ON（opt out）。明示的に false のときだけOFF。
        alert_enabled = 0 if data.get('alert_enabled') is False else 1
        review_end    = parse_datetime(data.get('review_end'))
        chair_message = (data.get('chair_message') or '').strip()
        # 委員会への通知も既定ON（opt out）。明示的に false のときだけ送らない。
        notify_committee = data.get('notify_committee') is not False

        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT status, review_round, acting_chair_user_id FROM ext_engagement_cases WHERE id=%s",
            (case_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
        if row['status'] != 'draft':
            return jsonify({'success': False, 'error': '下書き状態の案件のみ審議開始を宣言できます'}), 400

        round_no        = row['review_round']
        acting_chair_id = row.get('acting_chair_user_id')
        now             = get_jst_now()
        # round_no が 2 以上 ＝ 一度は委員変更を経た再宣言
        is_rereview     = (round_no or 1) >= 2

        member_ids    = get_group_member_ids(GROUP_COMMITTEE)
        chair_ids     = get_group_member_ids(GROUP_CHAIR)
        ins           = conn.cursor()

        def get_coi_excluded(uid):
            c2 = conn.cursor(dictionary=True)
            c2.execute(
                "SELECT excluded_roles FROM ext_engagement_coi WHERE case_id=%s AND user_id=%s",
                (case_id, uid))
            r = c2.fetchone()
            return r['excluded_roles'] if r else None

        # 委員長スナップショット
        effective_chair_ids = [acting_chair_id] if acting_chair_id else chair_ids
        for uid in effective_chair_ids:
            ins.execute("""
                INSERT INTO ext_engagement_committee_snapshot
                    (case_id, round_no, user_id, role, coi_excluded, confirmed_at, confirmed_by)
                VALUES (%s, %s, %s, 'chair', %s, %s, %s)
            """, (case_id, round_no, uid, get_coi_excluded(uid), now, user_id))

        # 委員スナップショット
        for uid in member_ids:
            ins.execute("""
                INSERT INTO ext_engagement_committee_snapshot
                    (case_id, round_no, user_id, role, coi_excluded, confirmed_at, confirmed_by)
                VALUES (%s, %s, %s, 'member', %s, %s, %s)
            """, (case_id, round_no, uid, get_coi_excluded(uid), now, user_id))

        # ステータス更新＋アラート設定（ON/OFF・審査期限・設定者）
        upd = conn.cursor()
        upd.execute("""
            UPDATE ext_engagement_cases SET
                status='reviewing', updated_at=%s,
                alert_enabled=%s, review_end=%s,
                alert_set_by=%s, alert_set_at=%s,
                last_reminder_kind=NULL, last_reminder_at=NULL
            WHERE id=%s
        """, (now, alert_enabled, review_end, user_id, now, case_id))

        # アラート設定履歴に「送出時設定」を記録
        note = ('審議開始宣言時にアラートONで設定' if alert_enabled
                else '審議開始宣言時にアラートOFFで設定')
        write_alert_log(
            conn, case_id, alert_enabled, review_end, user_id,
            alert_role_of(user_id, case_id), 'send_to_review', note=note)

        conn.commit()

        # ── 委員会チャンネルへ審議開始を通知（opt out。既定で通知する） ──
        slack_ok    = None   # None=通知しない設定 / True / False
        slack_error = ''
        if notify_committee:
            try:
                cur3 = conn.cursor(dictionary=True)
                cur3.execute("SELECT * FROM ext_engagement_cases WHERE id=%s",
                             (case_id,))
                started = cur3.fetchone()
                cur3.close()
                if started:
                    text = build_review_started_message(
                        started, chair_message=chair_message,
                        is_rereview=is_rereview)
                    result = slack_notifier.notify_review_started(text)
                    slack_ok = bool(result.get('ok'))
                    if not slack_ok:
                        slack_error = str(result.get('error', ''))
                        logging.warning("send_to_review: 審議開始通知の送信失敗"
                                        "（案件 %s）: %s", case_id, slack_error)
                    notify_log.record(
                        conn, case_id, round_no,
                        notify_log.KIND_REVIEW_STARTED,
                        notify_log.TARGET_CHANNEL, '委員会チャンネル',
                        slack_ok, error=(None if slack_ok else slack_error),
                        summary=('委員構成を変更し、審議を再開しました'
                                 if is_rereview else '審議を開始しました'),
                        created_by=user_id)
            except Exception as se:
                slack_ok = False
                slack_error = str(se)
                logging.error("send_to_review: 審議開始通知で例外（案件 %s）: %s",
                              case_id, se)
                try:
                    notify_log.record(
                        conn, case_id, round_no,
                        notify_log.KIND_REVIEW_STARTED,
                        notify_log.TARGET_CHANNEL, '委員会チャンネル',
                        False, error=slack_error,
                        summary='審議開始の通知', created_by=user_id)
                except Exception:
                    pass

        return jsonify({
            'success': True,
            'round_no': round_no,
            'is_rereview': is_rereview,
            'slack_notified': notify_committee,
            'slack_ok': slack_ok,
            'slack_error': slack_error,
        })

    except Exception as e:
        logging.error("api_send_to_review error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            conn.close()


@ext_engagement_bp.route('/api/case/<int:case_id>/withdraw', methods=['POST'])
@login_required
def api_withdraw(case_id):
    """取り下げ（管理者のみ）。draft/reviewing どちらからも可。"""
    user_id = session.get('user_id')
    if not is_engagement_admin(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        data   = request.json or {}
        reason = data.get('reason', '').strip()
        now    = get_jst_now()

        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT status FROM ext_engagement_cases WHERE id=%s", (case_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
        if row['status'] in ('approved', 'rejected', 'withdrawn'):
            return jsonify({'success': False, 'error': '既に確定済みの案件は取り下げできません'}), 400

        upd = conn.cursor()
        upd.execute("""
            UPDATE ext_engagement_cases SET
                status='withdrawn', decision=%s, approved_date=%s, updated_at=%s
            WHERE id=%s
        """, (reason or None, now.date(), now, case_id))

        if reason:
            upd.execute("""
                INSERT INTO ext_engagement_comments
                    (case_id, user_id, comment_type, content, is_conclusion, created_at)
                VALUES (%s, %s, 'conclusion', %s, 1, %s)
            """, (case_id, user_id, f'【取り下げ】{reason}', now))

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_withdraw error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            conn.close()


@ext_engagement_bp.route('/api/case/<int:case_id>/change_round', methods=['POST'])
@login_required
def api_change_round(case_id):
    """委員変更（管理者のみ）。reviewing → draft に戻し review_round を +1。"""
    user_id = session.get('user_id')
    if not is_engagement_admin(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        now  = get_jst_now()
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT status, review_round FROM ext_engagement_cases WHERE id=%s", (case_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
        if row['status'] != 'reviewing':
            return jsonify({'success': False, 'error': '審議中の案件のみ委員変更できます'}), 400

        new_round = (row['review_round'] or 1) + 1
        upd = conn.cursor()
        # 未決の委員長提案は委員変更により無効化する（履歴は残す）
        upd.execute("""
            UPDATE ext_engagement_motions
               SET status='withdrawn', closing_note='委員変更により無効',
                   closed_by=%s, closed_at=%s, updated_at=%s
             WHERE case_id=%s AND status='open'
        """, (user_id, now, now, case_id))
        # 新ラウンドに移行。リマインド記録はクリアし、アラートは既定ONに戻す。
        upd.execute("""
            UPDATE ext_engagement_cases SET
                status='draft', review_round=%s, updated_at=%s,
                last_reminder_kind=NULL, last_reminder_at=NULL, alert_enabled=1
            WHERE id=%s
        """, (new_round, now, case_id))
        conn.commit()
        return jsonify({'success': True, 'new_round': new_round})
    except Exception as e:
        logging.error("api_change_round error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            conn.close()


@ext_engagement_bp.route('/api/case/<int:case_id>/conclude', methods=['POST'])
@login_required
def api_case_conclude(case_id):
    """結論宣言（有効な審査委員長のみ）。確定後にSlackチャンネルへ自動投稿する。"""
    user_id = session.get('user_id')
    if not is_effective_chair(user_id, case_id):
        return jsonify({'success': False, 'error': '審査委員長（または代理）のみ操作できます'}), 403
    try:
        data          = request.json
        status        = data.get('status', 'approved')
        decision      = data.get('decision', '').strip()
        approved_date = parse_date(data.get('approved_date')) or get_jst_now().date()

        if status not in ('approved', 'rejected'):
            return jsonify({'success': False, 'error': 'ステータスが不正です'}), 400

        now = get_jst_now()
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ext_engagement_cases SET
                status=%s, decision=%s, approved_date=%s, updated_at=%s
            WHERE id=%s
        """, (status, decision, approved_date, now, case_id))

        if decision:
            cursor.execute("""
                INSERT INTO ext_engagement_comments
                    (case_id, user_id, comment_type, content, is_conclusion, created_at)
                VALUES (%s, %s, 'conclusion', %s, 1, %s)
            """, (case_id, user_id, decision, now))

        conn.commit()

        # ── 確定した案件を取り直してSlackへ投稿 ──
        slack_ok    = False
        slack_error = ''
        try:
            cur2 = conn.cursor(dictionary=True)
            cur2.execute("SELECT * FROM ext_engagement_cases WHERE id=%s", (case_id,))
            concluded = cur2.fetchone()
            cur2.close()
            if concluded:
                text = build_conclusion_message(concluded)
                slack_ok, slack_res = post_to_engagement_channel(text)
                record_slack_result(case_id, slack_ok, slack_res)
                status_label = STATUS_LABELS.get(concluded.get('status', ''), '')
                notify_log.record(
                    conn, case_id, concluded.get('review_round', 1),
                    notify_log.KIND_CONCLUSION,
                    notify_log.TARGET_CHANNEL, '委員会チャンネル',
                    slack_ok, error=(None if slack_ok else str(slack_res)),
                    summary=f'結論の通知：{status_label}',
                    created_by=user_id)
                if not slack_ok:
                    slack_error = str(slack_res)
                    logging.warning("conclude: slack post failed (case %s): %s",
                                    case_id, slack_res)
        except Exception as se:
            # Slack投稿の失敗は結論確定の成否に影響させない
            slack_error = str(se)
            logging.error("conclude: slack post exception (case %s): %s", case_id, se)

        return jsonify({
            'success': True,
            'status': status,
            'approved_date': fmt_date(approved_date),
            'slack_ok': slack_ok,
            'slack_error': slack_error,
        })
    except Exception as e:
        logging.error("api_case_conclude error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: コメント
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/case/<int:case_id>/comment', methods=['POST'])
@login_required
def api_comment_post(case_id):
    user_id = session.get('user_id')
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT status FROM ext_engagement_cases WHERE id=%s", (case_id,))
        row = cursor.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()

    if not row:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if row['status'] != 'reviewing':
        return jsonify({'success': False, 'error': '審議中の案件にのみ投稿できます'}), 403
    if not can_access_review(user_id, case_id):
        return jsonify({'success': False, 'error': 'アクセス権限がありません（COI対象または委員会外）'}), 403

    try:
        data         = request.json
        content      = data.get('content', '').strip()
        comment_type = data.get('comment_type', 'opinion')
        # 委員会への通知は opt in（既定 false）。
        # 投稿者が明示的に true にしたときだけチャンネルへ通知する。
        notify_committee = data.get('notify_committee') is True
        if not content:
            return jsonify({'success': False, 'error': 'コメントが空です'}), 400
        if comment_type not in ('opinion', 'question', 'answer'):
            comment_type = 'opinion'

        now = get_jst_now()
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ext_engagement_comments
                (case_id, user_id, comment_type, content, is_conclusion, created_at)
            VALUES (%s, %s, %s, %s, 0, %s)
        """, (case_id, user_id, comment_type, content, now))
        conn.commit()
        comment_id = cursor.lastrowid

        cursor2 = conn.cursor(dictionary=True)
        cursor2.execute("SELECT full_name FROM users WHERE id=%s", (user_id,))
        u = cursor2.fetchone()
        author_name = u['full_name'] if u else 'Unknown'

        # ── opt in で選ばれていれば委員会チャンネルへ通知 ──
        slack_ok = None   # None=通知しない / True / False
        if notify_committee:
            try:
                cur3 = conn.cursor(dictionary=True)
                cur3.execute("SELECT * FROM ext_engagement_cases WHERE id=%s",
                             (case_id,))
                case_row = cur3.fetchone()
                cur3.close()
                if case_row:
                    text = build_review_comment_message(
                        case_row, author_name, comment_type, content)
                    result = slack_notifier.notify_review_comment(text)
                    slack_ok = bool(result.get('ok'))
                    if not slack_ok:
                        logging.warning("api_comment_post: 審議コメント通知の"
                                        "送信失敗（案件 %s）: %s",
                                        case_id, result.get('error'))
                    ctype_label = {'opinion': '意見', 'question': '質問',
                                   'answer': '回答'}.get(comment_type, 'コメント')
                    notify_log.record(
                        conn, case_id, case_row.get('review_round', 1),
                        notify_log.KIND_REVIEW_COMMENT,
                        notify_log.TARGET_CHANNEL, '委員会チャンネル',
                        slack_ok,
                        error=(None if slack_ok else result.get('error')),
                        summary=f'審議コメント（{ctype_label}）の通知：{author_name}',
                        created_by=user_id)
            except Exception as se:
                slack_ok = False
                logging.error("api_comment_post: 審議コメント通知で例外"
                              "（案件 %s）: %s", case_id, se)

        return jsonify({'success': True, 'comment': {
            'id': comment_id, 'case_id': case_id, 'user_id': user_id,
            'author_name': author_name,
            'comment_type': comment_type, 'content': content,
            'is_conclusion': 0,
            'created_at': now.strftime('%Y-%m-%d %H:%M'),
        }, 'slack_notified': notify_committee, 'slack_ok': slack_ok})
    except Exception as e:
        logging.error("api_comment_post error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 委員の意見表明（投票）
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/case/<int:case_id>/vote', methods=['POST'])
@login_required
def api_vote(case_id):
    """
    委員の意見表明（承認でよい／意見を控える／不承認）。
    審議中の案件にのみ、COI除外でない委員が投じられる。1委員1票・上書き可。
    リクエスト: { "vote": "approve|abstain|reject", "comment": "（任意）" }
    """
    user_id = session.get('user_id')
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT status, review_round FROM ext_engagement_cases WHERE id=%s",
            (case_id,))
        row = cursor.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()

    if not row:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if row['status'] != 'reviewing':
        return jsonify({'success': False, 'error': '審議中の案件にのみ意見表明できます'}), 403
    if not can_access_review(user_id, case_id):
        return jsonify({'success': False, 'error': 'アクセス権限がありません（COI対象または委員会外）'}), 403

    round_no = row['review_round'] or 1
    # 意見表明の対象は委員（委員長は判定する立場なので対象外）
    eligible = get_round_committee_user_ids(case_id, round_no)
    if user_id not in eligible:
        return jsonify({'success': False, 'error': '意見表明できるのは審査委員のみです'}), 403

    try:
        data    = request.json or {}
        vote    = data.get('vote', '')
        comment = (data.get('comment') or '').strip()
        if vote not in VOTE_LABELS:
            return jsonify({'success': False, 'error': '意見区分が不正です'}), 400

        now = get_jst_now()
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ext_engagement_votes
                (case_id, round_no, user_id, vote, comment, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE vote=%s, comment=%s, updated_at=%s
        """, (case_id, round_no, user_id, vote, comment, now, now,
              vote, comment, now))
        conn.commit()

        summary = get_vote_summary(case_id, round_no)
        return jsonify({'success': True, 'vote_summary': summary,
                        'my_vote': {'vote': vote, 'comment': comment}})
    except Exception as e:
        logging.error("api_vote error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 期日アラートのON/OFF・審査期限変更（委員長または事務局）
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/case/<int:case_id>/alert', methods=['POST'])
@login_required
def api_set_alert(case_id):
    """
    期日リマインドのON/OFF切り替え、および審査期限（アラート基準日時）の変更。
    委員長（または代理）と事務局（管理者）が操作できる。
    操作はすべて ext_engagement_alert_log に記録される。
    リクエスト: {
        "enabled":    true|false,            # アラートON/OFF
        "review_end": "YYYY-MM-DD HH:MM"     # 任意。指定時は審査期限も更新
    }
    """
    user_id = session.get('user_id')
    role    = alert_role_of(user_id, case_id)
    if role not in ('chair', 'secretariat'):
        return jsonify({'success': False,
                        'error': '委員長または事務局のみ操作できます'}), 403
    try:
        data    = request.json or {}
        enabled = 1 if data.get('enabled') else 0
        now     = get_jst_now()

        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 変更前の審査期限を取得
        cursor.execute(
            "SELECT review_end FROM ext_engagement_cases WHERE id=%s", (case_id,))
        before = cursor.fetchone()
        if not before:
            return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
        old_review_end = before['review_end']

        # 審査期限：リクエストに review_end があれば更新、なければ据え置き
        deadline_changed = False
        if 'review_end' in data:
            new_review_end = parse_datetime(data.get('review_end'))
            deadline_changed = (old_review_end != new_review_end)
        else:
            new_review_end = old_review_end

        # 案件本体の更新
        if deadline_changed:
            # 期限が変わったらリマインド記録もクリア（再び効くように）
            cursor.execute("""
                UPDATE ext_engagement_cases SET
                    alert_enabled=%s, review_end=%s,
                    alert_set_by=%s, alert_set_at=%s,
                    last_reminder_kind=NULL, last_reminder_at=NULL,
                    updated_at=%s
                WHERE id=%s
            """, (enabled, new_review_end, user_id, now, now, case_id))
        else:
            cursor.execute("""
                UPDATE ext_engagement_cases SET
                    alert_enabled=%s, alert_set_by=%s, alert_set_at=%s, updated_at=%s
                WHERE id=%s
            """, (enabled, user_id, now, now, case_id))

        # アラート設定履歴を記録
        if deadline_changed:
            action = 'deadline'
            note   = ('審査期限を変更（アラートON）' if enabled
                      else '審査期限を変更（アラートOFF）')
        else:
            action = 'toggle'
            note   = ('アラートをONに設定' if enabled else 'アラートをOFFに設定')
        write_alert_log(conn, case_id, enabled, new_review_end,
                        user_id, role, action, note=note)

        conn.commit()
        return jsonify({
            'success': True,
            'alert_enabled': enabled,
            'review_end': fmt_datetime(new_review_end),
        })
    except Exception as e:
        logging.error("api_set_alert error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: Slack通知の手動再送（管理者・委員長）
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/case/<int:case_id>/notify_slack', methods=['POST'])
@login_required
def api_notify_slack(case_id):
    """
    結論をSlackの連携審査委員会チャンネルへ（再）投稿する。
    結論宣言時に自動投稿されるが、失敗した場合の手動再送用。
    管理者または有効な審査委員長が実行可能。
    """
    user_id = session.get('user_id')
    if not (is_engagement_admin(user_id) or is_effective_chair(user_id, case_id)):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM ext_engagement_cases WHERE id=%s", (case_id,))
        case = cursor.fetchone()
        cursor.close()
        if not case:
            conn.close()
            return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
        if case['status'] not in ('approved', 'rejected'):
            conn.close()
            return jsonify({'success': False, 'error': '結論が出ている案件のみ投稿できます'}), 400

        text    = build_conclusion_message(case)
        ok, res = post_to_engagement_channel(text)
        record_slack_result(case_id, ok, res)
        status_label = STATUS_LABELS.get(case.get('status', ''), '')
        notify_log.record(
            conn, case_id, case.get('review_round', 1),
            notify_log.KIND_CONCLUSION,
            notify_log.TARGET_CHANNEL, '委員会チャンネル',
            ok, error=(None if ok else str(res)),
            summary=f'結論の通知（再送）：{status_label}',
            created_by=user_id)
        conn.close()
        if ok:
            return jsonify({'success': True})
        # res は失敗時はエラーメッセージ文字列
        return jsonify({'success': False,
                        'error': f'Slack投稿に失敗しました: {res}'}), 502
    except Exception as e:
        logging.error("api_notify_slack error: %s", e)
        for vname in ('conn',):
            c = locals().get(vname)
            if c and c.is_connected(): c.close()
        return jsonify({'success': False, 'error': str(e)}), 500


# ────────────────────────────────────────────
# API: COI管理（管理者のみ）
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/case/<int:case_id>/coi/add', methods=['POST'])
@login_required
def api_coi_add(case_id):
    user_id = session.get('user_id')
    if not is_engagement_admin(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        data           = request.json
        target_uid     = data.get('user_id')
        reason         = data.get('reason', '').strip()

        # COI除外は常に「委員ごと除外」に固定する。
        # 議長がCOIで外れる ＝ その案件では委員としても審議に参加しない（外野から見守るのみ）。
        # 議長だけ外して委員に残す（旧 chair_only）状態は現実に起こらないため廃止。
        # UI が何を送ってきても committee に倒す。
        excluded_roles = 'committee'

        if not target_uid:
            return jsonify({'success': False, 'error': 'ユーザIDが必要です'}), 400

        now = get_jst_now()
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ext_engagement_coi
                (case_id, user_id, reason, excluded_roles, created_by, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE reason=%s, excluded_roles=%s
        """, (case_id, target_uid, reason, excluded_roles, user_id, now,
              reason, excluded_roles))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_coi_add error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ext_engagement_bp.route('/api/case/<int:case_id>/coi/remove', methods=['POST'])
@login_required
def api_coi_remove(case_id):
    user_id = session.get('user_id')
    if not is_engagement_admin(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        data       = request.json
        target_uid = data.get('user_id')
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM ext_engagement_coi WHERE case_id=%s AND user_id=%s",
            (case_id, target_uid))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_coi_remove error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ext_engagement_bp.route('/api/case/<int:case_id>/acting_chair', methods=['POST'])
@login_required
def api_set_acting_chair(case_id):
    user_id = session.get('user_id')
    if not is_engagement_admin(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        data             = request.json
        acting_chair_uid = data.get('user_id')
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE ext_engagement_cases SET acting_chair_user_id=%s, updated_at=%s WHERE id=%s",
            (acting_chair_uid, get_jst_now(), case_id))
        conn.commit()
        return jsonify({'success': True, 'acting_chair_user_id': acting_chair_uid})
    except Exception as e:
        logging.error("api_set_acting_chair error: %s", e)
        if 'conn' in locals(): conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ext_engagement_bp.route('/api/committee_members', methods=['GET'])
@login_required
def api_committee_members():
    user_id = session.get('user_id')
    # 委員会構成のレビューは事務局と委員長が行える。
    # （委員選定の操作ではなく、現在のまいぐる構成の閲覧のため）
    if not (is_engagement_admin(user_id) or
            user_is_in_group(user_id, GROUP_CHAIR)):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        member_ids = get_group_member_ids(GROUP_COMMITTEE)
        chair_ids  = get_group_member_ids(GROUP_CHAIR)
        all_ids    = list(set(member_ids + chair_ids))
        if not all_ids:
            return jsonify({'success': True, 'members': []})

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        fmt = ','.join(['%s'] * len(all_ids))
        cursor.execute(
            f"SELECT id, full_name, email FROM users WHERE id IN ({fmt}) ORDER BY full_name",
            all_ids)
        members = cursor.fetchall()
        for m in members:
            m['is_chair']     = m['id'] in chair_ids
            m['is_committee'] = m['id'] in member_ids
        return jsonify({'success': True, 'members': members})
    except Exception as e:
        logging.error("api_committee_members error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 通知記録（事務局・委員長用）
#   案件ごとの通知ログ（送信済みの履歴）と、
#   期日アラートの「点検・送信予定」を返す。
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/case/<int:case_id>/notify_log', methods=['GET'])
@login_required
def api_notify_log(case_id):
    user_id = session.get('user_id')
    # 通知記録は事務局と委員長（および admin）のみ閲覧できる
    if not (is_engagement_admin(user_id) or is_effective_chair(user_id, case_id)):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 案件の現状（点検・送信予定の判定に使う）
        cursor.execute("""
            SELECT status, review_round, review_end, alert_enabled,
                   last_reminder_kind, last_reminder_at
            FROM ext_engagement_cases WHERE id=%s
        """, (case_id,))
        case = cursor.fetchone()
        if not case:
            cursor.close(); conn.close()
            return jsonify({'success': False, 'error': '案件が見つかりません'}), 404

        # ── 送信済みの通知（履歴）。新しい順 ──
        cursor.execute("""
            SELECT n.id, n.round_no, n.notify_kind, n.target_kind,
                   n.target_label, n.status, n.error, n.summary,
                   n.created_at, u.full_name AS created_by_name
            FROM ext_engagement_notify_log n
            LEFT JOIN users u ON u.id = n.created_by
            WHERE n.case_id = %s
            ORDER BY n.created_at DESC, n.id DESC
        """, (case_id,))
        rows = cursor.fetchall()
        history = []
        for r in rows:
            history.append({
                'id':            r['id'],
                'round_no':      r['round_no'],
                'notify_kind':   r['notify_kind'],
                'kind_label':    notify_log.KIND_LABEL.get(
                                     r['notify_kind'], r['notify_kind']),
                'target_kind':   r['target_kind'],
                'target_label':  r['target_label'] or '',
                'status':        r['status'],
                'error':         r['error'] or '',
                'summary':       r['summary'] or '',
                'created_by_name': r['created_by_name'] or '',
                'created_at':    fmt_datetime(r['created_at']),
            })

        # ── 点検・送信予定（期日アラートの状態）──
        # 細かい予測はせず、アラームがセットされているかを示す。
        plan = {}
        if case['status'] != 'reviewing':
            plan = {'active': False,
                    'message': 'この案件は審議中ではないため、'
                               '期日アラートによるリマインドの対象外です。'}
        elif not case['alert_enabled']:
            plan = {'active': False,
                    'message': '期日アラートは無効です。'
                               '委員長へのリマインドは送信されません。'}
        else:
            msg = ('期日アラートは有効です。審査期限を過ぎても結論が出ない場合、'
                   'または対象委員全員が意見表明した場合に、委員長へ'
                   'リマインドを送信します（条件付き送付）。')
            if case['review_end']:
                msg += f'　審査期限：{fmt_datetime(case["review_end"])}'
            last_kind = case.get('last_reminder_kind')
            if last_kind:
                lk_label = notify_log.KIND_LABEL.get(
                    'reminder_overdue' if last_kind == 'overdue'
                    else 'reminder_all_voted', last_kind)
                msg += (f'　※ 直近のリマインド送信：{lk_label}'
                        f'（{fmt_datetime(case.get("last_reminder_at"))}）')
            plan = {'active': True, 'message': msg}

        cursor.close(); conn.close()
        return jsonify({'success': True, 'history': history, 'plan': plan})
    except Exception as e:
        logging.error("api_notify_log error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500


# ────────────────────────────────────────────
# API: ユーザ検索（管理者用）
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/users/search', methods=['GET'])
@login_required
def api_users_search():
    user_id = session.get('user_id')
    if not is_engagement_admin(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        q = request.args.get('q', '').strip()
        if not q:
            return jsonify({'success': True, 'users': []})

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, full_name FROM users "
            "WHERE full_name LIKE %s ORDER BY full_name LIMIT 20",
            (f'%{q}%',))
        users = cursor.fetchall()
        return jsonify({'success': True, 'users': users})
    except Exception as e:
        logging.error("api_users_search error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 年度報告・CSV・通知文
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/report', methods=['GET'])
@login_required
def api_report():
    """
    年度報告。
      連携審査_管理者：全状態の案件と各件の直接経費・間接経費・合計経費、経費の合計
      ゲスト（regular 以上）：承認済みの案件だけを、案件ID・金額・ステータスを除いて返す
    """
    user_id = session.get('user_id')
    is_admin_v = is_engagement_admin(user_id)
    if not (is_admin_v or can_view_concluded(user_id)):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        fiscal_year = request.args.get('fiscal_year', get_current_fiscal_year(), type=int)
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM ext_engagement_cases WHERE fiscal_year=%s ORDER BY type_key, seq_no",
            (fiscal_year,))
        cases = [serialize_case(c) for c in cursor.fetchall()]

        if not is_admin_v:
            # ゲスト用：承認済みのみ。案件ID・リンク・金額・ステータスは返さない
            approved_cases = [c for c in cases if c['status'] == 'approved']
            by_type = {}
            out = []
            for c in approved_cases:
                tk = c['type_key']
                if tk not in by_type:
                    by_type[tk] = {'label': TYPE_LABELS.get(tk, tk), 'approved': 0}
                by_type[tk]['approved'] += 1
                out.append({
                    'application_no':      c.get('application_no') or '',
                    'type_key':            tk,
                    'type_label':          c.get('type_label', ''),
                    'is_new_application':  c.get('is_new_application'),
                    'title':               c.get('title') or '',
                    'org_name':            c.get('org_name') or '',
                    'faculty_dept':        c.get('faculty_dept') or '',
                    'representative_name': c.get('representative_name') or '',
                    'period_start':        c.get('period_start') or '',
                    'period_end':          c.get('period_end') or '',
                    'approved_date':       c.get('approved_date') or '',
                })
            return jsonify({'success': True, 'view': 'guest',
                            'fiscal_year': fiscal_year, 'cases': out,
                            'summary': {'approved': len(out), 'by_type': by_type}})

        # 管理者用：各件の経費。合計は直接＋間接、どちらも無ければ旧テキスト欄の数字
        for c in cases:
            d, i = c.get('direct_fee'), c.get('indirect_fee')
            if d is not None or i is not None:
                c['total_fee'] = (d or 0) + (i or 0)
                c['fee_from_text'] = False
            else:
                c['total_fee'] = _parse_fee_to_int(c.get('research_fee'))
                c['fee_from_text'] = c['total_fee'] is not None

        approved_cases = [c for c in cases if c['status'] == 'approved']
        total    = len(cases)
        approved = len(approved_cases)
        by_type  = {}
        for c in cases:
            tk = c['type_key']
            if tk not in by_type:
                by_type[tk] = {'label': TYPE_LABELS.get(tk, tk), 'total': 0, 'approved': 0}
            by_type[tk]['total'] += 1
            if c['status'] == 'approved':
                by_type[tk]['approved'] += 1

        # 経費の合計（承認済み案件）
        sum_direct   = sum(c.get('direct_fee') or 0 for c in approved_cases)
        sum_indirect = sum(c.get('indirect_fee') or 0 for c in approved_cases)
        sum_total    = sum(c.get('total_fee') or 0 for c in approved_cases)

        return jsonify({'success': True, 'view': 'admin',
            'fiscal_year': fiscal_year, 'cases': cases,
            'summary': {'total': total, 'approved': approved,
                'rejected':  sum(1 for c in cases if c['status'] == 'rejected'),
                'reviewing': sum(1 for c in cases if c['status'] == 'reviewing'),
                'draft':     sum(1 for c in cases if c['status'] == 'draft'),
                'by_type': by_type,
                'fee': {'direct': sum_direct, 'indirect': sum_indirect,
                        'total': sum_total}}})
    except Exception as e:
        logging.error("api_report error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ext_engagement_bp.route('/api/report/csv', methods=['GET'])
@login_required
def api_report_csv():
    # CSV 出力は連携審査_管理者（および admin）だけ。ゲスト用の年度報告からは使えない
    user_id = session.get('user_id')
    if not is_engagement_admin(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        fiscal_year = request.args.get('fiscal_year', get_current_fiscal_year(), type=int)
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM ext_engagement_cases WHERE fiscal_year=%s ORDER BY type_key, seq_no",
            (fiscal_year,))
        cases = cursor.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            '案件ID', '申請No', '年度', '種別', '新規/継続',
            '課題名/事業名', '申請組織', '概要', '研究費/事業費（旧テキスト）',
            '直接経費', '間接経費', '合計経費',
            '期間（開始）', '期間（終了）', '本学担当者（申請者希望）', '所属',
            '事前協議', '承認日', 'ステータス', '結論',
        ])
        for c in cases:
            direct   = c.get('direct_fee')
            indirect = c.get('indirect_fee')
            total    = (direct or 0) + (indirect or 0) if (direct is not None or indirect is not None) else ''
            writer.writerow([
                format_case_id(c['fiscal_year'], c['type_key'], c['seq_no']),
                c.get('application_no') or '',
                c['fiscal_year'],
                TYPE_LABELS.get(c['type_key'], ''),
                '新規' if c.get('is_new_application') else '継続',
                c.get('title', ''), c.get('org_name', ''), c.get('content', ''),
                c.get('research_fee', ''),
                direct if direct is not None else '',
                indirect if indirect is not None else '',
                total,
                fmt_date(c.get('period_start')), fmt_date(c.get('period_end')),
                c.get('representative_name', ''), c.get('faculty_dept', ''),
                c.get('pre_consultation') or '',
                fmt_date(c.get('approved_date')),
                STATUS_LABELS.get(c.get('status', ''), ''),
                c.get('decision', ''),
            ])

        output.seek(0)
        filename = f"ext_engagement_report_{fiscal_year}.csv"
        return Response('\ufeff' + output.getvalue(),
            mimetype='text/csv; charset=utf-8-sig',
            headers={'Content-Disposition': f'attachment; filename={filename}'})
    except Exception as e:
        logging.error("api_report_csv error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ext_engagement_bp.route('/api/case/<int:case_id>/notify_text', methods=['GET'])
@login_required
def api_notify_text(case_id):
    user_id = session.get('user_id')
    if not is_engagement_admin(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM ext_engagement_cases WHERE id=%s", (case_id,))
        c = cursor.fetchone()
        if not c:
            return jsonify({'success': False, 'error': '案件が見つかりません'}), 404

        type_label      = TYPE_LABELS.get(c['type_key'], '')
        case_display_id = format_case_id(c['fiscal_year'], c['type_key'], c['seq_no'])
        period_str      = (
            f"{to_wareki(c.get('period_start'))}～{to_wareki(c.get('period_end'))}"
            if c.get('period_start') else '')
        rep_str    = '　'.join(filter(None, [c.get('faculty_dept'), c.get('representative_name')]))
        app_no_str = f"申請No：{c['application_no']}　" if c.get('application_no') else ''

        text = (
            f"【{case_display_id}】{app_no_str}{type_label}承認：{c['title']}"
            f"〔承認日：{to_wareki(c.get('approved_date'))}〕\n"
            f"報告者　{getattr(Config, 'EXT_ENGAGEMENT_REPORTER', '') or '機構長'}\n\n"
            f"{ORG_LABEL.get(c['type_key'],'申請組織')}　　　　　{c.get('org_name','')}\n"
            f"{CONTENT_LABEL.get(c['type_key'],'内容')}　　　　　{c.get('content','')}\n"
            f"{PERIOD_LABEL.get(c['type_key'],'期間')}　　　　　{period_str}\n"
            f"{REP_LABEL.get(c['type_key'],'代表者')}　{rep_str}\n"
            + (f"事前協議　　　　　{c['pre_consultation']}\n" if c.get('pre_consultation') else '')
        )
        return jsonify({'success': True, 'text': text})
    except Exception as e:
        logging.error("api_notify_text error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 統計ダッシュボード
# ────────────────────────────────────────────

@ext_engagement_bp.route('/api/stats', methods=['GET'])
@login_required
def api_stats():
    user_id = session.get('user_id')
    if not can_view_index(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        fiscal_year = request.args.get('fiscal_year', get_current_fiscal_year(), type=int)

        # 今年度承認案件 (nishida$default)
        conn_d = mysql.connector.connect(**DatabaseConfig.default())
        cur_d  = conn_d.cursor(dictionary=True)
        cur_d.execute("""
            SELECT direct_fee, indirect_fee, approved_date
            FROM ext_engagement_cases
            WHERE status = 'approved' AND fiscal_year = %s
            ORDER BY approved_date
        """, (fiscal_year,))
        approved_this_year = cur_d.fetchall()
        cur_d.close(); conn_d.close()

        # 月別集計
        monthly = {m: {'count': 0, 'amount': 0} for m in range(1, 13)}
        for c in approved_this_year:
            d = c.get('approved_date')
            if d:
                if hasattr(d, 'month'):
                    m = d.month
                else:
                    try:
                        m = datetime.datetime.strptime(str(d)[:10], '%Y-%m-%d').month
                    except Exception:
                        m = None
                if m:
                    fee = (c.get('direct_fee') or 0) + (c.get('indirect_fee') or 0)
                    monthly[m]['count']  += 1
                    monthly[m]['amount'] += fee

        fiscal_months = list(range(4, 13)) + list(range(1, 4))
        cum_count = 0; cum_amount = 0
        monthly_series = []
        for m in fiscal_months:
            cum_count  += monthly[m]['count']
            cum_amount += monthly[m]['amount']
            monthly_series.append({
                'month':             m,
                'month_label':       f"{m}月",
                'count':             monthly[m]['count'],
                'amount':            monthly[m]['amount'],
                'cumulative_count':  cum_count,
                'cumulative_amount': cum_amount,
            })

        total_approved = sum(v['count']  for v in monthly.values())
        total_amount   = sum(v['amount'] for v in monthly.values())

        # T_06_04 過去データ (nishida$fujinp)
        conn_f = mysql.connector.connect(**DatabaseConfig.fujinp())
        cur_f  = conn_f.cursor(dictionary=True)
        cur_f.execute("""
            SELECT 委託年度,
                   COUNT(*) AS cnt,
                   SUM(COALESCE(経費, 0))    AS total_fee,
                   SUM(COALESCE(直接経費, 0)) AS total_direct,
                   SUM(COALESCE(間接経費, 0)) AS total_indirect
            FROM T_06_04_受託共同研究事業費受入実績
            GROUP BY 委託年度
            ORDER BY 委託年度
        """)
        historical = []
        for row in cur_f.fetchall():
            historical.append({
                'year':           row['委託年度'],
                'count':          int(row['cnt']),
                'total_fee':      int(row['total_fee']      or 0),
                'total_direct':   int(row['total_direct']   or 0),
                'total_indirect': int(row['total_indirect'] or 0),
            })
        cur_f.close(); conn_f.close()

        return jsonify({
            'success':       True,
            'fiscal_year':   fiscal_year,
            'summary':       {'total_approved': total_approved, 'total_amount': total_amount},
            'monthly_series': monthly_series,
            'historical':    historical,
        })

    except Exception as e:
        logging.error("api_stats error: %s", e)
        for vname in ('conn_d', 'conn_f'):
            c = locals().get(vname)
            if c and c.is_connected(): c.close()
        return jsonify({'success': False, 'error': str(e)}), 500


# ────────────────────────────────────────────
# API: T_06_04_受託共同研究事業費受入実績 同期
# ────────────────────────────────────────────

def _parse_fee_to_int(fee_str):
    """research_fee 文字列を int に変換。失敗時は None を返す。"""
    if not fee_str:
        return None
    # 数字以外を除去して変換
    digits = ''.join(c for c in str(fee_str) if c.isdigit())
    return int(digits) if digits else None


@ext_engagement_bp.route('/api/t0604/check', methods=['GET'])
@login_required
def api_t0604_check():
    """
    承認済み案件のうち T_06_04 に未反映のものを返す（管理者専用）。
    突合キー：委託年度 × 委託者 × 内容（タイトル）の3点で重複チェック。
    """
    user_id = session.get('user_id')
    if not is_engagement_admin(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        # ext_engagement_cases は nishida$default
        conn_default   = mysql.connector.connect(**DatabaseConfig.default())
        cursor_default = conn_default.cursor(dictionary=True)

        # 承認済み案件を全年度取得
        cursor_default.execute("""
            SELECT id, fiscal_year, type_key, seq_no, application_no,
                   title, org_name, content,
                   representative_name, faculty_dept,
                   research_fee, direct_fee, indirect_fee, approved_date
            FROM ext_engagement_cases
            WHERE status = 'approved'
            ORDER BY fiscal_year DESC, type_key, seq_no
        """)
        approved = cursor_default.fetchall()
        cursor_default.close(); conn_default.close()

        # T_06_04 は nishida$fujinp
        conn_fujinp   = mysql.connector.connect(**DatabaseConfig.fujinp())
        cursor_fujinp = conn_fujinp.cursor(dictionary=True)

        # T_06_04 の既存レコードを取得（突合用）
        cursor_fujinp.execute("""
            SELECT 委託年度, 委託者, 内容
            FROM T_06_04_受託共同研究事業費受入実績
        """)
        existing = cursor_fujinp.fetchall()
        cursor_fujinp.close(); conn_fujinp.close()

        # 突合セット（年度・委託者・内容の組）
        existing_keys = set()
        for row in existing:
            key = (
                row.get('委託年度'),
                (row.get('委託者') or '').strip(),
                (row.get('内容') or '').strip(),
            )
            existing_keys.add(key)

        # 未反映案件を抽出
        unsynced = []
        for c in approved:
            content_str = (c.get('title') or '').strip()
            key = (
                c['fiscal_year'],
                (c.get('org_name') or '').strip(),
                content_str,
            )
            if key not in existing_keys:
                unsynced.append({
                    'id':               c['id'],
                    'case_display_id':  format_case_id(c['fiscal_year'], c['type_key'], c['seq_no']),
                    'fiscal_year':      c['fiscal_year'],
                    'type_key':         c['type_key'],
                    'type_label':       TYPE_LABELS.get(c['type_key'], c['type_key']),
                    'application_no':   c.get('application_no') or '',
                    'title':            c.get('title') or '',
                    'org_name':         c.get('org_name') or '',
                    'content':          c.get('content') or '',
                    'representative_name': c.get('representative_name') or '',
                    'faculty_dept':     c.get('faculty_dept') or '',
                    'research_fee':     c.get('research_fee') or '',
                    'research_fee_int': _parse_fee_to_int(c.get('research_fee')),
                    'direct_fee':       c.get('direct_fee'),    # int or None
                    'indirect_fee':     c.get('indirect_fee'),  # int or None
                    'approved_date':    fmt_date(c.get('approved_date')),
                })

        return jsonify({'success': True, 'unsynced': unsynced, 'total': len(unsynced)})

    except Exception as e:
        logging.error("api_t0604_check error: %s", e)
        for v in ('conn_default', 'conn_fujinp'):
            c = locals().get(v)
            if c and c.is_connected(): c.close()
        return jsonify({'success': False, 'error': str(e)}), 500


@ext_engagement_bp.route('/api/t0604/sync', methods=['POST'])
@login_required
def api_t0604_sync():
    """
    指定された承認済み案件を T_06_04 に追記する（管理者専用）。
    リクエスト: { "case_ids": [1, 2, ...] }
    - 整理番号  : application_no があれば使用、なければ NULL
    - 経費      : research_fee を数値変換。失敗時は NULL
    - 直接経費・間接経費 : 常に NULL（点検時に手動補完）
    - 内容      : title を使用（content は備考に含める）
    - 備考      : "ext_engagement ID:{id} / {content}" 形式で自動付記
    """
    user_id = session.get('user_id')
    if not is_engagement_admin(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    try:
        data     = request.json or {}
        case_ids = data.get('case_ids', [])
        if not case_ids:
            return jsonify({'success': False, 'error': '案件IDが指定されていません'}), 400

        # ext_engagement_cases は nishida$default
        conn_default   = mysql.connector.connect(**DatabaseConfig.default())
        cursor_default = conn_default.cursor(dictionary=True)

        # 対象案件を取得（承認済みのみ）
        fmt = ','.join(['%s'] * len(case_ids))
        cursor_default.execute(
            f"SELECT * FROM ext_engagement_cases WHERE id IN ({fmt}) AND status='approved'",
            case_ids)
        cases = cursor_default.fetchall()
        cursor_default.close(); conn_default.close()

        if not cases:
            return jsonify({'success': False, 'error': '承認済み案件が見つかりません'}), 404

        # T_06_04 は nishida$fujinp
        conn_fujinp = mysql.connector.connect(**DatabaseConfig.fujinp())
        ins = conn_fujinp.cursor()
        inserted = []
        for c in cases:
            biko_parts = [f"ext_engagement ID:{c['id']}"]
            if c.get('content'):
                biko_parts.append(c['content'])
            biko = ' / '.join(biko_parts)

            # 経費：direct_fee/indirect_fee 優先、なければ research_fee から変換
            direct_fee   = c.get('direct_fee')
            indirect_fee = c.get('indirect_fee')
            if direct_fee is not None or indirect_fee is not None:
                total_fee = (direct_fee or 0) + (indirect_fee or 0)
            else:
                total_fee    = _parse_fee_to_int(c.get('research_fee'))
                direct_fee   = None
                indirect_fee = None

            ins.execute(
                "INSERT INTO T_06_04_受託共同研究事業費受入実績 "
                "(整理番号, 委託者, 委託年度, 内容, 所属, 担当者, "
                " 経費, 直接経費, 間接経費, 備考) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    c.get('application_no') or None,
                    c.get('org_name') or None,
                    c.get('fiscal_year'),
                    (c.get('title') or '').strip() or None,
                    c.get('faculty_dept') or None,
                    c.get('representative_name') or None,
                    total_fee,
                    direct_fee,
                    indirect_fee,
                    biko or None,
                ))
            inserted.append(format_case_id(c['fiscal_year'], c['type_key'], c['seq_no']))

        conn_fujinp.commit()
        conn_fujinp.close()
        logging.info("T_06_04 sync: inserted %d records by user %s: %s",
                     len(inserted), user_id, inserted)
        return jsonify({'success': True, 'inserted': len(inserted), 'cases': inserted})

    except Exception as e:
        logging.error("api_t0604_sync error: %s", e)
        for vname in ('conn_default', 'conn_fujinp'):
            c = locals().get(vname)
            if c and c.is_connected():
                try: c.rollback()
                except: pass
                c.close()
        return jsonify({'success': False, 'error': str(e)}), 500


# ────────────────────────────────────────────
# 委員長提案（motion）のAPIは motions.py に分離。
# 循環参照を避けるためファイル末尾でインポートする。
# ────────────────────────────────────────────
from . import motions  # noqa: E402
