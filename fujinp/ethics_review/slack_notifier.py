"""
slack_notifier.py
ethics_review（研究倫理審査）用 Slack 通知モジュール（2026-10-19 から Slack_port 経由）。

配置：
    ethics_review/slack_notifier.py（routes.py と同じディレクトリ）

送信の経路（2026-10-19）：
    Slack とのやりとりはすべて Slack_port（fujinp/slack_port）に頼む。
    このアプリはトークンもチャンネル ID も持たず，Slack_port のチャンネル番号と Bot の名前だけを使う。
    チャンネル通知は port.notify，個人宛 DM は port.notify_dm。
    Slack_port への登録（register）は __init__.py の読み込み時に行う（register_to_slack_port）。
    以前の config の SLACK_BOT_TOKEN・ETHICS_SLACK_*_CHANNEL_ID はもう読まない。
    通知の記録は，このアプリの表 ethics_notifications にすべて残し，体制運営パネルの「通知一覧」で見る（2026-10-19）。

送り先：
    admin がトップの「設定」の画面で選び，表 ethics_settings に入る。
      slack_ch_no            … 既定のチャンネル番号（すべての通知の送り先）
      slack_business_ch_no   … 業務会議（事務局）向けの通知の送り先．空なら既定
      slack_committee_ch_no  … 委員会向けの通知の送り先．空なら既定
      slack_bot              … 投稿と DM に使う Bot（config.py の SLACK_BOTS の鍵）
    設定が無いときは SLACK_PORT_CH_NO・SLACK_PORT_BOT の既定値を使う。
    DM には Bot に users:read.email と im:write のスコープが要る。

設計原則：
    この関数は「失敗しても呼び出し元を巻き込まない」ことを最優先とする。
    Slack への送信が失敗しても例外を投げず、{'ok': False, ...} を返すだけ。
    審査という本処理は、通知の成否に関わらず成立させる。
"""
import logging

SLACK_PORT_APP = 'ethics_review'
SLACK_PORT_DISPLAY = '研究倫理審査'
SLACK_PORT_CH_NO = 6            # 既定のチャンネル番号（Slack_port の 6 番）
SLACK_PORT_BOT = 'FujinpBot'    # 既定の Bot（config.py の SLACK_BOTS の鍵）

SETTINGS_DDL = (
    "CREATE TABLE IF NOT EXISTS `ethics_settings` ("
    "`name` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '設定の名前', "
    "`value` text COLLATE utf8mb4_unicode_ci COMMENT '値', "
    "`updated_at` datetime DEFAULT NULL, "
    "`updated_by` int DEFAULT NULL COMMENT '変えた人の user_id', "
    "PRIMARY KEY (`name`)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
_settings_ready = {'ok': False}
_KEYS = ('slack_ch_no', 'slack_business_ch_no', 'slack_committee_ch_no', 'slack_bot')

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


def get_slack_settings():
    """送り先の設定 {'ch_no', 'business_ch_no', 'committee_ch_no', 'bot'}．
       業務会議・委員会は未設定なら None（既定のチャンネルを使う）．例外は投げない"""
    out = {'ch_no': SLACK_PORT_CH_NO, 'business_ch_no': None, 'committee_ch_no': None, 'bot': SLACK_PORT_BOT}
    try:
        conn = _db()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT name, value FROM ethics_settings WHERE name IN (%s,%s,%s,%s)", _KEYS)
            vals = {r['name']: r['value'] for r in cur.fetchall()}
            cur.close()
        finally:
            conn.close()
        if vals.get('slack_ch_no'):
            out['ch_no'] = int(vals['slack_ch_no'])
        if vals.get('slack_business_ch_no'):
            out['business_ch_no'] = int(vals['slack_business_ch_no'])
        if vals.get('slack_committee_ch_no'):
            out['committee_ch_no'] = int(vals['slack_committee_ch_no'])
        if vals.get('slack_bot'):
            out['bot'] = vals['slack_bot']
    except Exception as e:
        logging.warning("slack_notifier(ethics): 設定を読めません（既定値を使います）: %s", e)
    return out


def save_slack_settings(ch_no, business_ch_no, committee_ch_no, bot, user_id=None):
    """送り先を保存する（admin の設定画面から）．業務会議・委員会は None で「既定と同じ」"""
    import datetime
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    vals = (('slack_ch_no', str(int(ch_no))),
            ('slack_business_ch_no', str(int(business_ch_no)) if business_ch_no else ''),
            ('slack_committee_ch_no', str(int(committee_ch_no)) if committee_ch_no else ''),
            ('slack_bot', bot))
    conn = _db()
    try:
        cur = conn.cursor()
        for name, value in vals:
            cur.execute("INSERT INTO ethics_settings (name, value, updated_at, updated_by) VALUES (%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE value=VALUES(value), updated_at=VALUES(updated_at), "
                        "updated_by=VALUES(updated_by)", (name, value, now, user_id))
        conn.commit()
        cur.close()
    finally:
        conn.close()
    register_to_slack_port()


def channel_for(purpose=None, settings=None):
    """用途別の送り先のチャンネル番号．'business' / 'committee' / None（既定）"""
    st = settings or get_slack_settings()
    if purpose == 'business' and st['business_ch_no']:
        return st['business_ch_no']
    if purpose == 'committee' and st['committee_ch_no']:
        return st['committee_ch_no']
    return st['ch_no']


def register_to_slack_port():
    """Slack_port にこのアプリを登録する（__init__.py から読み込み時に呼ぶ）。例外は投げない。"""
    port = _port()
    if port is None:
        logging.warning("slack_notifier(ethics): %s", NO_PORT)
        return {'ok': False, 'error': NO_PORT}
    try:
        return port.register(SLACK_PORT_APP, SLACK_PORT_DISPLAY, bot=get_slack_settings()['bot'])
    except Exception as e:
        logging.warning("slack_notifier(ethics): Slack_port への登録に失敗: %s", e)
        return {'ok': False, 'error': str(e)}


# ════════════════════════════════════════════════════════════════
# 通知の記録（2026-10-19）
#   Slack_port へ頼んだ通知は，成否にかかわらず 1 件ずつ ethics_notifications に残す．
#   いつ・だれ宛て（チャンネルか DM の相手）・どの種類・どの案件・本文・成否・発送の操作者．
#   体制運営パネルの「通知一覧」で見る．記録に失敗しても通知と本処理は止めない．
# ════════════════════════════════════════════════════════════════

NOTIFICATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS `ethics_notifications` ("
    "`id` int NOT NULL AUTO_INCREMENT, "
    "`sent_at` datetime NOT NULL COMMENT '送った時刻（JST）', "
    "`kind` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '通知の種類（申請通知・担当委員依頼通知・審査開始通知・判断通知・アラート発言通知・申請者連絡通知・書類追加通知・審査再開通知・判断結果の申請者DM・試し送信）', "
    "`route` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'channel か dm', "
    "`ch_no` int DEFAULT NULL COMMENT 'Slack_port のチャンネル番号（channel のとき）', `purpose` varchar(16) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '送り先の用途（空＝既定，business，committee）', "
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
    "`sent_by_user_id` int DEFAULT NULL COMMENT '通知のもとになった操作をした人', "
    "PRIMARY KEY (`id`), "
    "KEY `idx_ethics_notif_sent` (`sent_at`), "
    "KEY `idx_ethics_notif_case` (`case_display_id`, `sent_at`)"
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
            case_display_id=None, purpose=None):
    """通知 1 件を ethics_notifications に残す．例外は投げない"""
    try:
        conn = _db()
        try:
            cur = conn.cursor()
            if not _notif_ready['ok']:
                cur.execute(NOTIFICATIONS_DDL)
                _notif_ready['ok'] = True
            case_id = None
            if case_display_id:
                cur.execute("SELECT id FROM ethics_cases WHERE case_display_id = %s", (case_display_id,))
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
                "INSERT INTO ethics_notifications (sent_at, kind, route, ch_no, purpose, ch_label, recipient_email, "
                "recipient_user_id, recipient_name, bot, case_id, case_display_id, body, ok, error, "
                "sent_by_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (_jst_now(), kind, route, ch_no, purpose or None, ch_label, email, r_uid, r_name, bot, case_id,
                 case_display_id, body, 1 if ok else 0,
                 None if ok else str((result or {}).get('error') or ''), _operator_id()))
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        logging.warning("slack_notifier(ethics): 通知の記録に失敗: %s", e)


def _send(text, log_label, purpose=None, case_display_id=None):
    """
    設定の送り先へ text を投稿する内部共通処理。成否にかかわらず ethics_notifications に記録する。
    log_label … 通知の種類名（例 '担当委員依頼通知'）。記録の種類にもなる。
    purpose   … 宛先チャンネルの用途。'business' / 'committee' / None。
                専用の送り先が未設定なら既定のチャンネル。
    """
    port = _port()
    st = get_slack_settings()
    ch_no = channel_for(purpose, st)
    ch_label = None
    if port is None:
        result = {'ok': False, 'error': NO_PORT}
    else:
        ch_label = _channel_label(port, ch_no)
        try:
            result = port.notify(SLACK_PORT_APP, ch_no, text, bot=st['bot'])
        except Exception as e:
            result = {'ok': False, 'error': f'通知送信に失敗しました: {e}'}
    if not result.get('ok'):
        logging.warning("slack_notifier(ethics): %s の送信に失敗 — %s",
                        log_label, result.get('error'))
    _record(log_label, 'channel', text, result, ch_no=ch_no, ch_label=ch_label, bot=st['bot'],
            case_display_id=case_display_id, purpose=purpose)
    return result


def build_application_message(case_display_id, title, applicant_name=None,
                              case_url=None, mention_channel=True):
    """
    「申請がありました」の投稿本文を組み立てる。

    Args:
        case_display_id : 案件番号（ethics_cases.case_display_id）
        title           : 研究課題名（ethics_cases.title）
        applicant_name  : 申請者名（任意。無ければ省略）
        case_url        : 案件ページへのリンク（任意）
        mention_channel : True なら先頭に <!channel>（@channel メンション）を付ける

    Returns:
        str : 投稿本文
    """
    lines = []
    if mention_channel:
        # Slack で @channel として展開される特殊メンション記法
        lines.append('<!channel> 新しい研究倫理審査の申請がありました。')
    else:
        lines.append('新しい研究倫理審査の申請がありました。')

    lines.append('')
    lines.append(f'■ 案件番号：{case_display_id or "（番号未付与）"}')
    lines.append(f'■ 研究課題：{title or "（無題）"}')
    if applicant_name:
        lines.append(f'■ 申請者：{applicant_name}')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')

    return '\n'.join(lines)


def notify_application_submitted(case_display_id, title,
                                 applicant_name=None, case_url=None):
    """
    申請受付を既定のチャンネルへ通知する高レベル関数。
    routes.py の api_submit から呼ぶ。例外は投げない。
    """
    text = build_application_message(
        case_display_id=case_display_id,
        title=title,
        applicant_name=applicant_name,
        case_url=case_url,
        mention_channel=True,
    )
    return _send(text, '申請通知', case_display_id=case_display_id)


# ════════════════════════════════════════════════════════════════
# 審査の節目イベントの通知（2・3・7番ほか）．いずれもチャンネルへの一括通知
# ════════════════════════════════════════════════════════════════

def notify_reviewers_assigned(case_display_id, title, case_url=None):
    """
    【2番】担当委員の指名があったことをチャンネルへ一括通知する。

    誰が指名されたかは明示しない（委員全員に意向確認を促す方式）。
    routes.py の api_assign_reviewers から、指名・commit 後に呼ぶ。

    Returns: {'ok': True/False, ...}（呼び出し元はログ目的で見るだけ）
    """
    lines = [
        '<!channel> 担当審査委員の指名が行われました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
        '',
        '担当を依頼された委員は、できる限り速やかに案件ページから'
        '意向表明（応諾／辞退）をお願いします。',
        '※ ご自身が担当に含まれているかは案件ページでご確認ください。',
    ]
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '担当委員依頼通知', case_display_id=case_display_id)


def notify_review_started(case_display_id, title, case_url=None):
    """
    【3番】審査が開始されたこと（全担当委員が応諾し reviewing に遷移）を
    チャンネルへ一括通知する。

    routes.py の api_reviewer_response から、reviewing への遷移が
    実際に起きたとき・commit 後にのみ呼ぶ。

    Returns: {'ok': True/False, ...}
    """
    lines = [
        '<!channel> 審査が開始されました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
    ]
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '審査開始通知', case_display_id=case_display_id)


def notify_decision(case_display_id, title, decision_label,
                    case_url=None):
    """
    【7番】委員会としての判断（承認／不承認／条件付き承認）が
    宣言されたことをチャンネルへ一括通知する。

    decision_label … STATUS_LABELS による判断結果の表示名
                      （例 '承認' '不承認' '条件付き承認'）

    申請者本人への自動 DM は含まない（申請者 Slack ID 登録の
    仕組みが必要なため次段階）。当面は事務局がこの通知に気づき、
    申請者へ手動で連絡する運用を想定する。

    routes.py の api_decide から、commit 後に呼ぶ。

    Returns: {'ok': True/False, ...}
    """
    lines = [
        '<!channel> 委員会としての判断が示されました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
        f'■ 判断：{decision_label or "（不明）"}',
    ]
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '判断通知', case_display_id=case_display_id)

def notify_alert_message(case_display_id, title, case_url=None):
    """
    【変更C】審査トラックに「アラート発言」が投稿されたことを
    チャンネルへ一括通知する。

    アラート発言とは、委員長・委員が「重要な発言があるので皆すぐ
    確認してほしい」という意図で投稿する発言。Slack には発言の
    中身は載せず、案件番号と「確認してください」の誘導だけを流す
    （アウェアネスのドアベルに徹する。中身は FUJIN-P 側で読む）。

    routes.py の api_post_review_message から、message_kind='alert'
    の投稿・commit 後に呼ぶ。

    Returns: {'ok': True/False, ...}
    """
    lines = [
        '<!channel> 審査案件について重要な発言があります。ご確認ください。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
        '',
        '審査トラックに重要な発言が投稿されました。'
        '担当の方は案件ページでご確認ください。',
    ]
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), 'アラート発言通知', case_display_id=case_display_id)


def notify_applicant_message(case_display_id, title,
                             applicant_name=None, case_url=None):
    """
    申請者が申請者チャンネルにメッセージを発信したことを
    業務会議（事務局）チャンネルへ通知する。

    申請者からの連絡はいつ来るか予測できないため、新規案件提出と
    同様に業務会議へ知らせ、事務局が速やかに気づけるようにする。
    投稿者が申請者本人のときのみ呼ばれる（委員長・事務局・admin の
    投稿では呼ばない）。本文の中身は載せず、連絡があった事実と
    案件番号だけを流す（中身は FUJIN-P 側で読む）。

    routes.py の api_post_applicant_message から、commit 後に呼ぶ。

    Returns: {'ok': True/False, ...}
    """
    lines = [
        '<!channel> 申請者から連絡がありました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
    ]
    if applicant_name:
        lines.append(f'■ 申請者：{applicant_name}')
    lines.append('')
    lines.append('申請者チャンネルに新しいメッセージが投稿されました。'
                 '内容を案件ページでご確認ください。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '申請者連絡通知', purpose='business', case_display_id=case_display_id)


def notify_document_added(case_display_id, title,
                          applicant_name=None, case_url=None):
    """
    申請者が（申請後・審査中に）書類を追加したことを
    委員会チャンネルへ通知する。

    審査が始まっている案件への書類追加は、審査に関わる重要な事案
    なので委員会へ知らせる。新規申請の準備段階での初回アップロード
    では呼ばれない（routes.py 側で状態を判定して制御する）。
    書類の中身は載せず、追加があった事実と案件番号だけを流す。

    routes.py の api_add_document から、commit 後に呼ぶ。

    Returns: {'ok': True/False, ...}
    """
    lines = [
        '<!channel> 申請者が書類を追加しました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
    ]
    if applicant_name:
        lines.append(f'■ 申請者：{applicant_name}')
    lines.append('')
    lines.append('審査中の案件に申請書類が追加されました。'
                 '担当の方は案件ページでご確認ください。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '書類追加通知', purpose='committee', case_display_id=case_display_id)


def notify_review_resumed(case_display_id, title,
                          applicant_name=None, case_url=None):
    """
    申請者が「審査再開申請」を押し、審議が再開されたことを
    委員会チャンネルへ通知する。

    条件付き承認・審査継続を受けた回答書類の提出が完了したときに、
    routes.py の api_submit から commit 後に呼ぶ。

    Returns: {'ok': True/False, ...}
    """
    lines = [
        '<!channel> 申請者から回答書類が提出され、審議が再開されました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
    ]
    if applicant_name:
        lines.append(f'■ 申請者：{applicant_name}')
    lines.append('')
    lines.append('担当の方は案件ページで回答書類をご確認のうえ、'
                 '審議をお願いします。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '審査再開通知', purpose='committee', case_display_id=case_display_id)

# ════════════════════════════════════════════════════════════════
# 個人宛 DM（メールアドレス照合方式．Slack_port の notify_dm）
#   FUJIN-P 側のメールアドレスから Slack ユーザーを引き当てて DM を送る。
#   FUJIN-P と Slack で同じメールアドレスを使っている人にのみ届く。
# ════════════════════════════════════════════════════════════════

def send_dm_by_email(email, text, log_label='DM', case_display_id=None):
    """
    メールアドレスの人へ DM を送る。routes.py からはこれ一本を呼べばよい。
    例外は投げない。メール照合に失敗した場合も {'ok': False} を返すだけ。
    成否にかかわらず ethics_notifications に記録する。
    """
    if not email:
        return {'ok': False, 'error': 'メールアドレスが空です'}
    port = _port()
    bot = get_slack_settings()['bot']
    if port is None:
        result = {'ok': False, 'error': NO_PORT}
    else:
        try:
            result = port.notify_dm(SLACK_PORT_APP, email, text, bot=bot)
        except Exception as e:
            result = {'ok': False, 'error': f'DM送信に失敗しました: {e}'}
    if not result.get('ok'):
        logging.warning("slack_notifier(ethics): %s の送信に失敗 — %s",
                        log_label, result.get('error'))
    _record(log_label, 'dm', text, result, email=email, bot=bot, case_display_id=case_display_id)
    return result
