#!/usr/bin/env python3
"""
ext_engagement 期日チェック・委員長リマインド スクリプト
================================================================

PythonAnywhere の「Scheduled Tasks（日時指定タスク）」から1日1回起動する、
スタンドアロンの期日チェックスクリプト。

FUJIN-P のアプリ本体（Flask のルート）には依存せず、config.py / db.py だけを
読み込んで直接DBを参照する。プラットフォーム独立に書いてあるため、
別アカウントへ移しても config.py の書き換えだけで動く。

────────────────────────────────────────────────────────────────
リマインドの対象（status='reviewing' かつ alert_enabled=1 の案件のみ）
  ケース①  all_voted : 審査期限前に、COI除外を除く対象委員全員の意見が
                        出そろった。客観的には判定可能な状態。
  ケース②  overdue   : 審査期限を過ぎても結論が出ていない。

  いずれも委員長（acting_chair があればその人、なければ委員長プール全員）の
  Slack DM へ1回だけ送る。送信済みは last_reminder_kind / last_reminder_at に
  記録し、二重送信しない。委員長が期間延長（review_end 変更）または
  委員変更（change_round）を行うと記録はクリアされ、再び1回送られる。

  優先順位：両ケースに該当する場合は overdue（期限超過）を優先する。

実行方法（PythonAnywhere の Scheduled Tasks に登録するコマンド例）:
  python3 /home/<owner>/fujinp/ext_engagement/check_deadlines.py

  ※ <owner> は config.py の設定に合わせる。fujinp 配下に置くこと。
  ※ config.py / db.py を import できるよう、fujinp ディレクトリを
    sys.path に追加している（下記参照）。
────────────────────────────────────────────────────────────────
"""

import os
import sys
import logging
import datetime

# ── config.py / db.py を import できるよう sys.path を整える ──
# このスクリプトは /home/<owner>/fujinp/ext_engagement/ に置かれる。
# 一方、FUJIN-P 共通の config.py / db.py / app.py は、この環境では
# /home/<owner>/ の直下に置かれている（fujinp/ の中ではない）。
# そのため、候補となるディレクトリを複数 sys.path に追加し、
# config.py が実際にある場所を確実に拾えるようにする。
#   候補1: /home/<owner>/fujinp/ext_engagement/   （スクリプト自身）
#   候補2: /home/<owner>/fujinp/                   （アプリ群の親）
#   候補3: /home/<owner>/                          （共通モジュールの実際の場所）
_THIS_DIR   = os.path.dirname(os.path.abspath(__file__))     # .../fujinp/ext_engagement
_FUJINP_DIR = os.path.dirname(_THIS_DIR)                     # .../fujinp
_HOME_DIR   = os.path.dirname(_FUJINP_DIR)                   # .../<owner>
for _cand in (_THIS_DIR, _FUJINP_DIR, _HOME_DIR):
    if _cand not in sys.path:
        sys.path.insert(0, _cand)

import mysql.connector
from config import Config
from db import DatabaseConfig

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [check_deadlines] %(levelname)s %(message)s',
)

# まいぐるの委員長グループ名（routes.py の GROUP_CHAIR と一致させること）
GROUP_CHAIR = '連携審査_審査委員長'

# 意見表明の3区分（routes.py の VOTE_LABELS と一致させること）
VOTE_LABELS = {
    'approve': '承認でよい',
    'abstain': '意見を控える',
    'reject':  '不承認',
}

JST_OFFSET = datetime.timedelta(hours=9)


# ════════════════════════════════════════════════════════════
# 日時ユーティリティ
# ════════════════════════════════════════════════════════════

def get_jst_now():
    """現在のJST日時（naive datetime）。"""
    return (datetime.datetime.utcnow() + JST_OFFSET)


# ════════════════════════════════════════════════════════════
# まいぐる連携：委員長プールの取得
# ════════════════════════════════════════════════════════════

def get_chair_pool_user_ids():
    """
    まいぐるの委員長グループに所属する有効メンバーのuser_id一覧。
    user_groups / user_group_memberships を直接参照する（有効期間を考慮）。
    """
    now = get_jst_now()
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT m.user_id
            FROM user_group_memberships m
            JOIN user_groups g ON m.group_id = g.id
            WHERE g.name = %s
              AND (m.valid_from  IS NULL OR m.valid_from  <= %s)
              AND (m.valid_until IS NULL OR m.valid_until >= %s)
        """, (GROUP_CHAIR, now, now))
        return [r['user_id'] for r in cursor.fetchall()]
    except Exception as e:
        logging.error("get_chair_pool_user_ids error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def get_effective_chair_users(case):
    """
    案件の「実質的な委員長」を {user_id, full_name, email} の辞書リストで返す。
    acting_chair_user_id があればその1名、なければ委員長プール全員。
    DMはメールアドレス照合方式で送るため、email を必ず含める。
    """
    if case.get('acting_chair_user_id'):
        chair_ids = [case['acting_chair_user_id']]
    else:
        chair_ids = get_chair_pool_user_ids()
    if not chair_ids:
        return []
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        fmt = ','.join(['%s'] * len(chair_ids))
        cursor.execute(
            f"SELECT id, full_name, email FROM users WHERE id IN ({fmt})",
            chair_ids)
        return [{'user_id': r['id'],
                 'full_name': r.get('full_name') or '',
                 'email': r.get('email') or ''}
                for r in cursor.fetchall()]
    except Exception as e:
        logging.error("get_effective_chair_users error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ════════════════════════════════════════════════════════════
# Slack DM 送信
#
#   Slack との通信は ext_engagement/slack_notifier.py に集約する
#   （ethics_review と同じ方式）。DMはメールアドレス照合方式：
#   FUJIN-P 側の users.email から Slack ユーザーを引き当てる。
#   users.slack_user_id カラムは使わない。
# ════════════════════════════════════════════════════════════

# slack_notifier はアプリ内モジュール。check_deadlines.py は
# /home/<owner>/fujinp/ext_engagement/ に置かれ、同じディレクトリに
# slack_notifier.py / notify_log.py がある。冒頭で sys.path に _THIS_DIR を
# 追加済みなので直接 import できる。
import slack_notifier
import notify_log


def send_chair_dm(email, text):
    """
    委員長へDMを送る。メールアドレス照合方式（slack_notifier 経由）。
    (ok, message) を返す。
    """
    if not email:
        return False, 'メールアドレスが未登録です'
    result = slack_notifier.send_dm_by_email(email, text,
                                             log_label='委員長リマインドDM')
    if result.get('ok'):
        return True, result
    return False, result.get('error', 'DM送信に失敗しました')


# ════════════════════════════════════════════════════════════
# 案件・意見表明の取得
# ════════════════════════════════════════════════════════════

def get_reviewing_cases():
    """status='reviewing' かつ alert_enabled=1 の案件を取得。"""
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT *
            FROM ext_engagement_cases
            WHERE status = 'reviewing'
              AND (alert_enabled IS NULL OR alert_enabled = 1)
        """)
        return cursor.fetchall()
    except Exception as e:
        logging.error("get_reviewing_cases error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def get_eligible_committee_ids(case_id, round_no):
    """
    指定ラウンドの意見表明対象委員（role='member' かつ COIで委員除外でない者）。
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
        logging.error("get_eligible_committee_ids error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def get_voted_user_ids(case_id, round_no):
    """指定ラウンドで意見表明済みの委員のuser_id集合。"""
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT user_id FROM ext_engagement_votes
            WHERE case_id = %s AND round_no = %s
        """, (case_id, round_no))
        return {r['user_id'] for r in cursor.fetchall()}
    except Exception as e:
        logging.error("get_voted_user_ids error: %s", e)
        return set()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def get_open_motion(case_id):
    """
    未決の委員長提案（ext_engagement_motions.status='open'）があれば
    {id, motion_no, round_no, voted:set(user_id)} を返す。なければ None。
    """
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, motion_no, round_no FROM ext_engagement_motions
            WHERE case_id = %s AND status = 'open'
            ORDER BY id DESC LIMIT 1
        """, (case_id,))
        m = cursor.fetchone()
        if not m:
            return None
        cursor.execute("""
            SELECT user_id FROM ext_engagement_motion_votes WHERE motion_id = %s
        """, (m['id'],))
        m['voted'] = {r['user_id'] for r in cursor.fetchall()}
        return m
    except Exception as e:
        # テーブル未作成などは「提案なし」として扱う
        logging.warning("get_open_motion: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def mark_reminder_sent(case_id, kind):
    """リマインド送信済みを ext_engagement_cases に記録する。"""
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ext_engagement_cases
            SET last_reminder_kind = %s, last_reminder_at = %s
            WHERE id = %s
        """, (kind, get_jst_now(), case_id))
        conn.commit()
    except Exception as e:
        logging.error("mark_reminder_sent error: %s", e)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ════════════════════════════════════════════════════════════
# 案件IDの表示用整形
# ════════════════════════════════════════════════════════════

TYPE_SHORT = {
    'contract_research':      '受託研究',
    'contract_project':       '受託事業',
    'collaborative_research': '共同研究',
    'collaborative_project':  '共同事業',
}


def format_case_id(case):
    return f"{case['fiscal_year']}-{TYPE_SHORT.get(case['type_key'], case['type_key'])}-{case['seq_no']}"


def case_app_url(case_id):
    """案件詳細ページの絶対URL。BASE_URL は config.py 由来（プラットフォーム独立）。"""
    base = getattr(Config, 'BASE_URL', '').rstrip('/')
    return f"{base}/ext_engagement/case/{case_id}"


# ════════════════════════════════════════════════════════════
# リマインド本文の組み立て
# ════════════════════════════════════════════════════════════

def build_reminder_text(case, kind, vote_summary):
    """
    委員長へのリマインドDM本文。
      kind='all_voted' : 意見が出そろった
      kind='overdue'   : 期限超過
    """
    cid   = format_case_id(case)
    title = case.get('title', '')
    url   = case_app_url(case['id'])
    counts = vote_summary['counts']
    summary_line = (
        f"意見表明：{vote_summary['total_voted']}/{vote_summary['eligible']}名"
        f"（{VOTE_LABELS['approve']} {counts['approve']}・"
        f"{VOTE_LABELS['abstain']} {counts['abstain']}・"
        f"{VOTE_LABELS['reject']} {counts['reject']}）"
    )

    if kind == 'all_voted':
        motion = case.get('_open_motion')
        if motion:
            head = (
                f"【連携申請審査 リマインド】委員長提案（第{motion['motion_no']}号）に対する"
                f"対象委員全員の意見が出そろいました\n"
                f"提案の採択・取り下げ・修正再提案を行ってください。"
            )
        else:
            head = (
                f"【連携申請審査 リマインド】対象委員全員の意見が出そろいました\n"
                f"審査期限を待たずに結論を出せる状態です。"
            )
    else:  # overdue
        review_end = case.get('review_end')
        # review_end は DATETIME。日時で表示する。
        end_str = review_end.strftime('%Y-%m-%d %H:%M') if review_end else '（未設定）'
        head = (
            f"【連携申請審査 リマインド】審査期限を過ぎています（期限：{end_str}）\n"
            f"期間を延長するか、判断を下してください。"
        )

    return (
        f"{head}\n"
        f"────────────\n"
        f"案件：{cid}　{title}\n"
        f"{summary_line}\n"
        f"詳細・操作：{url}\n"
        f"────────────\n"
        f"※ このリマインドは1回のみ送信されます。"
        f"不要な場合は案件画面で「期日アラート」をOFFにできます。"
    )


# ════════════════════════════════════════════════════════════
# メイン処理
# ════════════════════════════════════════════════════════════

def evaluate_case(case):
    """
    1案件を評価し、送るべきリマインド種別を返す。
    送信不要なら None。
    """
    case_id  = case['id']
    round_no = case.get('review_round') or 1
    now      = get_jst_now()

    eligible = get_eligible_committee_ids(case_id, round_no)
    # 未決の委員長提案があるときは、その提案への意見表明を「全員表明」の対象にする
    motion = get_open_motion(case_id)
    case['_open_motion'] = motion
    voted = motion['voted'] if motion else get_voted_user_ids(case_id, round_no)

    # review_end は DATETIME（アラート基準日時）。現在日時と比較する。
    review_end = case.get('review_end')
    is_overdue = bool(review_end) and (review_end < now)
    all_voted  = bool(eligible) and all(uid in voted for uid in eligible)

    last_kind = case.get('last_reminder_kind')

    # ── 優先順位：期限超過（overdue）を先に判定 ──
    if is_overdue:
        # overdue は1案件1回のみ。送信済みならスキップ。
        if last_kind == 'overdue':
            return None
        return 'overdue'

    # ── 期限前で、対象委員全員の意見が出そろった ──
    if all_voted:
        # all_voted も1案件1回のみ。
        if last_kind == 'all_voted':
            return None
        return 'all_voted'

    return None


def build_vote_summary(case_id, round_no):
    """リマインド本文用の簡易集計。"""
    eligible = get_eligible_committee_ids(case_id, round_no)
    counts   = {'approve': 0, 'abstain': 0, 'reject': 0}
    voted    = set()
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT user_id, vote FROM ext_engagement_votes
            WHERE case_id = %s AND round_no = %s
        """, (case_id, round_no))
        for r in cursor.fetchall():
            if r['vote'] in counts:
                counts[r['vote']] += 1
            voted.add(r['user_id'])
    except Exception as e:
        logging.error("build_vote_summary error: %s", e)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()
    return {
        'counts':      counts,
        'total_voted': len(voted),
        'eligible':    len(eligible),
    }


def main():
    logging.info("=== ext_engagement 期日チェック開始 ===")
    cases = get_reviewing_cases()
    logging.info("審議中（アラートON）の案件: %d 件", len(cases))

    sent  = 0
    for case in cases:
        case_id  = case['id']
        round_no = case.get('review_round') or 1
        kind = evaluate_case(case)
        if not kind:
            continue

        vote_summary = build_vote_summary(case_id, round_no)
        text = build_reminder_text(case, kind, vote_summary)

        # 送信先：実質的な委員長（acting優先、なければ委員長プール全員）
        chair_users = get_effective_chair_users(case)
        if not chair_users:
            logging.warning("案件 %s: 委員長が特定できずリマインド送信不可", case_id)
            continue

        # 委員長へDM。宛先はメールアドレス（slack_user_id カラムは使わない）。
        # kind（overdue / all_voted）を通知ログの種別定数へ対応づける。
        nl_kind = (notify_log.KIND_REMINDER_OVERDUE if kind == 'overdue'
                   else notify_log.KIND_REMINDER_ALLVOTED)
        nl_summary = ('審査期限の超過によるリマインド' if kind == 'overdue'
                      else ('委員長提案への全員の意見表明によるリマインド'
                            if case.get('_open_motion')
                            else '対象委員全員の意見表明によるリマインド'))
        any_ok = False
        for cu in chair_users:
            email = cu.get('email')
            chair_name = cu.get('full_name') or ''
            if not email:
                logging.warning("案件 %s: 委員長 %s（user_id=%s）の"
                                "メールアドレス未登録",
                                case_id, chair_name, cu.get('user_id'))
                # メールアドレス未登録も「送れなかった」記録として残す
                _record_reminder_log(
                    case_id, round_no, nl_kind, f'委員長 {chair_name}',
                    False, 'メールアドレス未登録', nl_summary)
                continue
            ok, res = send_chair_dm(email, text)
            _record_reminder_log(
                case_id, round_no, nl_kind, f'委員長 {chair_name}',
                ok, (None if ok else res), nl_summary)
            if ok:
                any_ok = True
                logging.info("案件 %s: 委員長 %s へ %s リマインド送信",
                             case_id, chair_name, kind)
            else:
                logging.error("案件 %s: DM送信失敗（委員長 %s, %s）: %s",
                              case_id, chair_name, email, res)

        # 1名でも送れたら送信済みとして記録（二重送信を防ぐ）
        if any_ok:
            mark_reminder_sent(case_id, kind)
            sent += 1

    logging.info("=== 完了：%d 件のリマインドを送信 ===", sent)


def _record_reminder_log(case_id, round_no, nl_kind, target_label,
                         ok, error, summary):
    """
    委員長リマインドの送信結果を ext_engagement_notify_log に記録する。
    記録のために短時間DB接続を開いて閉じる。失敗しても本処理は止めない。
    """
    conn = None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        notify_log.record(
            conn, case_id, round_no, nl_kind,
            notify_log.TARGET_DM, target_label,
            ok, error=error, summary=summary, created_by=None)
    except Exception as e:
        logging.warning("リマインド通知ログの記録に失敗（案件 %s）: %s",
                         case_id, e)
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


if __name__ == '__main__':
    main()
