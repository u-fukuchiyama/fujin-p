"""
slack_notifier.py
キターレオンライン審議 用 Slack 通知モジュール（2026-10-19 から Slack_port 経由）。

配置：
    kitare_deliberation/slack_notifier.py（routes.py と同じディレクトリ）

通知の対象（設計メモ v2 の7に対応）：
    1. 申請があったとき   … チャンネル一括通知 ＋ 委員長への個別 DM
    2. 締切が来たとき     … チャンネル一括通知（日次バッチから）
    3. 審議の開始         … チャンネル一括通知（業務会議が受理→開始操作時）
    4. 審議の終了（議決） … チャンネル一括通知
    付随：重要発言（alert）のアウェアネス通知

送信の経路（2026-10-19）：
    Slack とのやりとりはすべて Slack_port（fujinp/slack_port）に頼む。
    このアプリはトークンもチャンネル ID も持たず，Slack_port のチャンネル番号と Bot の名前だけを使う。
    チャンネル通知は port.notify，個人宛 DM は port.notify_dm。
    Slack_port への登録（register）は __init__.py の読み込み時に行う（register_to_slack_port）。
    以前の config の SLACK_BOT_TOKEN・KITARE_SLACK_CHANNEL_ID はもう読まない。
    通知の記録は，このアプリの表 kitare_notifications にすべて残し，業務会議タブの「通知一覧」で見る（2026-10-19）。

送り先：
    審議通知を投稿する Slack_port のチャンネル番号と，投稿と DM に使う Bot（config.py の SLACK_BOTS の鍵）．
    admin がトップの「設定」の画面で選び，表 kitare_settings（slack_ch_no，slack_bot）に入る．
    設定が無いときは SLACK_PORT_CH_NO・SLACK_PORT_BOT の既定値を使う．
    DM には Bot に users:read.email と im:write のスコープが要る。

設計原則：
    この関数は「失敗しても呼び出し元を巻き込まない」ことを最優先とする。
    Slack 送信が失敗しても例外を投げず、{'ok': False, ...} を返すだけ。
    審議という本処理は、通知の成否に関わらず成立させる。
"""
import logging

SLACK_PORT_APP = 'kitare_deliberation'
SLACK_PORT_DISPLAY = 'キターレオンライン審議'
# 送り先の既定値．実際の送り先は admin がトップの「設定」で選び，表 kitare_settings に入る（2026-10-19）
SLACK_PORT_CH_NO = 3            # Slack_port の 3 番（北近畿地域連携機構-運営委員会協議用）
SLACK_PORT_BOT = 'FujinpBot'    # config.py の SLACK_BOTS の鍵

SETTINGS_DDL = (
    "CREATE TABLE IF NOT EXISTS `kitare_settings` ("
    "`name` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '設定の名前', "
    "`value` text COLLATE utf8mb4_unicode_ci COMMENT '値', "
    "`updated_at` datetime DEFAULT NULL, "
    "`updated_by` int DEFAULT NULL COMMENT '変えた人の user_id', "
    "PRIMARY KEY (`name`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
_settings_ready = {'ok': False}

NO_PORT = 'Slack_port が導入されていません（Slack 通知は送れません）'


def _port():
    """Slack_port の api．無ければ None"""
    try:
        from ..slack_port import api
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
    if not _settings_ready['ok']:
        cur = conn.cursor()
        cur.execute(SETTINGS_DDL)
        conn.commit()
        cur.close()
        _settings_ready['ok'] = True
    return conn


def get_slack_target():
    """通知の送り先 (チャンネル番号, Bot の名前)．設定が無ければ既定値．例外は投げない"""
    ch_no, bot = SLACK_PORT_CH_NO, SLACK_PORT_BOT
    try:
        conn = _db()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT name, value FROM kitare_settings WHERE name IN ('slack_ch_no', 'slack_bot')")
            vals = {r['name']: r['value'] for r in cur.fetchall()}
            cur.close()
        finally:
            conn.close()
        if vals.get('slack_ch_no'):
            ch_no = int(vals['slack_ch_no'])
        if vals.get('slack_bot'):
            bot = vals['slack_bot']
    except Exception as e:
        logging.warning("slack_notifier(kitare): 設定を読めません（既定値を使います）: %s", e)
    return ch_no, bot


def save_slack_target(ch_no, bot, user_id=None):
    """送り先を保存する（admin の設定画面から）．Slack_port の登録の既定の Bot も合わせる"""
    import datetime
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    conn = _db()
    try:
        cur = conn.cursor()
        for name, value in (('slack_ch_no', str(int(ch_no))), ('slack_bot', bot)):
            cur.execute("INSERT INTO kitare_settings (name, value, updated_at, updated_by) VALUES (%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE value=VALUES(value), updated_at=VALUES(updated_at), "
                        "updated_by=VALUES(updated_by)", (name, value, now, user_id))
        conn.commit()
        cur.close()
    finally:
        conn.close()
    register_to_slack_port()


def register_to_slack_port():
    """Slack_port にこのアプリを登録する（__init__.py から読み込み時に呼ぶ）。例外は投げない。"""
    port = _port()
    if port is None:
        logging.warning("slack_notifier(kitare): %s", NO_PORT)
        return {'ok': False, 'error': NO_PORT}
    try:
        return port.register(SLACK_PORT_APP, SLACK_PORT_DISPLAY, bot=get_slack_target()[1])
    except Exception as e:
        logging.warning("slack_notifier(kitare): Slack_port への登録に失敗: %s", e)
        return {'ok': False, 'error': str(e)}


# ════════════════════════════════════════════════════════════════
# 通知の記録（2026-10-19）
#   Slack_port へ頼んだ通知は，成否にかかわらず 1 件ずつ kitare_notifications に残す．
#   いつ・だれ宛て（チャンネルか DM の相手）・どの種類・どの案件・本文・成否・発送の操作者．
#   業務会議タブの「通知一覧」で見る．記録に失敗しても通知と本処理は止めない．
# ════════════════════════════════════════════════════════════════

NOTIFICATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS `kitare_notifications` ("
    "`id` int NOT NULL AUTO_INCREMENT, "
    "`sent_at` datetime NOT NULL COMMENT '送った時刻（JST）', "
    "`kind` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '通知の種類（申請通知・申請通知DM・審議開始通知・議決通知・締切通知・重要発言通知・試し送信）', "
    "`route` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'channel か dm', "
    "`ch_no` int DEFAULT NULL COMMENT 'Slack_port のチャンネル番号（channel のとき）', "
    "`ch_label` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'チャンネルの名前（送ったときのもの）', "
    "`recipient_email` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'DM の宛先メール', "
    "`recipient_user_id` int DEFAULT NULL COMMENT 'DM の宛先の users.id（メールで引けたとき）', "
    "`recipient_name` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'DM の宛先の氏名（送ったときのもの）', "
    "`bot` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '使った Bot', "
    "`case_id` int DEFAULT NULL, "
    "`case_display_id` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL, "
    "`body` text COLLATE utf8mb4_unicode_ci COMMENT '送った本文', "
    "`ok` tinyint(1) NOT NULL DEFAULT '0' COMMENT '送れたか', "
    "`error` text COLLATE utf8mb4_unicode_ci COMMENT '送れなかったときの理由', "
    "`sent_by_user_id` int DEFAULT NULL COMMENT '通知のもとになった操作をした人（締切バッチは NULL）', "
    "PRIMARY KEY (`id`), "
    "KEY `idx_kitare_notif_sent` (`sent_at`), "
    "KEY `idx_kitare_notif_case` (`case_display_id`, `sent_at`)"
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
            case_display_id=None):
    """通知 1 件を kitare_notifications に残す．例外は投げない"""
    try:
        conn = _db()
        try:
            cur = conn.cursor()
            if not _notif_ready['ok']:
                cur.execute(NOTIFICATIONS_DDL)
                _notif_ready['ok'] = True
            case_id = None
            if case_display_id:
                cur.execute("SELECT id FROM kitare_cases WHERE case_display_id = %s", (case_display_id,))
                row = cur.fetchone()
                case_id = row[0] if row else None
            r_uid = r_name = None
            if email:
                cur.execute("SELECT id, full_name FROM users WHERE LOWER(email) = LOWER(%s) LIMIT 1",
                            (email.strip(),))
                row = cur.fetchone()
                if row:
                    r_uid, r_name = row[0], row[1]
            ok = bool((result or {}).get('ok'))
            cur.execute(
                "INSERT INTO kitare_notifications (sent_at, kind, route, ch_no, ch_label, recipient_email, "
                "recipient_user_id, recipient_name, bot, case_id, case_display_id, body, ok, error, "
                "sent_by_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (_jst_now(), kind, route, ch_no, ch_label, email, r_uid, r_name, bot, case_id,
                 case_display_id, body, 1 if ok else 0,
                 None if ok else str((result or {}).get('error') or ''), _operator_id()))
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        logging.warning("slack_notifier(kitare): 通知の記録に失敗: %s", e)


# ════════════════════════════════════════════════════════════════
# チャンネル投稿
# ════════════════════════════════════════════════════════════════

def _send(text, log_label, case_display_id=None):
    """設定の送り先（Slack_port のチャンネル番号と Bot）へ text を投稿する内部共通処理。
       成否にかかわらず kitare_notifications に記録する。"""
    port = _port()
    ch_no, bot = get_slack_target()
    ch_label = None
    if port is None:
        result = {'ok': False, 'error': NO_PORT}
    else:
        ch_label = _channel_label(port, ch_no)
        try:
            result = port.notify(SLACK_PORT_APP, ch_no, text, bot=bot)
        except Exception as e:
            result = {'ok': False, 'error': f'通知送信に失敗しました: {e}'}
    if not result.get('ok'):
        logging.warning("slack_notifier(kitare): %s の送信に失敗 — %s",
                        log_label, result.get('error'))
    _record(log_label, 'channel', text, result, ch_no=ch_no, ch_label=ch_label, bot=bot,
            case_display_id=case_display_id)
    return result


def notify_application_submitted(case_display_id, title, applicant_name=None,
                                 case_url=None):
    """【1】申請があったことをチャンネルへ一括通知する。
       routes.py の api_submit から、commit 後に呼ぶ。"""
    lines = [
        '<!channel> 新しい審議案件の申請がありました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 案件名：{title or "（無題）"}',
    ]
    if applicant_name:
        lines.append(f'■ 申請者：{applicant_name}')
    lines.append('')
    lines.append('業務会議（委員長・事務局）で受理判断をお願いします。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '申請通知', case_display_id)


def notify_review_started(case_display_id, title, deadline=None, case_url=None):
    """【3】審議が開始されたことをチャンネルへ一括通知する。
       routes.py の api_start_review から、commit 後に呼ぶ。"""
    lines = [
        '<!channel> 運営委員会の審議が開始されました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 案件名：{title or "（無題）"}',
    ]
    if deadline:
        lines.append(f'■ 審議期日：{deadline}')
    lines.append('')
    lines.append('委員の皆さまは案件ページで審議にご参加ください。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '審議開始通知', case_display_id)


def notify_decision(case_display_id, title, decision_label, case_url=None):
    """【4】委員長の議決をチャンネルへ一括通知する。
       routes.py の api_decide から、commit 後に呼ぶ。
       decision_label … '可決' '否決' '継続審議' のいずれか。"""
    lines = [
        '<!channel> 委員長による議決が示されました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 案件名：{title or "（無題）"}',
        f'■ 議決：{decision_label or "（不明）"}',
    ]
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '議決通知', case_display_id)


def notify_deadline_reached(case_display_id, title, deadline=None,
                            case_url=None):
    """【2】審議期日が到来したことをチャンネルへ一括通知する。
       日次バッチ（routes.py の api_deadline_batch）から呼ぶ。
       忘れ防止のリマインダ一発。"""
    lines = [
        '<!channel> 審議案件の審議期日が来ています。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 案件名：{title or "（無題）"}',
    ]
    if deadline:
        lines.append(f'■ 審議期日：{deadline}')
    lines.append('')
    lines.append('委員長は議決（可決／否決／継続審議）をご検討ください。'
                 '継続審議とする場合は新しい審議期日を設定してください。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '締切通知', case_display_id)


def notify_alert_message(case_display_id, title, case_url=None):
    """付随：審議トラックに重要発言（alert）が投稿されたことを通知する。
       中身は載せず案件番号と誘導だけ。
       routes.py の api_post_review_message から、alert 投稿後に呼ぶ。"""
    lines = [
        '<!channel> 審議中の案件について重要な発言があります。ご確認ください。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 案件名：{title or "（無題）"}',
        '',
        '審議トラックに重要な発言が投稿されました。'
        '担当の方は案件ページでご確認ください。',
    ]
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '重要発言通知', case_display_id)


# ════════════════════════════════════════════════════════════════
# 個人宛 DM（メールアドレス照合方式．Slack_port の notify_dm）
#   FUJIN-P 上のメールから Slack ユーザーを引き当て DM する。
#   FUJIN-P と Slack で同じメールの人にのみ届く。
# ════════════════════════════════════════════════════════════════

def send_dm_by_email(email, text, log_label='DM', case_display_id=None):
    """メールアドレスの人へ DM を送る。例外は投げない。照合失敗時も {'ok': False} を返すだけ。
       成否にかかわらず kitare_notifications に記録する。"""
    if not email:
        return {'ok': False, 'error': 'メールアドレスが空です'}
    port = _port()
    bot = get_slack_target()[1]
    if port is None:
        result = {'ok': False, 'error': NO_PORT}
    else:
        try:
            result = port.notify_dm(SLACK_PORT_APP, email, text, bot=bot)
        except Exception as e:
            result = {'ok': False, 'error': f'DM送信に失敗しました: {e}'}
    if not result.get('ok'):
        logging.warning("slack_notifier(kitare): %s の送信に失敗 — %s",
                        log_label, result.get('error'))
    _record(log_label, 'dm', text, result, email=email, bot=bot, case_display_id=case_display_id)
    return result


def notify_application_to_recipients(recipient_emails, case_display_id, title,
                                     applicant_name=None, case_url=None):
    """【1の後半】申請があったことを宛先（当面は委員長）へ DM で通知する。
       routes.py の api_submit から、commit 後・チャンネル通知とあわせて呼ぶ。

    Args:
        recipient_emails : 宛先メールアドレスのリスト（委員長たちのメール。
                           将来 routes 側で宛先グループを差し替えれば変わる）。
    Returns:
        {'ok':..., 'sent': 成功数, 'failed': 失敗数, 'results': [...]}
    例外は投げない。1人への失敗が他をブロックしない。
    """
    lines = [
        '新しい審議案件の申請がありました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 案件名：{title or "（無題）"}',
    ]
    if applicant_name:
        lines.append(f'■ 申請者：{applicant_name}')
    lines.append('')
    lines.append('業務会議で受理判断をお願いします。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    text = '\n'.join(lines)

    results, sent, failed = [], 0, 0
    for email in (recipient_emails or []):
        if not email:
            continue
        r = send_dm_by_email(email, text, log_label='申請通知DM',
                             case_display_id=case_display_id)
        ok = bool(r.get('ok'))
        results.append({'email': email, 'ok': ok, 'error': r.get('error')})
        if ok:
            sent += 1
        else:
            failed += 1

    if sent == 0 and failed == 0:
        logging.warning("slack_notifier(kitare): 申請通知DM — 宛先が0件です")
        return {'ok': False, 'sent': 0, 'failed': 0, 'results': []}
    return {'ok': sent > 0, 'sent': sent, 'failed': failed, 'results': results}
