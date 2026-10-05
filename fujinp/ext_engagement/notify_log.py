"""
notify_log.py
ext_engagement（連携申請審査）通知ログ記録モジュール。

配置：
    ext_engagement/notify_log.py
    （routes.py / check_deadlines.py と同じディレクトリ）

役割：
    通知（案件登録DM・審議開始宣言・審議コメント・結論・委員長リマインド）が
    送られるたびに、その成否を ext_engagement_notify_log テーブルへ
    1行記録する。案件詳細画面の「通知記録」パネルがこのテーブルを表示する。

設計：
    この関数群は「記録に失敗しても呼び出し元を巻き込まない」ことを最優先とする。
    ログ記録の失敗で、通知本体や案件処理を止めてはならない。
    例外は投げず、失敗時は logging に残すだけ。

    DB 接続（mysql.connector の conn）は呼び出し側が用意して渡す。
    routes.py / check_deadlines.py のいずれも接続を持っているため、
    このモジュール自身は接続の作法を持たない。
"""
import logging

# 通知種別の定数（notify_kind カラムに入る値）
KIND_CASE_REGISTERED   = 'case_registered'     # 案件登録DM
KIND_REVIEW_STARTED    = 'review_started'      # 審議開始宣言
KIND_REVIEW_COMMENT    = 'review_comment'      # 審議コメントの通知
KIND_CONCLUSION        = 'conclusion'          # 結論の通知
KIND_REMINDER_OVERDUE  = 'reminder_overdue'    # 委員長リマインド（期限超過）
KIND_REMINDER_ALLVOTED = 'reminder_all_voted'  # 委員長リマインド（全員表明）
KIND_MOTION_PROPOSED   = 'motion_proposed'     # 委員長提案の起案
KIND_MOTION_CLOSED     = 'motion_closed'       # 委員長提案の採択・取り下げ

# 宛先種別の定数（target_kind カラムに入る値）
TARGET_CHANNEL = 'channel'   # 委員会チャンネル
TARGET_DM      = 'dm'        # 委員長へDM

# 通知種別 → 日本語表示名（通知記録パネルでの表示用）
KIND_LABEL = {
    KIND_CASE_REGISTERED:   '案件登録の通知',
    KIND_REVIEW_STARTED:    '審議開始の通知',
    KIND_REVIEW_COMMENT:    '審議コメントの通知',
    KIND_CONCLUSION:        '結論の通知',
    KIND_REMINDER_OVERDUE:  '委員長リマインド（審査期限の超過）',
    KIND_REMINDER_ALLVOTED: '委員長リマインド（全員の意見表明）',
    KIND_MOTION_PROPOSED:   '委員長提案の通知',
    KIND_MOTION_CLOSED:     '委員長提案の採択・取り下げの通知',
}


def record(conn, case_id, round_no, notify_kind, target_kind,
           target_label, ok, error=None, summary=None, created_by=None):
    """
    通知ログを1行記録する。

    Args:
        conn         : mysql.connector のコネクション（呼び出し側が用意）
        case_id      : 案件ID
        round_no     : 送信時点の審議ラウンド（review_round）
        notify_kind  : 通知種別（KIND_* 定数）
        target_kind  : 宛先種別（TARGET_CHANNEL / TARGET_DM）
        target_label : 宛先の表示用文字列（例 '委員会チャンネル' / '委員長 ○○○○'）
        ok           : 送信成功なら True、失敗なら False
        error        : 失敗時のエラー内容（成功時は None）
        summary      : 通知内容の要約（任意）
        created_by   : 操作者のユーザーID。自動送信（リマインド）は None

    Returns:
        記録成功なら True、失敗なら False。
        例外は投げない。失敗しても呼び出し元の処理は止めない。
    """
    try:
        from datetime import datetime
        from pytz import timezone
        now = datetime.now(timezone('Asia/Tokyo')).replace(tzinfo=None)
        status = 'success' if ok else 'failed'
        # error は255文字に収める
        err = (str(error)[:255] if error else None)
        summ = (str(summary)[:255] if summary else None)

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ext_engagement_notify_log
                (case_id, round_no, notify_kind, target_kind, target_label,
                 status, error, summary, created_by, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (case_id, round_no, notify_kind, target_kind, target_label,
              status, err, summ, created_by, now))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        # ログ記録の失敗は本処理を止めない。警告だけ残す。
        logging.warning("notify_log.record 失敗（case_id=%s, kind=%s）: %s",
                         case_id, notify_kind, e)
        return False
