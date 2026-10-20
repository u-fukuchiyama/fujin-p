"""
slack_notifier.py
ext_engagement（連携申請審査）用 Slack 通知モジュール（2026-10-19 から Slack_port 経由）。

配置：
    kitare_deliberation/engagement/slack_notifier.py（routes.py と同じディレクトリ）
    期日チェックのスクリプト check_deadlines.py からも，同じディレクトリの
    モジュールとして import される（Flask の外で動く）。

この連携審査アプリで使う通知：
  1. チャンネル投稿（連携審査委員会のチャンネルへ）
     - 審議開始宣言（notify_review_started）
     - 審議コメント通知（notify_review_comment．opt in）
     - 結論通知（notify_conclusion）
     - 委員長提案の起案・採択・取り下げ（notify_motion）
  2. 個人宛 DM（メールアドレス照合方式．send_dm_by_email）
     - 案件登録通知（委員長へ），委員長リマインド（check_deadlines.py から）

送信の経路（2026-10-19）：
    Slack とのやりとりはすべて Slack_port（fujinp/slack_port）に頼む。
    このアプリはトークンもチャンネル ID も持たず，Slack_port のチャンネル番号と Bot の名前だけを使う。
    チャンネル通知は port.notify，DM は port.notify_dm。
    Slack_port への登録（register）は，__init__.py の読み込み時と，送信の前（そのプロセスで
    まだ登録していなければ）に行う。check_deadlines.py のように Flask の外で動くときも登録される。
    以前の config の SLACK_BOT_TOKEN はもう読まない。
    送った通知は，成否にかかわらず表 ext_engagement_notifications に本文ごと残し，
    トップ画面の「通知一覧」で見る（2026-10-19）。

送り先：
    admin がトップの「設定」の画面で選び，表 ext_engagement_settings に入る。
      slack_ch_no … 通知を投稿する Slack_port のチャンネル番号
      slack_bot   … 投稿と DM に使う Bot（config.py の SLACK_BOTS の鍵）
    チャンネル番号を一度も保存していないときは，Slack_port の登録のうち，チャンネル ID が
    config の EXT_ENGAGEMENT_SLACK_CHANNEL_ID と同じものを使う（これまでの送り先を引き継ぐため）。
    見つからなければ送らず，誤りを返す。Bot の既定は SLACK_PORT_BOT。
    DM には Bot に users:read.email と im:write のスコープが要る。

設計原則：
    この関数群は「失敗しても呼び出し元を巻き込まない」ことを最優先とする。
    Slack への送信が失敗しても例外を投げず、{'ok': False, ...} を返すだけ。
"""
import logging

SLACK_PORT_APP = 'ext_engagement'
SLACK_PORT_DISPLAY = '連携申請審査'
SLACK_PORT_BOT = 'FujinpBot'    # 既定の Bot（config.py の SLACK_BOTS の鍵）

SETTINGS_DDL = (
    "CREATE TABLE IF NOT EXISTS `ext_engagement_settings` ("
    "`name` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '設定の名前', "
    "`value` text COLLATE utf8mb4_unicode_ci COMMENT '値', "
    "`updated_at` datetime DEFAULT NULL, "
    "`updated_by` int DEFAULT NULL COMMENT '変えた人の user_id', "
    "PRIMARY KEY (`name`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
_state = {'ready': False, 'registered': False}

NO_PORT = 'Slack_port が導入されていません（Slack 通知は送れません）'
NO_CHANNEL = ('Slack 通知の送り先のチャンネルが決まっていません'
              '（連携申請審査の「設定」でチャンネルを選んでください）')


def _port():
    """Slack_port の api．無ければ None（アプリの中でも，check_deadlines.py からでも探す）"""
    try:
        from ...slack_port import api
        return api
    except Exception:
        try:
            from fujinp.slack_port import api
            return api
        except Exception:
            return None


def _db():
    import mysql.connector
    from db import DatabaseConfig
    conn = mysql.connector.connect(**DatabaseConfig.default())
    if not _state['ready']:
        cur = conn.cursor()
        cur.execute(SETTINGS_DDL)
        conn.commit()
        cur.close()
        _state['ready'] = True
    return conn


def _legacy_ch_no(port):
    """config の EXT_ENGAGEMENT_SLACK_CHANNEL_ID と同じチャンネル ID を持つ Slack_port の番号．無ければ None"""
    try:
        from config import Config
        cid = getattr(Config, 'EXT_ENGAGEMENT_SLACK_CHANNEL_ID', None)
        if not cid or port is None:
            return None
        for r in port._channel_rows():
            if r.get('channel_id') == cid:
                return r['ch_no']
    except Exception as e:
        logging.warning("slack_notifier(ext_engagement): 以前の送り先を探せません: %s", e)
    return None


def get_slack_settings(port=None):
    """送り先 {'ch_no', 'bot', 'ch_from'}．ch_from は 'saved'（設定）/ 'legacy'（以前の config から）/ None（未定）．
       例外は投げない"""
    out = {'ch_no': None, 'bot': SLACK_PORT_BOT, 'ch_from': None}
    try:
        conn = _db()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT name, value FROM ext_engagement_settings WHERE name IN ('slack_ch_no', 'slack_bot')")
            vals = {r['name']: r['value'] for r in cur.fetchall()}
            cur.close()
        finally:
            conn.close()
        if vals.get('slack_ch_no'):
            out['ch_no'], out['ch_from'] = int(vals['slack_ch_no']), 'saved'
        if vals.get('slack_bot'):
            out['bot'] = vals['slack_bot']
    except Exception as e:
        logging.warning("slack_notifier(ext_engagement): 設定を読めません: %s", e)
    if out['ch_no'] is None:
        n = _legacy_ch_no(port if port is not None else _port())
        if n is not None:
            out['ch_no'], out['ch_from'] = n, 'legacy'
    return out


def save_slack_settings(ch_no, bot, user_id=None):
    """送り先を保存する（admin の設定画面から）"""
    import datetime
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    conn = _db()
    try:
        cur = conn.cursor()
        for name, value in (('slack_ch_no', str(int(ch_no))), ('slack_bot', bot)):
            cur.execute("INSERT INTO ext_engagement_settings (name, value, updated_at, updated_by) "
                        "VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE value=VALUES(value), "
                        "updated_at=VALUES(updated_at), updated_by=VALUES(updated_by)",
                        (name, value, now, user_id))
        conn.commit()
        cur.close()
    finally:
        conn.close()
    _state['registered'] = False
    register_to_slack_port()


def register_to_slack_port():
    """Slack_port にこのアプリを登録する。例外は投げない。"""
    port = _port()
    if port is None:
        logging.warning("slack_notifier(ext_engagement): %s", NO_PORT)
        return {'ok': False, 'error': NO_PORT}
    try:
        r = port.register(SLACK_PORT_APP, SLACK_PORT_DISPLAY, bot=get_slack_settings(port)['bot'])
        _state['registered'] = True
        return r
    except Exception as e:
        logging.warning("slack_notifier(ext_engagement): Slack_port への登録に失敗: %s", e)
        return {'ok': False, 'error': str(e)}


def _ready_port():
    """(port, None) または (None, エラー文)．このプロセスで未登録なら登録してから渡す"""
    port = _port()
    if port is None:
        return None, NO_PORT
    if not _state['registered']:
        register_to_slack_port()
    return port, None


# ════════════════════════════════════════════════════════════════
# 通知の記録（2026-10-19）
#   Slack_port へ頼んだ通知は，成否にかかわらず 1 件ずつ ext_engagement_notifications に残す．
#   いつ・だれ宛て（チャンネルか DM の相手）・どの種類・どの案件・本文・成否・発送の操作者．
#   トップ画面の「通知一覧」で見る．記録に失敗しても通知と本処理は止めない．
# ════════════════════════════════════════════════════════════════

NOTIFICATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS `ext_engagement_notifications` ("
    "`id` int NOT NULL AUTO_INCREMENT, "
    "`sent_at` datetime NOT NULL COMMENT '送った時刻（JST）', "
    "`kind` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '通知の種類（案件登録通知DM・審議開始通知・審議コメント通知・結論通知・委員長提案通知・委員長リマインドDM・試し送信）', "
    "`route` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'channel か dm', "
    "`ch_no` int DEFAULT NULL COMMENT 'Slack_port のチャンネル番号（channel のとき）', "
    "`ch_label` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'チャンネルの名前（送ったときのもの）', "
    "`recipient_email` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'DM の宛先メール', "
    "`recipient_user_id` int DEFAULT NULL COMMENT 'DM の宛先の users.id（メールで引けたとき）', "
    "`recipient_name` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'DM の宛先の氏名（送ったときのもの）', "
    "`bot` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '使った Bot', "
    "`case_id` int DEFAULT NULL, "
    "`body` text COLLATE utf8mb4_unicode_ci COMMENT '送った本文', "
    "`ok` tinyint(1) NOT NULL DEFAULT '0' COMMENT '送れたか', "
    "`error` text COLLATE utf8mb4_unicode_ci COMMENT '送れなかったときの理由', "
    "`sent_by_user_id` int DEFAULT NULL COMMENT '通知のもとになった操作をした人（期日チェックの自動送信は NULL）', "
    "PRIMARY KEY (`id`), "
    "KEY `idx_ext_notif_sent` (`sent_at`), "
    "KEY `idx_ext_notif_case` (`case_id`, `sent_at`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
_notif_ready = {'ok': False}


def _jst_now():
    import datetime
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)


def _operator_id():
    """いまの操作者の user_id（ログイン中の画面操作から）．バッチなどでは None"""
    try:
        from flask import session, has_request_context
        if has_request_context():
            return session.get('user_id')
    except Exception:
        pass
    return None


def _channel_label(port, ch_no):
    try:
        for c in port.channels():
            if c.get('no') == ch_no:
                return c.get('label')
    except Exception:
        pass
    return None


def _record(kind, route, body, result, *, ch_no=None, ch_label=None, email=None, bot=None,
            case_id=None):
    """通知 1 件を ext_engagement_notifications に残す．例外は投げない．
       check_deadlines.py（Flask の外）から呼ばれたときも動く"""
    try:
        conn = _db()
        try:
            cur = conn.cursor()
            if not _notif_ready['ok']:
                cur.execute(NOTIFICATIONS_DDL)
                _notif_ready['ok'] = True
            r_uid = r_name = None
            if email:
                cur.execute("SELECT id, full_name FROM users WHERE LOWER(email) = LOWER(%s) LIMIT 1",
                            (email.strip(),))
                row = cur.fetchone()
                if row:
                    r_uid, r_name = row[0], row[1]
            ok = bool((result or {}).get('ok'))
            cur.execute(
                "INSERT INTO ext_engagement_notifications (sent_at, kind, route, ch_no, ch_label, "
                "recipient_email, recipient_user_id, recipient_name, bot, case_id, body, ok, error, "
                "sent_by_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (_jst_now(), kind, route, ch_no, ch_label, email, r_uid, r_name, bot, case_id,
                 body, 1 if ok else 0,
                 None if ok else str((result or {}).get('error') or ''), _operator_id()))
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        logging.warning("slack_notifier(ext_engagement): 通知の記録に失敗: %s", e)


def _send(text, log_label, case_id=None):
    """設定の送り先のチャンネルへ text を投稿する内部共通処理。
       成否にかかわらず ext_engagement_notifications に記録する（2026-10-19）。"""
    port, err = _ready_port()
    ch_no = ch_label = None
    bot = None
    if err:
        result = {'ok': False, 'error': err}
    else:
        try:
            st = get_slack_settings(port)
            ch_no, bot = st['ch_no'], st['bot']
            if ch_no is None:
                result = {'ok': False, 'error': NO_CHANNEL}
            else:
                ch_label = _channel_label(port, ch_no)
                result = port.notify(SLACK_PORT_APP, ch_no, text, bot=bot)
        except Exception as e:
            result = {'ok': False, 'error': f'通知送信に失敗しました: {e}'}
    if not result.get('ok'):
        logging.warning("slack_notifier(ext_engagement): %s の送信に失敗 — %s",
                        log_label, result.get('error'))
    _record(log_label, 'channel', text, result, ch_no=ch_no, ch_label=ch_label, bot=bot,
            case_id=case_id)
    return result


def notify_conclusion(text, case_id=None):
    """結論宣言を連携審査委員会チャンネルへ投稿する（routes.py の api_case_conclude / api_notify_slack から）。
       本文は呼び出し側が build_conclusion_message で組み立てる。例外は投げない。"""
    return _send(text, '結論通知', case_id)


def notify_review_started(text, case_id=None):
    """審議開始宣言を連携審査委員会チャンネルへ投稿する（routes.py の api_send_to_review から）。
       初回の宣言と，委員構成を変更しての再宣言の両方で使う。例外は投げない。"""
    return _send(text, '審議開始通知', case_id)


def notify_review_comment(text, case_id=None):
    """審議コメントの投稿を連携審査委員会チャンネルへ通知する（routes.py の api_comment_post から．opt in）。
       例外は投げない。"""
    return _send(text, '審議コメント通知', case_id)


def notify_motion(text, case_id=None):
    """委員長提案（起案・採択・取り下げ）を連携審査委員会チャンネルへ通知する（motions.py から）。
       例外は投げない。"""
    return _send(text, '委員長提案通知', case_id)


# ════════════════════════════════════════════════════════════════
# 個人宛 DM（メールアドレス照合方式．Slack_port の notify_dm）
#   FUJIN-P 側のメールアドレスから Slack ユーザーを引き当てて DM を送る。
#   FUJIN-P と Slack で同じメールアドレスを使っている人にのみ届く。
# ════════════════════════════════════════════════════════════════

def send_dm_by_email(email, text, log_label='DM', case_id=None):
    """メールアドレスの人へ DM を送る。routes.py・check_deadlines.py から呼ぶ。
       例外は投げない。メール照合に失敗した場合も {'ok': False} を返すだけ。
       成否にかかわらず ext_engagement_notifications に記録する。"""
    if not email:
        return {'ok': False, 'error': 'メールアドレスが空です'}
    port, err = _ready_port()
    bot = None
    if err:
        result = {'ok': False, 'error': err}
    else:
        try:
            bot = get_slack_settings(port)['bot']
            result = port.notify_dm(SLACK_PORT_APP, email, text, bot=bot)
        except Exception as e:
            result = {'ok': False, 'error': f'DM送信に失敗しました: {e}'}
    if not result.get('ok'):
        logging.warning("slack_notifier(ext_engagement): %s の送信に失敗 — %s",
                        log_label, result.get('error'))
    _record(log_label, 'dm', text, result, email=email, bot=bot, case_id=case_id)
    return result
