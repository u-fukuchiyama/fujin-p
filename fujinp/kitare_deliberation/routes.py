"""
kitare_deliberation - ルート定義
キターレオンライン審議

研究倫理審査(ethics_review)を簡単化した、固定メンバーによる
オンライン審議システム。設計メモ v2 に対応。

組織構造（固定メンバー。北近畿地域連携機構のグループで表現）:
  業務会議  : まいぐる '北近畿地域連携機構_業務会議' 所属メンバー。
              委員長と事務局。申請の受理判断・書類整備・審議開始を担う。
  運営委員会: まいぐる '北近畿地域連携機構_運営委員会' 所属メンバー。
              委員。附議された案件を審議する。委員長も運営委員。
  申請者    : 上記いずれにも属さない一般ユーザー（パブリック）。
              案件を申請でき、自分の案件を閲覧できる。

  ※ 委員長は業務会議・運営委員会の両方に属する。
  ※ 担当委員は opt-out 方式：運営委員会グループの全員が既定で担当。
     案件ごとの指名・応諾はしない。COI と判明した委員だけ kitare_coi に
     記録して担当から外す（委員長も外しうる）。

ステータス遷移:
  channel_open        申請者がチャンネルを立ち上げた直後
  documents_uploading 書類アップロード中
  submitted           申請ボタン押下。業務会議の受理判断待ち
  accepted_review     附議決定。運営委員会で審議中
  approved            可決（終局）
  rejected            否決（終局）
  not_accepted        不受理（業務会議が附議せず・終局）
  withdrawn           取り下げ（終局）
  terminated          打ち切り（終局）
  ※「継続審議」は委員長が選ぶと新しい審議期日が設定され、
    accepted_review のまま審議が再開される（ステータスではない）。
"""
import datetime
import logging
import csv
import io
import os
import re
import uuid

from pytz import timezone

from flask import (
    render_template, request, jsonify, session, Response, redirect, url_for,
    send_file, abort
)
from werkzeug.utils import secure_filename
import mysql.connector
from auth import redirect_to_dashboard

from config import Config
from db import DatabaseConfig
from decorators import login_required
from ..user_groups.utils import user_is_in_group, get_group_member_ids

from . import kitare_deliberation_bp

# ────────────────────────────────────────────
# 定数
# ────────────────────────────────────────────

JST = timezone('Asia/Tokyo')

# まいぐるグループ名（北近畿地域連携機構の体系）
GROUP_BUSINESS  = '北近畿地域連携機構_業務会議'          # 委員長＋事務局
GROUP_COMMITTEE = '北近畿地域連携機構_運営委員会'        # 委員（委員長を含む）
GROUP_CHAIR     = '北近畿地域連携機構_運営委員会_委員長'  # 委員長

STATUS_LABELS = {
    'channel_open':         'チャンネル開設',
    'documents_uploading':  '書類提出中',
    'submitted':            '申請受付（受理判断待ち）',
    'accepted_review':      '審議中',
    'approved':             '可決',
    'rejected':             '否決',
    'not_accepted':         '不受理',
    'withdrawn':            '取り下げ',
    'terminated':           '打ち切り',
}

# 終局ステータス（変更不可）
TERMINAL_STATUSES = {'approved', 'rejected', 'not_accepted',
                     'withdrawn', 'terminated'}

# パブリックで「受付中」と見せるステータス
ACTIVE_STATUSES = {'channel_open', 'documents_uploading',
                   'submitted', 'accepted_review'}

# 議決アクション → 結果ラベル
DECISION_LABELS = {
    'approve':  '可決',
    'reject':   '否決',
    'continue': '継続審議',
}

MAX_DOCUMENTS = 10                  # 1案件あたりの最大書類数
MAX_FILE_SIZE = 20 * 1024 * 1024    # 1ファイル20MB
MAX_BODY_LEN  = 20000

# ファイル保管ルート。非公開の投稿データなので <app>/static/ に置く
# （~/static/ は nginx が認証なしで配信するため使わない）。
# 配信は api_download_document（login_required + 権限判定）が担う。
KITARE_DOCS_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'static', 'kitare_docs'
)

# 締切バッチ用トークン（config から。Scheduled Task が叩くURLの保護）
DEADLINE_BATCH_TOKEN = getattr(Config, 'KITARE_DEADLINE_BATCH_TOKEN', None)


# ────────────────────────────────────────────
# 日時・ID ユーティリティ
# ────────────────────────────────────────────

def get_jst_now():
    return datetime.datetime.now(JST).replace(tzinfo=None)


def get_jst_today():
    return get_jst_now().date()


def get_current_fiscal_year():
    now = get_jst_now()
    return now.year if now.month >= 4 else now.year - 1


def fmt_date(d):
    """date/datetime を 'YYYY-MM-DD' 文字列にする。
       すでに文字列が渡された場合はそのまま返す（取り違えでの例外を防ぐ）。"""
    if not d:
        return ''
    if isinstance(d, str):
        return d
    if isinstance(d, datetime.datetime):
        d = d.date()
    return d.isoformat()


def fmt_datetime(dt):
    if not dt:
        return ''
    return dt.strftime('%Y-%m-%d %H:%M')


def format_case_id(fiscal_year, seq_no):
    """K-2026-001 形式"""
    return f"K-{fiscal_year}-{seq_no:03d}"


def _conn():
    return mysql.connector.connect(**DatabaseConfig.default())


# ────────────────────────────────────────────
# カテゴリー機能：冪等ブートストラップ
#   schema_categories.sql を流し忘れても落ちないよう、
#   テーブル／列の存在を確認して足りなければ作る。
#   初回のカテゴリー系アクセス時に一度だけ走らせる。
# ────────────────────────────────────────────

_category_schema_ready = False


def ensure_category_schema():
    """kitare_categories テーブルと kitare_cases.category_id 列を保証する。
       既にある場合は何もしない（冪等）。"""
    global _category_schema_ready
    if _category_schema_ready:
        return
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor()

        # 1) 定義テーブル
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS kitare_categories ("
            "  id INT AUTO_INCREMENT PRIMARY KEY,"
            "  name VARCHAR(100) NOT NULL,"
            "  sort_order INT NOT NULL DEFAULT 0,"
            "  is_active TINYINT(1) NOT NULL DEFAULT 1,"
            "  created_by_user_id INT NULL,"
            "  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "  UNIQUE KEY uq_kitare_cat_name (name),"
            "  KEY idx_kitare_cat_active (is_active, sort_order)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
            "COLLATE=utf8mb4_unicode_ci")

        # 2) kitare_cases.category_id 列（無ければ追加）
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name = 'kitare_cases' "
            "  AND column_name = 'category_id'")
        has_col = cursor.fetchone()[0]
        if not has_col:
            cursor.execute(
                "ALTER TABLE kitare_cases "
                "ADD COLUMN category_id INT NULL AFTER public_title")
            cursor.execute(
                "ALTER TABLE kitare_cases "
                "ADD KEY idx_kitare_case_category (category_id)")
            # 外部キーは失敗しても致命的でないので個別に握りつぶす
            try:
                cursor.execute(
                    "ALTER TABLE kitare_cases "
                    "ADD CONSTRAINT fk_kitare_case_category "
                    "FOREIGN KEY (category_id) "
                    "REFERENCES kitare_categories(id) ON DELETE SET NULL")
            except Exception as fk_err:
                logging.warning("ensure_category_schema: FK追加スキップ: %s",
                                fk_err)

        # 3) kitare_cases.cover_letter 列（添書。無ければ追加）
        #    申請者が送付時に書くメッセージ。単純なテキストは PDF 化せず
        #    ここにベタ打ちできる。申請前のみ編集可、申請後は固定表示。
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name = 'kitare_cases' "
            "  AND column_name = 'cover_letter'")
        has_cover = cursor.fetchone()[0]
        if not has_cover:
            cursor.execute(
                "ALTER TABLE kitare_cases "
                "ADD COLUMN cover_letter TEXT NULL AFTER applicant_affiliation")

        conn.commit()
        _category_schema_ready = True
        logging.info("kitare: カテゴリースキーマを確認・整備しました")
    except Exception as e:
        logging.error("ensure_category_schema error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# カテゴリー：取得ヘルパ
# ────────────────────────────────────────────

def get_category(category_id):
    """カテゴリー1件。無効・存在しない場合は None。"""
    if not category_id:
        return None
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, name, sort_order, is_active "
            "FROM kitare_categories WHERE id = %s", (category_id,))
        return cursor.fetchone()
    except Exception as e:
        logging.error("get_category error: %s", e)
        return None
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


def get_category_name(category_id):
    """カテゴリー名。無ければ空文字。"""
    cat = get_category(category_id)
    return cat['name'] if cat else ''


# ────────────────────────────────────────────
# ファイル保管ユーティリティ
# ────────────────────────────────────────────

def _safe_filename(original_name):
    """元のファイル名から安全なファイル名を生成する。
       日本語ファイル名は secure_filename で空になりやすいため、
       拡張子を保持しつつ UUID で衝突回避する命名を採る。"""
    ext = ''
    if original_name and '.' in original_name:
        ext = '.' + original_name.rsplit('.', 1)[1].lower()
        ext = re.sub(r'[^.a-z0-9]', '', ext)[:12]
    return uuid.uuid4().hex + ext


def _case_doc_dir(case_id):
    """case_<id> ディレクトリの絶対パス"""
    return os.path.join(KITARE_DOCS_ROOT, f'case_{case_id}')


def _ensure_case_doc_dir(case_id):
    """書類保存ディレクトリを作成して絶対パスを返す"""
    d = _case_doc_dir(case_id)
    os.makedirs(d, exist_ok=True)
    return d


def _doc_absolute_path(relative_path):
    """DB の file_path（相対パス）から絶対パスを得る。
       パストラバーサル防止のため KITARE_DOCS_ROOT 配下に限定する。"""
    if not relative_path:
        return None
    abs_path = os.path.normpath(os.path.join(KITARE_DOCS_ROOT, relative_path))
    root = os.path.normpath(KITARE_DOCS_ROOT)
    if not abs_path.startswith(root):
        return None
    return abs_path


# ────────────────────────────────────────────
# ユーザー情報
# ────────────────────────────────────────────

def get_user_category(user_id):
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT category FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return row['category'] if row else None
    except Exception as e:
        logging.error("get_user_category error: %s", e)
        return None
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


def get_user_display_name(user_id):
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT full_name FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            return f"User#{user_id}"
        return row.get('full_name') or f"User#{user_id}"
    except Exception as e:
        logging.error("get_user_display_name error: %s", e)
        return f"User#{user_id}"
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


def get_user_email(user_id):
    """ユーザーのメールアドレス。見つからない／エラー時は None。"""
    if not user_id:
        return None
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT email FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return row.get('email') or None
    except Exception as e:
        logging.error("get_user_email error: %s", e)
        return None
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# 権限判定（北近畿地域連携機構グループ）
# ────────────────────────────────────────────

def is_admin(user_id):
    return get_user_category(user_id) == 'admin'


def is_business(user_id):
    """業務会議メンバー（委員長＋事務局）か。admin も含む。"""
    if is_admin(user_id):
        return True
    return user_is_in_group(user_id, GROUP_BUSINESS)


def is_committee(user_id):
    """運営委員会メンバー（委員。委員長を含む）か。admin も含む。
       委員長は運営委員でもあるため GROUP_CHAIR 所属者も含める
       （委員長が GROUP_COMMITTEE に登録漏れでも審議に参加できるよう）。"""
    if is_admin(user_id):
        return True
    if user_is_in_group(user_id, GROUP_COMMITTEE):
        return True
    if user_is_in_group(user_id, GROUP_CHAIR):
        return True
    return False


def is_member(user_id):
    """固定メンバー（業務会議または運営委員会のいずれか）か。"""
    return is_business(user_id) or is_committee(user_id)


# 注：北近畿地域連携機構は3グループ運用。
#     委員長は GROUP_CHAIR に所属する。委員長は運営委員会・業務会議にも
#     属する（運営委員として審議でき、業務会議で受理判断もできる）。
#     事務局は業務会議に属するが委員長ではない人。
def is_chair(user_id):
    """委員長か。GROUP_CHAIR 所属で判定。"""
    if is_admin(user_id):
        return True
    return user_is_in_group(user_id, GROUP_CHAIR)


def is_secretariat(user_id):
    """事務局か。業務会議に属するが委員長ではない人。"""
    if is_admin(user_id):
        return True
    return (user_is_in_group(user_id, GROUP_BUSINESS) and
            not user_is_in_group(user_id, GROUP_CHAIR))


# ────────────────────────────────────────────
# 案件取得
# ────────────────────────────────────────────

def get_case(case_id):
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM kitare_cases WHERE id = %s", (case_id,))
        return cursor.fetchone()
    except Exception as e:
        logging.error("get_case error: %s", e)
        return None
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


def is_applicant(user_id, case):
    """この案件の申請者本人か"""
    if not case:
        return False
    return case.get('applicant_user_id') == user_id


def get_coi_user_ids(case_id):
    """この案件で COI により担当を外れた委員のIDリスト"""
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT user_id FROM kitare_coi WHERE case_id = %s", (case_id,))
        return [r['user_id'] for r in cursor.fetchall()]
    except Exception as e:
        logging.error("get_coi_user_ids error: %s", e)
        return []
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


def is_coi(user_id, case_id):
    """この案件で COI 扱い（担当から外れている）か"""
    return user_id in get_coi_user_ids(case_id)


def get_assigned_reviewer_ids(case_id):
    """この案件の担当委員のIDリスト（opt-out 方式）。
       運営委員会グループ（委員長グループ含む）の全員から、
       COI で外れた人を引く。"""
    try:
        committee_ids = set(get_group_member_ids(GROUP_COMMITTEE))
        committee_ids |= set(get_group_member_ids(GROUP_CHAIR))
    except Exception as e:
        logging.error("get_assigned_reviewer_ids: グループ取得失敗: %s", e)
        return []
    coi_ids = set(get_coi_user_ids(case_id))
    return list(committee_ids - coi_ids)


def is_assigned_reviewer(user_id, case_id):
    """この案件の担当委員か（運営委員会メンバーで COI でない）"""
    if not is_committee(user_id):
        return False
    return not is_coi(user_id, case_id)


# ────────────────────────────────────────────
# アクセス制御
# ────────────────────────────────────────────

def can_access_review_track(user_id, case_id):
    """審議トラック（運営委員会の審議）を閲覧できるか。
       担当委員（COIでない運営委員）・業務会議・admin。"""
    if is_business(user_id):       # 委員長・事務局・admin
        return True
    return is_assigned_reviewer(user_id, case_id)


def can_write_review_track(user_id, case_id):
    """審議トラックに発言できるか。担当委員（COIでない運営委員）。
       事務局も運営の補助として発言できる。"""
    if is_admin(user_id):
        return True
    if is_secretariat(user_id):
        return True
    return is_assigned_reviewer(user_id, case_id)


def can_view_application_content(user_id, case):
    """申請内容（案件情報・書類セット）を閲覧できるか。
       申請者本人・業務会議・運営委員（担当）・admin。"""
    if not case:
        return False
    if is_applicant(user_id, case):
        return True
    if is_business(user_id):
        return True
    return is_assigned_reviewer(user_id, case['id'])


def can_read_applicant_channel(user_id, case):
    """申請者チャンネルを閲覧できるか。
       申請者本人・業務会議・admin。"""
    if not case:
        return False
    if is_applicant(user_id, case):
        return True
    return is_business(user_id)


def can_write_applicant_channel(user_id, case):
    """申請者チャンネルに書き込めるか。
       申請者本人（終局前）・業務会議・admin。"""
    if not case:
        return False
    if is_applicant(user_id, case):
        return case['status'] not in TERMINAL_STATUSES
    return is_business(user_id)


def can_accept_decide(user_id):
    """受理判断（附議するか）を行えるか。業務会議＝委員長＋事務局。"""
    return is_business(user_id)


def can_decide(user_id):
    """議決（可決／否決／継続審議）を行えるか。委員長のみ。"""
    return is_chair(user_id)


def can_manage_case(user_id, case):
    """打ち切り・COI設定など事務局運営操作を行えるか。業務会議・admin。"""
    return is_business(user_id)

def can_manage_categories(user_id):
    """カテゴリーの定義・更新・削除を行えるか。
       業務会議（委員長＋事務局）＋運営委員長＋admin。
       ※付与（案件への割り当て）は従来どおり業務会議のみ（can_accept_decide）。"""
    return is_business(user_id) or is_chair(user_id)

# ────────────────────────────────────────────
# ステータス遷移
# ────────────────────────────────────────────

def next_seq_no(cursor, fiscal_year):
    """その年度内の次の連番"""
    cursor.execute(
        "SELECT COALESCE(MAX(seq_no), 0) AS m "
        "FROM kitare_cases WHERE fiscal_year = %s", (fiscal_year,))
    row = cursor.fetchone()
    m = row['m'] if isinstance(row, dict) else row[0]
    return (m or 0) + 1


def transition_status(cursor, case_id, new_status, user_id, note='',
                      set_received_date=False, set_final_date=False,
                      set_approved_date=False):
    """ステータス遷移を実行（接続済み cursor を使用）。履歴も記録する。
       cursor は dictionary=True で開いていること。"""
    cursor.execute("SELECT status FROM kitare_cases WHERE id = %s", (case_id,))
    row = cursor.fetchone()
    if not row:
        return False
    from_status = row['status'] if isinstance(row, dict) else row[0]

    updates = ['status = %s']
    params  = [new_status]
    today = get_jst_today()
    if set_received_date:
        updates.append('received_date = %s'); params.append(today)
    if set_final_date:
        updates.append('final_date = %s'); params.append(today)
    if set_approved_date:
        updates.append('approved_date = %s'); params.append(today)
    updates.append('updated_at = %s'); params.append(get_jst_now())
    params.append(case_id)

    cursor.execute(
        f"UPDATE kitare_cases SET {', '.join(updates)} WHERE id = %s", params)
    cursor.execute(
        "INSERT INTO kitare_status_history "
        "(case_id, from_status, to_status, changed_by_user_id, note) "
        "VALUES (%s, %s, %s, %s, %s)",
        (case_id, from_status, new_status, user_id, note or ''))
    return True


def post_applicant_notification(cursor, case_id, user_id, body):
    """申請者チャンネルへ通知メッセージを自動投稿（委員会→申請者への通知）"""
    cursor.execute(
        "INSERT INTO kitare_applicant_messages "
        "(case_id, user_id, role, message_type, body) "
        "VALUES (%s, %s, 'chair', 'notification', %s)",
        (case_id, user_id, body))


def case_url(case_id):
    """案件ページの絶対 URL（Slack 通知用）"""
    try:
        return url_for('kitare_deliberation.case_detail',
                       case_id=case_id, _external=True)
    except Exception:
        return None


def get_chair_emails():
    """委員長グループ全員のメールアドレス一覧（申請通知 DM の宛先）。
       将来は宛先を事務局に変えるなら GROUP_CHAIR を GROUP_BUSINESS 等へ。"""
    try:
        ids = get_group_member_ids(GROUP_CHAIR)
    except Exception as e:
        logging.error("get_chair_emails: グループ取得失敗: %s", e)
        return []
    emails, seen = [], set()
    for uid in ids:
        em = get_user_email(uid)
        if em and em not in seen:
            seen.add(em)
            emails.append(em)
    return emails


def _deny(msg):
    return ('<h2 style="font-family:sans-serif;padding:40px;color:#991b1b">'
            + msg + '</h2><p style="font-family:sans-serif;padding:0 40px">'
            '<a href="/kitare_deliberation/">キターレオンライン審議トップへ戻る'
            '</a></p>'), 403


def _not_found():
    return ('<h2 style="font-family:sans-serif;padding:40px;color:#6b7280">'
            '案件が見つかりません</h2>'
            '<p style="font-family:sans-serif;padding:0 40px">'
            '<a href="/kitare_deliberation/">キターレオンライン審議トップへ戻る'
            '</a></p>'), 404


# ════════════════════════════════════════════
# ページ（render_template）
# ════════════════════════════════════════════

@kitare_deliberation_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJINダッシュボードに戻る（ユーザーカテゴリに応じた遷移）"""
    return redirect_to_dashboard()

@kitare_deliberation_bp.route('/')
@login_required
def index():
    user_id = session.get('user_id')
    return render_template(
        'kitare_deliberation/index.html',
        current_fy=get_current_fiscal_year(),
        status_labels=STATUS_LABELS,
        is_admin=is_admin(user_id),
        is_business=is_business(user_id),
        is_committee=is_committee(user_id),
        is_chair=is_chair(user_id),
        is_secretariat=is_secretariat(user_id),
        can_manage_categories=can_manage_categories(user_id),
    )


@kitare_deliberation_bp.route('/new')
@login_required
def new_case():
    """新規申請：チャンネル立ち上げ＋書類登録＋申請。全ユーザー可。"""
    return render_template(
        'kitare_deliberation/new.html',
        max_documents=MAX_DOCUMENTS,
        max_file_mb=MAX_FILE_SIZE // (1024 * 1024),
    )


@kitare_deliberation_bp.route('/case/<int:case_id>')
@login_required
def case_detail(case_id):
    """案件詳細：審議トラック中心の画面（業務会議・運営委員向け）"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return _not_found()
    if not can_access_review_track(user_id, case_id):
        return _deny('権限がありません')
    return render_template(
        'kitare_deliberation/detail.html',
        case_id=case_id,
        status_labels=STATUS_LABELS,
        decision_labels=DECISION_LABELS,
        terminal_statuses=list(TERMINAL_STATUSES),
        is_admin=is_admin(user_id),
        is_business=is_business(user_id),
        is_chair=is_chair(user_id),
        is_secretariat=is_secretariat(user_id),
        can_accept=can_accept_decide(user_id),
        can_decide=can_decide(user_id),
    )


@kitare_deliberation_bp.route('/case/<int:case_id>/view')
@login_required
def case_view(case_id):
    """申請内容（案件情報・書類セット）の閲覧専用ページ"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return _not_found()
    if not can_view_application_content(user_id, case):
        return _deny('権限がありません')
    return render_template(
        'kitare_deliberation/case_view.html',
        case_id=case_id,
        status_labels=STATUS_LABELS,
    )


@kitare_deliberation_bp.route('/case/<int:case_id>/applicant')
@login_required
def case_applicant_channel(case_id):
    """申請者チャンネル画面"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return _not_found()
    if not can_read_applicant_channel(user_id, case):
        return _deny('権限がありません')
    read_only = not can_write_applicant_channel(user_id, case)
    return render_template(
        'kitare_deliberation/applicant_channel.html',
        case_id=case_id,
        read_only=read_only,
        is_applicant=is_applicant(user_id, case),
        status_labels=STATUS_LABELS,
    )


@kitare_deliberation_bp.route('/public')
@login_required
def public_view():
    """パブリック公開：受付状況（タイトルまで／特定案件はぼかし）。
       一覧はサーバ側で HTML に書き込んで返す（オールなど HTML だけを
       読む利用者にも同じ中身が渡るように）。年度は ?fiscal_year= で選ぶ。"""
    current_fy = get_current_fiscal_year()
    fy = request.args.get('fiscal_year', type=int) or current_fy
    cases, error = _fetch_public_cases(fy)
    return render_template(
        'kitare_deliberation/public.html',
        current_fy=current_fy,
        selected_fy=fy,
        cases=cases,
        error=error,
        status_labels=STATUS_LABELS,
    )


@kitare_deliberation_bp.route('/public/approved')
@login_required
def public_approved():
    """可決案件の公開。一覧はサーバ側で HTML に書き込んで返す。
       年度は ?fiscal_year= で選ぶ（無指定は全年度）。"""
    fy = request.args.get('fiscal_year', type=int)
    cases, error = _fetch_public_approved(fy)
    return render_template(
        'kitare_deliberation/public_approved.html',
        current_fy=get_current_fiscal_year(),
        selected_fy=fy,
        cases=cases,
        error=error,
    )


@kitare_deliberation_bp.route('/report')
@login_required
def report():
    """年度報告（事務局向け）"""
    user_id = session.get('user_id')
    if not is_business(user_id):
        return _deny('権限がありません')
    return render_template(
        'kitare_deliberation/report.html',
        current_fy=get_current_fiscal_year(),
        status_labels=STATUS_LABELS,
    )


@kitare_deliberation_bp.route('/categories')
@login_required
def categories_admin():
    """カテゴリー定義の管理ページ（業務会議・運営委員長・admin 向け）。"""
    user_id = session.get('user_id')
    if not can_manage_categories(user_id):
        return _deny('カテゴリー定義は業務会議・運営委員長のみ可能です')
    ensure_category_schema()
    return render_template('kitare_deliberation/categories.html')


# ════════════════════════════════════════════
# API: 申請者ワークフロー
# ════════════════════════════════════════════

@kitare_deliberation_bp.route('/api/case', methods=['POST'])
@login_required
def api_create_case():
    """申請者：新規チャンネル立ち上げ。status='channel_open' で案件作成。
       特定案件フラグ・公開用タイトルもここで受け取る。"""
    user_id = session.get('user_id')
    data = request.json or {}

    title = (data.get('title') or '').strip()
    applicant_name        = (data.get('applicant_name') or '').strip()
    applicant_affiliation = (data.get('applicant_affiliation') or '').strip()
    cover_letter = (data.get('cover_letter') or '').strip() or None
    is_personnel = 1 if data.get('is_personnel') else 0
    public_title = (data.get('public_title') or '').strip() or None

    if not title:
        return jsonify({'success': False, 'error': '案件名は必須です'}), 400

    ensure_category_schema()  # cover_letter 列を保証
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)

        fiscal_year = get_current_fiscal_year()
        seq_no      = next_seq_no(cursor, fiscal_year)
        display_id  = format_case_id(fiscal_year, seq_no)
        if not applicant_name:
            applicant_name = get_user_display_name(user_id)
        now = get_jst_now()

        cursor.execute(
            "INSERT INTO kitare_cases "
            "(fiscal_year, seq_no, case_display_id, title, "
            " is_personnel, public_title, "
            " applicant_user_id, applicant_name, applicant_affiliation, "
            " cover_letter, "
            " status, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s, %s,%s, %s,%s,%s, %s, "
            " 'channel_open', %s,%s)",
            (fiscal_year, seq_no, display_id, title,
             is_personnel, public_title,
             user_id, applicant_name, applicant_affiliation,
             cover_letter,
             now, now))
        case_id = cursor.lastrowid

        cursor.execute(
            "INSERT INTO kitare_status_history "
            "(case_id, from_status, to_status, changed_by_user_id, note) "
            "VALUES (%s, NULL, 'channel_open', %s, 'チャンネル立ち上げ')",
            (case_id, user_id))
        conn.commit()
        logging.info("kitare case created: %s by user %s", display_id, user_id)
        return jsonify({'success': True, 'case_id': case_id,
                        'case_display_id': display_id})
    except Exception as e:
        logging.error("api_create_case error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/case/<int:case_id>/cover_letter',
                              methods=['POST'])
@login_required
def api_set_cover_letter(case_id):
    """申請者：添書（送付メッセージ）の保存。申請前のみ編集可。
       body: {cover_letter: <str>}。空文字で添書なしに戻せる。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not is_applicant(user_id, case):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    if case['status'] not in ('channel_open', 'documents_uploading'):
        return jsonify({'success': False,
                        'error': '申請前のみ添書を編集できます'}), 409

    ensure_category_schema()  # cover_letter 列を保証
    data = request.json or {}
    cover_letter = (data.get('cover_letter') or '').strip() or None

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "UPDATE kitare_cases SET cover_letter = %s, updated_at = %s "
            "WHERE id = %s", (cover_letter, get_jst_now(), case_id))
        conn.commit()
        return jsonify({'success': True, 'cover_letter': cover_letter or ''})
    except Exception as e:
        logging.error("api_set_cover_letter error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/case/<int:case_id>/document',
                              methods=['POST'])
@login_required
def api_add_document(case_id):
    """申請者：書類ファイルをアップロード。multipart/form-data。
       フィールド: file（必須）, title（必須）, note（任意）。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not is_applicant(user_id, case):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    if case['status'] not in ('channel_open', 'documents_uploading'):
        return jsonify({'success': False,
                        'error': f"このステータス（{STATUS_LABELS.get(case['status'])}）"
                                 "では書類追加できません"}), 400

    title = (request.form.get('title') or '').strip()
    note  = (request.form.get('note')  or '').strip()
    up_file = request.files.get('file')
    if not title:
        return jsonify({'success': False, 'error': '書類タイトルは必須です'}), 400
    if not up_file or not (up_file.filename or '').strip():
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400

    original_filename = up_file.filename
    up_file.stream.seek(0, os.SEEK_END)
    file_size = up_file.stream.tell()
    up_file.stream.seek(0)
    if file_size <= 0:
        return jsonify({'success': False, 'error': 'ファイルが空です'}), 400
    if file_size > MAX_FILE_SIZE:
        return jsonify({'success': False,
                        'error': f'ファイルサイズは {MAX_FILE_SIZE // (1024*1024)}MB '
                                 '以下にしてください'}), 400

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM kitare_documents WHERE case_id = %s",
            (case_id,))
        if cursor.fetchone()['cnt'] >= MAX_DOCUMENTS:
            return jsonify({'success': False,
                            'error': f'書類は1案件あたり最大{MAX_DOCUMENTS}件まで'
                                     'です'}), 400

        # ファイル保存
        try:
            save_dir = _ensure_case_doc_dir(case_id)
        except OSError as oe:
            logging.error("kitare: save dir 作成失敗 %s: %s",
                          KITARE_DOCS_ROOT, oe)
            return jsonify({'success': False,
                            'error': '保存ディレクトリ作成に失敗しました'}), 500
        safe_name = _safe_filename(original_filename)
        abs_path  = os.path.join(save_dir, safe_name)
        while os.path.exists(abs_path):
            safe_name = _safe_filename(original_filename)
            abs_path  = os.path.join(save_dir, safe_name)
        try:
            up_file.save(abs_path)
        except OSError as oe:
            logging.error("kitare: ファイル保存失敗 %s: %s", abs_path, oe)
            return jsonify({'success': False,
                            'error': f'ファイル保存に失敗しました: {oe}'}), 500

        rel_path = os.path.join(f'case_{case_id}', safe_name)
        now = get_jst_now()
        cursor.execute(
            "INSERT INTO kitare_documents "
            "(case_id, title, note, file_path, original_filename, "
            " file_size, uploaded_by_user_id, uploaded_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (case_id, title, note, rel_path, original_filename,
             file_size, user_id, now))
        doc_id = cursor.lastrowid

        # 最初の書類追加で channel_open → documents_uploading
        if case['status'] == 'channel_open':
            transition_status(cursor, case_id, 'documents_uploading',
                              user_id, note='書類アップロード開始')
        conn.commit()
        return jsonify({'success': True, 'doc_id': doc_id})
    except Exception as e:
        logging.error("api_add_document error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/case/<int:case_id>/document/<int:doc_id>',
                              methods=['DELETE'])
@login_required
def api_delete_document(case_id, doc_id):
    """書類の削除（申請者のみ、申請前のみ）。実ファイルも削除。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not is_applicant(user_id, case):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    if case['status'] not in ('channel_open', 'documents_uploading'):
        return jsonify({'success': False,
                        'error': '申請前のみ書類を削除できます'}), 400

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT file_path FROM kitare_documents "
            "WHERE id = %s AND case_id = %s", (doc_id, case_id))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '書類が見つかりません'}), 404

        cursor.execute(
            "DELETE FROM kitare_documents WHERE id = %s AND case_id = %s",
            (doc_id, case_id))
        conn.commit()

        # 実ファイル削除（DB削除成功後。失敗しても致命的でない）
        abs_path = _doc_absolute_path(row.get('file_path'))
        if abs_path and os.path.exists(abs_path):
            try:
                os.remove(abs_path)
            except OSError as oe:
                logging.warning("api_delete_document: ファイル削除失敗 %s: %s",
                                abs_path, oe)
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_delete_document error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route(
    '/api/case/<int:case_id>/document/<int:doc_id>/download', methods=['GET'])
@login_required
def api_download_document(case_id, doc_id):
    """書類ファイルのダウンロード。閲覧権限のある人のみ。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return abort(404)
    if not can_view_application_content(user_id, case):
        return abort(403)

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT file_path, original_filename FROM kitare_documents "
            "WHERE id = %s AND case_id = %s", (doc_id, case_id))
        row = cursor.fetchone()
    except Exception as e:
        logging.error("api_download_document error: %s", e)
        return abort(500)
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()

    if not row or not row.get('file_path'):
        return abort(404)
    abs_path = _doc_absolute_path(row['file_path'])
    if not abs_path or not os.path.exists(abs_path):
        return abort(404)
    return send_file(abs_path, as_attachment=True,
                     download_name=row.get('original_filename') or 'document')


@kitare_deliberation_bp.route('/api/case/<int:case_id>/submit',
                              methods=['POST'])
@login_required
def api_submit(case_id):
    """申請者：申請ボタン押下。documents_uploading → submitted。
       業務会議の受理判断待ちに入る。commit 後に Slack 通知＋委員長DM。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not is_applicant(user_id, case):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    # 書類はあってもなくても申請可。書類未追加なら channel_open のまま、
    # 1件以上あれば documents_uploading。いずれの状態からも申請できる。
    if case['status'] not in ('channel_open', 'documents_uploading'):
        return jsonify({'success': False,
                        'error': '申請前の状態でのみ申請できます'}), 400

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        transition_status(cursor, case_id, 'submitted', user_id,
                          note='申請受付', set_received_date=True)
        conn.commit()
    except Exception as e:
        logging.error("api_submit error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()

    # --- Slack 通知（commit 後）：チャンネル一括＋委員長へ個別DM -------
    try:
        from .slack_notifier import (notify_application_submitted,
                                     notify_application_to_recipients)
        notify_application_submitted(
            case_display_id=case.get('case_display_id'),
            title=case.get('title'),
            applicant_name=case.get('applicant_name'),
            case_url=case_url(case_id))
        chair_emails = get_chair_emails()
        if chair_emails:
            notify_application_to_recipients(
                recipient_emails=chair_emails,
                case_display_id=case.get('case_display_id'),
                title=case.get('title'),
                applicant_name=case.get('applicant_name'),
                case_url=case_url(case_id))
        else:
            logging.info("api_submit: 委員長DMの宛先が0件のためDMスキップ")
    except Exception as slack_err:
        logging.warning("api_submit: Slack通知でエラー（申請は成功）: %s",
                        slack_err)
    # --------------------------------------------------------------
    return jsonify({'success': True})


@kitare_deliberation_bp.route('/api/case/<int:case_id>/withdraw',
                              methods=['POST'])
@login_required
def api_withdraw(case_id):
    """申請者：取り下げ。終局前のものに対してのみ。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not is_applicant(user_id, case):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    if case['status'] in TERMINAL_STATUSES:
        return jsonify({'success': False,
                        'error': 'この案件はすでに終了しています'}), 409

    data   = request.json or {}
    reason = (data.get('reason') or '').strip()

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        transition_status(cursor, case_id, 'withdrawn', user_id,
                          note='取り下げ' + (f'：{reason}' if reason else ''),
                          set_final_date=True)
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_withdraw error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


# ════════════════════════════════════════════
# API: 審議ワークフロー（業務会議・運営委員会）
# ════════════════════════════════════════════

@kitare_deliberation_bp.route('/api/case/<int:case_id>/accept',
                              methods=['POST'])
@login_required
def api_accept_decide(case_id):
    """業務会議：受理判断。submitted の案件を「附議する／しない」で振り分け。
       action='accept'  → accepted_review（運営委員会の審議へ）
       action='reject'  → not_accepted（不受理・終局）
       附議する場合 deadline（審議期日）を任意で設定できる。
       附議＝審議開始なので、accept のとき審議開始 Slack 通知も出す。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not can_accept_decide(user_id):
        return jsonify({'success': False,
                        'error': '受理判断の権限がありません（業務会議のみ）'}), 403
    if case['status'] != 'submitted':
        return jsonify({'success': False,
                        'error': '申請受付中の案件のみ受理判断できます'}), 409

    data   = request.json or {}
    action = data.get('action')
    note   = (data.get('note') or '').strip()
    deadline = data.get('deadline') or None
    if action not in ('accept', 'reject'):
        return jsonify({'success': False, 'error': '不正なアクションです'}), 400

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        now = get_jst_now()
        cursor.execute(
            "UPDATE kitare_cases SET "
            "  accepted_decided_by_user_id = %s, accepted_decided_at = %s, "
            "  accept_note = %s WHERE id = %s",
            (user_id, now, note, case_id))

        if action == 'accept':
            # 附議する：審議へ。期日を設定するなら同時に。
            if deadline:
                cursor.execute(
                    "UPDATE kitare_cases SET deadline = %s, "
                    "deadline_notified_for = NULL WHERE id = %s",
                    (deadline, case_id))
            transition_status(cursor, case_id, 'accepted_review', user_id,
                              note='附議決定（受理）')
            post_applicant_notification(
                cursor, case_id, user_id,
                '【業務会議通知】申請を受理し、運営委員会の審議に附議しました。'
                + (f'\n\n{note}' if note else ''))
        else:
            # 附議しない：不受理で終局
            transition_status(cursor, case_id, 'not_accepted', user_id,
                              note='不受理', set_final_date=True)
            post_applicant_notification(
                cursor, case_id, user_id,
                '【業務会議通知】審議の結果、不受理となりました。'
                + (f'\n\n{note}' if note else ''))
        conn.commit()
    except Exception as e:
        logging.error("api_accept_decide error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()

    # --- Slack 通知（commit 後。受理＝審議開始のとき）-------------------
    if action == 'accept':
        try:
            from .slack_notifier import notify_review_started
            # deadline はリクエスト由来の文字列。fmt_date は文字列も
            # 素通しするのでそのまま渡してよい。
            notify_review_started(
                case_display_id=case.get('case_display_id'),
                title=case.get('title'),
                deadline=fmt_date(deadline) if deadline else None,
                case_url=case_url(case_id))
        except Exception as slack_err:
            logging.warning("api_accept_decide: Slack通知でエラー"
                            "（受理は成功）: %s", slack_err)
    # --------------------------------------------------------------
    return jsonify({'success': True})


@kitare_deliberation_bp.route('/api/case/<int:case_id>/set_deadline',
                              methods=['POST'])
@login_required
def api_set_deadline(case_id):
    """業務会議：審議中案件の審議期日を設定／変更する。
       受理時に未設定だった期日を後から入れる、または変更する用途。
       Slack 通知はしない（締切バッチが期日当日に拾う）。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not can_accept_decide(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    if case['status'] != 'accepted_review':
        return jsonify({'success': False,
                        'error': '審議中の案件のみ期日を設定できます'}), 409

    data = request.json or {}
    deadline = data.get('deadline') or None
    if not deadline:
        return jsonify({'success': False, 'error': '審議期日を指定してください'}), 400

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        old_dl = fmt_date(case.get('deadline')) or '未設定'
        cursor.execute(
            "UPDATE kitare_cases SET deadline = %s, "
            "deadline_notified_for = NULL, updated_at = %s WHERE id = %s",
            (deadline, get_jst_now(), case_id))
        cursor.execute(
            "INSERT INTO kitare_status_history "
            "(case_id, from_status, to_status, changed_by_user_id, note) "
            "VALUES (%s, 'accepted_review', 'accepted_review', %s, %s)",
            (case_id, user_id, f'審議期日を設定（{old_dl} → {deadline}）'))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_set_deadline error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/case/<int:case_id>/coi', methods=['POST'])
@login_required
def api_set_coi(case_id):
    """業務会議：COIと判明した委員を、その案件の担当から外す／戻す。
       action='set'    → kitare_coi に登録（担当から外す）
       action='unset'  → kitare_coi から削除（担当に戻す）
       委員長にも適用できる。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not can_manage_case(user_id, case):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    data    = request.json or {}
    target  = data.get('user_id')
    action  = data.get('action')
    reason  = (data.get('reason') or '').strip()
    if not target or action not in ('set', 'unset'):
        return jsonify({'success': False, 'error': '不正なパラメータです'}), 400

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        if action == 'set':
            cursor.execute(
                "INSERT INTO kitare_coi "
                "(case_id, user_id, reason, set_by_user_id) "
                "VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE reason=VALUES(reason), "
                "set_by_user_id=VALUES(set_by_user_id), "
                "set_at=CURRENT_TIMESTAMP",
                (case_id, target, reason, user_id))
        else:
            cursor.execute(
                "DELETE FROM kitare_coi WHERE case_id = %s AND user_id = %s",
                (case_id, target))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_set_coi error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/case/<int:case_id>/decide',
                              methods=['POST'])
@login_required
def api_decide(case_id):
    """委員長：議決。accepted_review の案件に対して。
       action='approve'  → approved（可決・終局）
       action='reject'   → rejected（否決・終局）
       action='continue' → 継続審議。新しい審議期日を設定し accepted_review
                           のまま審議を続行（終局しない）。
       commit 後に Slack 通知。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not can_decide(user_id):
        return jsonify({'success': False,
                        'error': '議決の権限がありません（委員長のみ）'}), 403
    if case['status'] != 'accepted_review':
        return jsonify({'success': False,
                        'error': '審議中の案件のみ議決できます'}), 409

    data   = request.json or {}
    action = data.get('action')
    # note            : 委員会宛てコメント（decision_note に保存。審議記録に残る。
    #                   申請者には表示しない）
    # applicant_message: 申請者宛てメッセージ（申請者チャンネルに通知として届く。
    #                   定型の「【委員会通知】可決／否決」に続けて表示する）
    note   = (data.get('note') or '').strip()
    applicant_message = (data.get('applicant_message') or '').strip()
    new_deadline = data.get('deadline') or None
    if action not in ('approve', 'reject', 'continue'):
        return jsonify({'success': False, 'error': '不正なアクションです'}), 400
    if action == 'continue' and not new_deadline:
        return jsonify({'success': False,
                        'error': '継続審議には新しい審議期日が必要です'}), 400
    if len(note) > MAX_BODY_LEN or len(applicant_message) > MAX_BODY_LEN:
        return jsonify({'success': False, 'error': 'コメントが長すぎます'}), 400

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        now = get_jst_now()

        if action in ('approve', 'reject'):
            new_status = 'approved' if action == 'approve' else 'rejected'
            cursor.execute(
                "UPDATE kitare_cases SET decision_note = %s, "
                "decision_by_user_id = %s, decision_at = %s WHERE id = %s",
                (note, user_id, now, case_id))
            transition_status(cursor, case_id, new_status, user_id,
                              note='委員長議決：' + DECISION_LABELS[action],
                              set_final_date=True,
                              set_approved_date=(action == 'approve'))
            post_applicant_notification(
                cursor, case_id, user_id,
                f'【委員会通知】{DECISION_LABELS[action]}'
                + (f'\n\n{applicant_message}' if applicant_message else ''))
        else:
            # 継続審議：終局しない。新しい期日を設定し審議続行。
            cursor.execute(
                "UPDATE kitare_cases SET deadline = %s, "
                "deadline_notified_for = NULL, "
                "extension_count = extension_count + 1, "
                "updated_at = %s WHERE id = %s",
                (new_deadline, now, case_id))
            cursor.execute(
                "INSERT INTO kitare_status_history "
                "(case_id, from_status, to_status, changed_by_user_id, note) "
                "VALUES (%s, 'accepted_review', 'accepted_review', %s, %s)",
                (case_id, user_id,
                 f'委員長議決：継続審議（新期日 {new_deadline}）'))
            # 審議トラックにも継続審議を記録
            cursor.execute(
                "INSERT INTO kitare_review_messages "
                "(case_id, user_id, body, message_kind) "
                "VALUES (%s, %s, %s, 'alert')",
                (case_id, user_id,
                 f'【継続審議】委員長判断により審議を継続します。'
                 f'新しい審議期日：{new_deadline}。'
                 + (f'\n{note}' if note else '')))
            # 申請者宛てメッセージが入力されていれば申請者チャンネルへ通知
            if applicant_message:
                post_applicant_notification(
                    cursor, case_id, user_id,
                    f'【委員会通知】継続審議\n\n{applicant_message}')
        conn.commit()
    except Exception as e:
        logging.error("api_decide error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()

    # --- Slack 通知（commit 後）-----------------------------------------
    try:
        from .slack_notifier import notify_decision
        notify_decision(
            case_display_id=case.get('case_display_id'),
            title=case.get('title'),
            decision_label=DECISION_LABELS[action],
            case_url=case_url(case_id))
    except Exception as slack_err:
        logging.warning("api_decide: Slack通知でエラー（議決は成功）: %s",
                        slack_err)
    # --------------------------------------------------------------
    return jsonify({'success': True})


@kitare_deliberation_bp.route('/api/case/<int:case_id>/terminate',
                              methods=['POST'])
@login_required
def api_terminate(case_id):
    """業務会議：長期放置案件を打ち切り。終局前のものに対してのみ。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not can_manage_case(user_id, case):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    if case['status'] in TERMINAL_STATUSES:
        return jsonify({'success': False,
                        'error': 'この案件はすでに終了しています'}), 409

    data   = request.json or {}
    reason = (data.get('reason') or '').strip()

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        transition_status(cursor, case_id, 'terminated', user_id,
                          note='打ち切り' + (f'：{reason}' if reason else ''),
                          set_final_date=True)
        post_applicant_notification(
            cursor, case_id, user_id,
            '【業務会議通知】この案件は打ち切りとなりました。'
            + (f'\n\n{reason}' if reason else ''))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_terminate error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


# ════════════════════════════════════════════
# API: 審議トラックメッセージ
# ════════════════════════════════════════════

@kitare_deliberation_bp.route('/api/case/<int:case_id>/review_messages',
                              methods=['GET'])
@login_required
def api_review_messages(case_id):
    """審議トラックメッセージ取得"""
    user_id = session.get('user_id')
    if not can_access_review_track(user_id, case_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT m.id, m.user_id, m.body, m.message_kind, m.created_at, "
            "       u.full_name AS user_name "
            "FROM kitare_review_messages m "
            "LEFT JOIN users u ON u.id = m.user_id "
            "WHERE m.case_id = %s ORDER BY m.created_at ASC, m.id ASC",
            (case_id,))
        msgs = []
        for r in cursor.fetchall():
            msgs.append({
                'id':           r['id'],
                'user_id':      r['user_id'],
                'user_name':    r['user_name'] or f"User#{r['user_id']}",
                'body':         r['body'],
                'message_kind': r.get('message_kind') or 'normal',
                'role':         _role_of(r['user_id']),
                'created_at':   fmt_datetime(r['created_at']),
                'source':       'review',
            })

        # 申請者チャンネルへ送られた「申請者宛て」通知も、審議の記録として
        # 併合表示する（正本は kitare_applicant_messages 側。ここは参照のみ）。
        # 委員会・業務会議は「申請者に何を伝えたか」をこの場で確認できる。
        # 申請者発の質問・回答などは審議トラックには出さず、委員側からの
        # 通知（role が申請者以外）だけを対象とする。
        cursor.execute(
            "SELECT m.id, m.user_id, m.body, m.message_type, m.created_at, "
            "       u.full_name AS user_name "
            "FROM kitare_applicant_messages m "
            "LEFT JOIN users u ON u.id = m.user_id "
            "WHERE m.case_id = %s AND m.role <> 'applicant' "
            "ORDER BY m.created_at ASC, m.id ASC",
            (case_id,))
        for r in cursor.fetchall():
            msgs.append({
                'id':           r['id'],
                'user_id':      r['user_id'],
                'user_name':    r['user_name'] or f"User#{r['user_id']}",
                'body':         r['body'],
                'message_kind': 'normal',
                'role':         _role_of(r['user_id']),
                'created_at':   fmt_datetime(r['created_at']),
                'source':       'applicant',   # 「申請者へ通知」印
            })

        # 時系列に整列（review と applicant を混ぜたので created_at で並べ直す）
        msgs.sort(key=lambda m: (m['created_at'], m['source'], m['id']))
        return jsonify({'success': True, 'messages': msgs})
    except Exception as e:
        logging.error("api_review_messages error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/case/<int:case_id>/review_messages',
                              methods=['POST'])
@login_required
def api_post_review_message(case_id):
    """審議トラックメッセージ投稿。審議中（accepted_review）の案件のみ。
       message_kind='alert' のとき Slack へ重要発言通知。"""
    user_id = session.get('user_id')
    if not can_write_review_track(user_id, case_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if case['status'] != 'accepted_review':
        return jsonify({'success': False,
                        'error': '審議中の案件にのみ発言できます'}), 409

    data = request.json or {}
    body = (data.get('body') or '').strip()
    if not body:
        return jsonify({'success': False, 'error': '本文は必須です'}), 400
    if len(body) > MAX_BODY_LEN:
        return jsonify({'success': False, 'error': '本文が長すぎます'}), 400
    message_kind = data.get('message_kind')
    if message_kind != 'alert':
        message_kind = 'normal'

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO kitare_review_messages "
            "(case_id, user_id, body, message_kind, created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (case_id, user_id, body, message_kind, get_jst_now()))
        conn.commit()
        new_id = cursor.lastrowid
    except Exception as e:
        logging.error("api_post_review_message error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()

    if message_kind == 'alert':
        try:
            from .slack_notifier import notify_alert_message
            notify_alert_message(
                case_display_id=case.get('case_display_id'),
                title=case.get('title'),
                case_url=case_url(case_id))
        except Exception as slack_err:
            logging.warning("api_post_review_message: Slack通知でエラー"
                            "（投稿は成功）: %s", slack_err)
    return jsonify({'success': True, 'message_id': new_id})


# ════════════════════════════════════════════
# API: 申請者チャンネルメッセージ
# ════════════════════════════════════════════

@kitare_deliberation_bp.route('/api/case/<int:case_id>/applicant_messages',
                              methods=['GET'])
@login_required
def api_applicant_messages(case_id):
    """申請者チャンネルメッセージ取得"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not can_read_applicant_channel(user_id, case):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT m.id, m.user_id, m.role, m.message_type, m.body, "
            "       m.created_at, u.full_name AS user_name "
            "FROM kitare_applicant_messages m "
            "LEFT JOIN users u ON u.id = m.user_id "
            "WHERE m.case_id = %s ORDER BY m.created_at ASC, m.id ASC",
            (case_id,))
        msgs = []
        for r in cursor.fetchall():
            msgs.append({
                'id':           r['id'],
                'user_id':      r['user_id'],
                'user_name':    r['user_name'] or f"User#{r['user_id']}",
                'role':         r.get('role') or 'applicant',
                'message_type': r.get('message_type') or 'general',
                'body':         r['body'],
                'created_at':   fmt_datetime(r['created_at']),
            })
        return jsonify({'success': True, 'messages': msgs})
    except Exception as e:
        logging.error("api_applicant_messages error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/case/<int:case_id>/applicant_messages',
                              methods=['POST'])
@login_required
def api_post_applicant_message(case_id):
    """申請者チャンネルメッセージ投稿"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not can_write_applicant_channel(user_id, case):
        return jsonify({'success': False, 'error': '書き込み権限がありません'}), 403

    data = request.json or {}
    body = (data.get('body') or '').strip()
    mtype = data.get('message_type') or 'general'
    if not body:
        return jsonify({'success': False, 'error': '本文は必須です'}), 400
    if len(body) > MAX_BODY_LEN:
        return jsonify({'success': False, 'error': '本文が長すぎます'}), 400
    if mtype not in ('general', 'question', 'answer', 'notification'):
        mtype = 'general'

    role = ('applicant' if is_applicant(user_id, case)
            else ('chair' if is_chair(user_id)
                  else ('secretariat' if is_secretariat(user_id)
                        else ('admin' if is_admin(user_id) else 'committee'))))

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO kitare_applicant_messages "
            "(case_id, user_id, role, message_type, body, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (case_id, user_id, role, mtype, body, get_jst_now()))
        conn.commit()
        return jsonify({'success': True, 'message_id': cursor.lastrowid})
    except Exception as e:
        logging.error("api_post_applicant_message error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# シリアライズ補助
# ────────────────────────────────────────────

def _role_of(user_id):
    """ユーザーのロール表示用（chair/secretariat/committee/admin）"""
    if is_admin(user_id):
        return 'admin'
    if is_chair(user_id):
        return 'chair'
    if is_secretariat(user_id):
        return 'secretariat'
    if is_committee(user_id):
        return 'committee'
    return ''


def _serialize_case(case, include_category=False):
    """kitare_cases の1行を JSON 向け dict に変換。
       include_category=True のときだけ category_id / category_name を含める。
       カテゴリーは運営委員会・業務会議にのみ見せ、申請者・パブリックには
       見せないため、申請者向け API では include_category=False のままにする。"""
    status = case['status']
    data = {
        'id':                case['id'],
        'case_display_id':   case['case_display_id'],
        'fiscal_year':       case['fiscal_year'],
        'title':             case['title'],
        'is_personnel':      bool(case.get('is_personnel')),
        'public_title':      case.get('public_title') or '',
        'applicant_name':    case.get('applicant_name') or '',
        'applicant_affiliation': case.get('applicant_affiliation') or '',
        'cover_letter':      case.get('cover_letter') or '',
        'status':            status,
        'status_label':      STATUS_LABELS.get(status, status),
        'deadline':          fmt_date(case.get('deadline')),
        'extension_count':   case.get('extension_count') or 0,
        'accept_note':       case.get('accept_note') or '',
        'decision_note':     case.get('decision_note') or '',
        'decision_at':       fmt_datetime(case.get('decision_at')),
        'received_date':     fmt_date(case.get('received_date')),
        'final_date':        fmt_date(case.get('final_date')),
        'approved_date':     fmt_date(case.get('approved_date')),
        'created_at':        fmt_datetime(case.get('created_at')),
        'updated_at':        fmt_datetime(case.get('updated_at')),
        'is_terminal':       status in TERMINAL_STATUSES,
    }
    if include_category:
        cid = case.get('category_id')
        data['category_id']   = cid
        # 行に JOIN 済みの category_name があればそれを使い、無ければ引く
        data['category_name'] = (case.get('category_name')
                                 or (get_category_name(cid) if cid else ''))
    return data


def _public_title_of(case):
    """パブリック公開で見せるタイトル。特定案件はぼかしタイトルを使う。"""
    if case.get('is_personnel'):
        return case.get('public_title') or '特定案件'
    return case.get('title') or '（無題）'


def _get_documents(case_id):
    """案件の書類一覧（dictのリスト）"""
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, title, note, file_path, original_filename, "
            "       file_size, uploaded_at "
            "FROM kitare_documents WHERE case_id = %s "
            "ORDER BY id ASC", (case_id,))
        docs = []
        for r in cursor.fetchall():
            download_url = None
            if r.get('file_path'):
                download_url = url_for(
                    'kitare_deliberation.api_download_document',
                    case_id=case_id, doc_id=r['id'])
            docs.append({
                'id':                r['id'],
                'title':             r['title'],
                'note':              r.get('note') or '',
                'original_filename': r.get('original_filename') or '',
                'file_size':         r.get('file_size'),
                'download_url':      download_url,
                'uploaded_at':       fmt_datetime(r['uploaded_at']),
            })
        return docs
    except Exception as e:
        logging.error("_get_documents error: %s", e)
        return []
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


# ════════════════════════════════════════════
# API: カテゴリー（定義は admin、付与は業務会議）
#   ・定義（追加・改名・並べ替え・有効/無効）: 事務局＝admin のみ
#   ・付与（案件への割り当て）: 業務会議（これまでの審議開始権者）
#   ・付与カテゴリーは運営委員会・業務会議の画面にのみ表示。
#     申請者・パブリックには返さない。
# ════════════════════════════════════════════

def _serialize_category(row):
    return {
        'id':         row['id'],
        'name':       row['name'],
        'sort_order': row.get('sort_order') or 0,
        'is_active':  bool(row.get('is_active')),
    }


@kitare_deliberation_bp.route('/api/categories', methods=['GET'])
@login_required
def api_list_categories():
    """カテゴリー一覧。業務会議・運営委員会・admin が利用できる。
       クエリ active_only=1 で有効なものだけ（付与の選択肢用）。
       申請者には返さない（カテゴリーは申請者非公開のため）。"""
    user_id = session.get('user_id')
    # カテゴリーを見られるのは固定メンバーのみ
    if not (is_business(user_id) or is_committee(user_id)):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    ensure_category_schema()

    active_only = request.args.get('active_only') in ('1', 'true', 'yes')
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        if active_only:
            cursor.execute(
                "SELECT id, name, sort_order, is_active FROM kitare_categories "
                "WHERE is_active = 1 ORDER BY sort_order ASC, name ASC")
        else:
            cursor.execute(
                "SELECT id, name, sort_order, is_active FROM kitare_categories "
                "ORDER BY sort_order ASC, name ASC")
        cats = [_serialize_category(r) for r in cursor.fetchall()]
        return jsonify({'success': True, 'categories': cats})
    except Exception as e:
        logging.error("api_list_categories error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/categories', methods=['POST'])
@login_required
def api_create_category():
    """カテゴリーを新規定義。admin（事務局）のみ。"""
    user_id = session.get('user_id')
    if not can_manage_categories(user_id):
        return jsonify({'success': False,
                        'error': 'カテゴリー定義は業務会議・運営委員長のみ可能です'}), 403
    ensure_category_schema()

    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'カテゴリー名は必須です'}), 400
    if len(name) > 100:
        return jsonify({'success': False,
                        'error': 'カテゴリー名は100文字以内にしてください'}), 400

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        # 想定上限（10個程度）。運用上の歯止めとして 50 で制限。
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM kitare_categories")
        if cursor.fetchone()['cnt'] >= 50:
            return jsonify({'success': False,
                            'error': 'カテゴリー数の上限に達しています'}), 400
        # 末尾に並べる
        cursor.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM kitare_categories")
        next_order = (cursor.fetchone()['m'] or 0) + 1
        now = get_jst_now()
        cursor.execute(
            "INSERT INTO kitare_categories "
            "(name, sort_order, is_active, created_by_user_id, "
            " created_at, updated_at) "
            "VALUES (%s, %s, 1, %s, %s, %s)",
            (name, next_order, user_id, now, now))
        cat_id = cursor.lastrowid
        conn.commit()
        return jsonify({'success': True, 'category_id': cat_id})
    except mysql.connector.IntegrityError:
        return jsonify({'success': False,
                        'error': '同じ名前のカテゴリーが既にあります'}), 409
    except Exception as e:
        logging.error("api_create_category error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/categories/<int:cat_id>', methods=['PUT'])
@login_required
def api_update_category(cat_id):
    """カテゴリーの改名・有効/無効・並び順変更。admin（事務局）のみ。
       受け取りうるフィールド: name, is_active, sort_order（いずれも任意）。"""
    user_id = session.get('user_id')
    if not can_manage_categories(user_id):
        return jsonify({'success': False,
                        'error': 'カテゴリー定義は業務会議・運営委員長のみ可能です'}), 403
    ensure_category_schema()

    data = request.json or {}
    sets, params = [], []
    if 'name' in data:
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'success': False, 'error': 'カテゴリー名は必須です'}), 400
        if len(name) > 100:
            return jsonify({'success': False,
                            'error': 'カテゴリー名は100文字以内にしてください'}), 400
        sets.append('name = %s'); params.append(name)
    if 'is_active' in data:
        sets.append('is_active = %s')
        params.append(1 if data.get('is_active') else 0)
    if 'sort_order' in data:
        try:
            so = int(data.get('sort_order'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': '並び順が不正です'}), 400
        sets.append('sort_order = %s'); params.append(so)
    if not sets:
        return jsonify({'success': False, 'error': '変更内容がありません'}), 400

    sets.append('updated_at = %s'); params.append(get_jst_now())
    params.append(cat_id)

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"UPDATE kitare_categories SET {', '.join(sets)} WHERE id = %s",
            params)
        if cursor.rowcount == 0:
            cursor.execute(
                "SELECT id FROM kitare_categories WHERE id = %s", (cat_id,))
            if not cursor.fetchone():
                return jsonify({'success': False,
                                'error': 'カテゴリーが見つかりません'}), 404
        conn.commit()
        return jsonify({'success': True})
    except mysql.connector.IntegrityError:
        return jsonify({'success': False,
                        'error': '同じ名前のカテゴリーが既にあります'}), 409
    except Exception as e:
        logging.error("api_update_category error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/categories/<int:cat_id>',
                              methods=['DELETE'])
@login_required
def api_delete_category(cat_id):
    """カテゴリーの物理削除。admin（事務局）のみ。
       どの案件にも付与されていない場合のみ削除を許可する。
       使用中の場合は『無効化（is_active=0）』を案内する。"""
    user_id = session.get('user_id')
    if not can_manage_categories(user_id):
        return jsonify({'success': False,
                        'error': 'カテゴリー定義は業務会議・運営委員長のみ可能です'}), 403
    ensure_category_schema()

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM kitare_cases WHERE category_id = %s",
            (cat_id,))
        in_use = cursor.fetchone()['cnt']
        if in_use:
            return jsonify({
                'success': False,
                'error': f'このカテゴリーは{in_use}件の案件で使用中のため削除でき'
                         'ません。代わりに無効化してください。'}), 409
        cursor.execute(
            "DELETE FROM kitare_categories WHERE id = %s", (cat_id,))
        if cursor.rowcount == 0:
            return jsonify({'success': False,
                            'error': 'カテゴリーが見つかりません'}), 404
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_delete_category error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/case/<int:case_id>/category',
                              methods=['POST'])
@login_required
def api_set_case_category(case_id):
    """案件にカテゴリーを付与／変更／解除。業務会議（審議開始権者）のみ。
       body: {category_id: <int> または null}
       null・0・未指定で「カテゴリーなし」に戻す。
       無効（is_active=0）なカテゴリーへの新規付与は不可。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not can_accept_decide(user_id):   # = is_business
        return jsonify({'success': False,
                        'error': 'カテゴリー付与は業務会議のみ可能です'}), 403
    ensure_category_schema()

    data = request.json or {}
    raw = data.get('category_id')
    category_id = None
    if raw not in (None, '', 0, '0'):
        try:
            category_id = int(raw)
        except (TypeError, ValueError):
            return jsonify({'success': False,
                            'error': 'カテゴリー指定が不正です'}), 400

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        if category_id is not None:
            cursor.execute(
                "SELECT id, is_active FROM kitare_categories WHERE id = %s",
                (category_id,))
            cat = cursor.fetchone()
            if not cat:
                return jsonify({'success': False,
                                'error': 'カテゴリーが見つかりません'}), 404
            if not cat['is_active']:
                return jsonify({'success': False,
                                'error': '無効化されたカテゴリーは付与できません'}), 400
        cursor.execute(
            "UPDATE kitare_cases SET category_id = %s, updated_at = %s "
            "WHERE id = %s", (category_id, get_jst_now(), case_id))
        conn.commit()
        return jsonify({'success': True,
                        'category_id':   category_id,
                        'category_name': get_category_name(category_id)
                                         if category_id else ''})
    except Exception as e:
        logging.error("api_set_case_category error: %s", e)
        if conn and conn.is_connected():
            try: conn.rollback()
            except Exception: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


# ════════════════════════════════════════════
# API: 案件詳細・閲覧
# ════════════════════════════════════════════

@kitare_deliberation_bp.route('/api/case/<int:case_id>', methods=['GET'])
@login_required
def api_case_detail(case_id):
    """案件詳細を返す。審議トラック画面（detail.html）が使う。
       業務会議・担当委員・admin 向け。担当委員・COI 情報も含む。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not can_access_review_track(user_id, case_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    documents = _get_documents(case_id)

    # 担当委員一覧（運営委員会全員。COIフラグ付き）
    reviewers = []
    coi_ids = set(get_coi_user_ids(case_id))
    try:
        committee_ids = set(get_group_member_ids(GROUP_COMMITTEE))
        committee_ids |= set(get_group_member_ids(GROUP_CHAIR))
    except Exception as e:
        logging.error("api_case_detail: グループ取得失敗: %s", e)
        committee_ids = set()
    for uid in sorted(committee_ids):
        reviewers.append({
            'user_id':   uid,
            'user_name': get_user_display_name(uid),
            'is_chair':  is_chair(uid),
            'is_coi':    uid in coi_ids,
        })

    # COIの詳細（理由つき）
    cois = []
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT c.user_id, c.reason, c.set_at, u.full_name AS user_name "
            "FROM kitare_coi c LEFT JOIN users u ON u.id = c.user_id "
            "WHERE c.case_id = %s ORDER BY c.set_at ASC", (case_id,))
        for r in cursor.fetchall():
            cois.append({
                'user_id':   r['user_id'],
                'user_name': r['user_name'] or f"User#{r['user_id']}",
                'reason':    r.get('reason') or '',
                'set_at':    fmt_datetime(r.get('set_at')),
            })
    except Exception as e:
        logging.error("api_case_detail COI error: %s", e)
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()

    return jsonify({
        'success':   True,
        'case':      _serialize_case(case, include_category=True),
        'documents': documents,
        'reviewers': reviewers,
        'cois':      cois,
    })


@kitare_deliberation_bp.route('/api/case/<int:case_id>/view', methods=['GET'])
@login_required
def api_case_view(case_id):
    """申請内容（案件情報・書類セット）の閲覧専用 API。
       審議の議論や担当委員情報は含まない。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not can_view_application_content(user_id, case):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    return jsonify({
        'success':   True,
        'case':      _serialize_case(case),
        'documents': _get_documents(case_id),
    })


# ════════════════════════════════════════════
# API: 一覧（タブ別）
# ════════════════════════════════════════════

@kitare_deliberation_bp.route('/api/my_cases', methods=['GET'])
@login_required
def api_my_cases():
    """申請者タブ：自分が申請した案件一覧。全ユーザー可。"""
    user_id = session.get('user_id')
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM kitare_cases WHERE applicant_user_id = %s "
            "ORDER BY fiscal_year DESC, seq_no DESC", (user_id,))
        cases = [_serialize_case(r) for r in cursor.fetchall()]
        return jsonify({'success': True, 'cases': cases})
    except Exception as e:
        logging.error("api_my_cases error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/business_cases', methods=['GET'])
@login_required
def api_business_cases():
    """業務会議タブ：受理判断が必要な案件＋全案件。業務会議メンバーのみ。
       クエリ: fiscal_year（任意）, status（任意）"""
    user_id = session.get('user_id')
    if not is_business(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    fy     = request.args.get('fiscal_year', type=int)
    status = request.args.get('status') or None
    where, params = [], []
    if fy:
        where.append("c.fiscal_year = %s"); params.append(fy)
    if status and status in STATUS_LABELS:
        where.append("c.status = %s"); params.append(status)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT c.*, cat.name AS category_name "
            "FROM kitare_cases c "
            "LEFT JOIN kitare_categories cat ON cat.id = c.category_id "
            + where_sql +
            " ORDER BY c.fiscal_year DESC, c.seq_no DESC", params)
        cases = [_serialize_case(r, include_category=True)
                 for r in cursor.fetchall()]
        return jsonify({'success': True, 'cases': cases})
    except Exception as e:
        logging.error("api_business_cases error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/committee_cases', methods=['GET'])
@login_required
def api_committee_cases():
    """運営委員会タブ：案件一覧（取り下げ・打ち切りを含む。不受理は除く）。
       公平性のため業務会議で受理判断済みの不受理以外は業務会議タブと同じ母集団を見せる。
       運営委員会メンバーのみ。一般委員は自分がCOIの案件を除く。
       クエリ: fiscal_year（任意）, status（任意）"""
    user_id = session.get('user_id')
    if not is_committee(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    fy     = request.args.get('fiscal_year', type=int)
    status = request.args.get('status') or None
    where  = ["c.status <> 'not_accepted'"]
    params = []
    if fy:
        where.append("c.fiscal_year = %s"); params.append(fy)
    if status and status in STATUS_LABELS and status != 'not_accepted':
        where.append("c.status = %s"); params.append(status)
    where_sql = 'WHERE ' + ' AND '.join(where)

    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT c.*, cat.name AS category_name "
            "FROM kitare_cases c "
            "LEFT JOIN kitare_categories cat ON cat.id = c.category_id "
            + where_sql +
            " ORDER BY c.fiscal_year DESC, c.seq_no DESC", params)
        rows = cursor.fetchall()
    except Exception as e:
        logging.error("api_committee_cases error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()

    # admin・委員長・事務局は全件、一般委員は自分がCOIの案件を除く
    cases = []
    for r in rows:
        if not (is_business(user_id) or is_admin(user_id)):
            if is_coi(user_id, r['id']):
                continue
        cases.append(_serialize_case(r, include_category=True))
    return jsonify({'success': True, 'cases': cases})


def _fetch_public_cases(fy):
    """公開状況（審議中と可決）の行。(cases, error) を返す。
       申請前・受理判断待ち・否決・不受理・取り下げ・打ち切りは含めない。"""
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM kitare_cases "
            "WHERE fiscal_year = %s AND status IN ('accepted_review','approved') "
            "ORDER BY seq_no DESC", (fy,))
        cases = []
        for r in cursor.fetchall():
            cases.append({
                'case_display_id': r['case_display_id'],
                'title':           _public_title_of(r),
                'status':          r['status'],
                'status_label':    STATUS_LABELS.get(r['status'], r['status']),
                'received_date':   fmt_date(r.get('received_date')),
                'final_date':      fmt_date(r.get('final_date')),
            })
        return cases, None
    except Exception as e:
        logging.error("_fetch_public_cases error: %s", e)
        return [], str(e)
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


def _fetch_public_approved(fy=None):
    """可決案件の行。fy が None なら全年度。(cases, error) を返す。"""
    where = ["status = 'approved'"]
    params = []
    if fy:
        where.append("fiscal_year = %s"); params.append(fy)
    where_sql = 'WHERE ' + ' AND '.join(where)
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM kitare_cases " + where_sql +
            " ORDER BY fiscal_year DESC, seq_no DESC", params)
        cases = []
        for r in cursor.fetchall():
            cases.append({
                'case_display_id': r['case_display_id'],
                'title':           _public_title_of(r),
                'approved_date':   fmt_date(r.get('approved_date')),
            })
        return cases, None
    except Exception as e:
        logging.error("_fetch_public_approved error: %s", e)
        return [], str(e)
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()


@kitare_deliberation_bp.route('/api/public/cases', methods=['GET'])
@login_required
def api_public_cases():
    """パブリック：受付状況一覧。タイトルまで（特定案件はぼかし）。
       表示するのは審議中（accepted_review）と可決（approved）のみ。"""
    fy = request.args.get('fiscal_year', type=int) or get_current_fiscal_year()
    cases, error = _fetch_public_cases(fy)
    if error:
        return jsonify({'success': False, 'error': error}), 500
    return jsonify({'success': True, 'cases': cases})


@kitare_deliberation_bp.route('/api/public/approved', methods=['GET'])
@login_required
def api_public_approved():
    """パブリック：可決案件一覧。タイトルまで（特定案件はぼかし）。"""
    fy = request.args.get('fiscal_year', type=int)
    cases, error = _fetch_public_approved(fy)
    if error:
        return jsonify({'success': False, 'error': error}), 500
    return jsonify({'success': True, 'cases': cases})


# ════════════════════════════════════════════
# API: 年度報告
# ════════════════════════════════════════════

@kitare_deliberation_bp.route('/api/report', methods=['GET'])
@login_required
def api_report():
    """年度集計＋一覧。業務会議向け。"""
    user_id = session.get('user_id')
    if not is_business(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    fy = request.args.get('fiscal_year', type=int) or get_current_fiscal_year()
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT c.*, cat.name AS category_name "
            "FROM kitare_cases c "
            "LEFT JOIN kitare_categories cat ON cat.id = c.category_id "
            "WHERE c.fiscal_year = %s "
            "ORDER BY c.seq_no DESC", (fy,))
        rows = cursor.fetchall()
    except Exception as e:
        logging.error("api_report error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()

    summary = {'total': len(rows), 'in_review': 0, 'approved': 0,
               'rejected': 0, 'not_accepted': 0, 'withdrawn': 0,
               'terminated': 0}
    for r in rows:
        st = r['status']
        if st in ('channel_open', 'documents_uploading', 'submitted',
                  'accepted_review'):
            summary['in_review'] += 1
        elif st in summary:
            summary[st] += 1
    return jsonify({'success': True, 'summary': summary,
                    'cases': [_serialize_case(r, include_category=True)
                              for r in rows]})


@kitare_deliberation_bp.route('/api/report/csv', methods=['GET'])
@login_required
def api_report_csv():
    """年度報告 CSV。業務会議向け。"""
    user_id = session.get('user_id')
    if not is_business(user_id):
        return Response('権限がありません', status=403)

    fy = request.args.get('fiscal_year', type=int) or get_current_fiscal_year()
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT c.*, cat.name AS category_name "
            "FROM kitare_cases c "
            "LEFT JOIN kitare_categories cat ON cat.id = c.category_id "
            "WHERE c.fiscal_year = %s "
            "ORDER BY c.seq_no ASC", (fy,))
        rows = cursor.fetchall()
    except Exception as e:
        logging.error("api_report_csv error: %s", e)
        return Response('エラー: ' + str(e), status=500)
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()

    buf = io.StringIO()
    buf.write('\ufeff')
    w = csv.writer(buf)
    w.writerow(['案件番号', '案件名', 'カテゴリー', '特定案件', '申請者',
                'ステータス', '審議期日', '受付日', '終局日', '可決日'])
    for r in rows:
        w.writerow([
            r['case_display_id'], r['title'],
            r.get('category_name') or '',
            'はい' if r.get('is_personnel') else '',
            r.get('applicant_name') or '',
            STATUS_LABELS.get(r['status'], r['status']),
            fmt_date(r.get('deadline')),
            fmt_date(r.get('received_date')),
            fmt_date(r.get('final_date')),
            fmt_date(r.get('approved_date')),
        ])
    return Response(
        buf.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition':
                 f'attachment; filename="kitare_report_{fy}.csv"'})


# ════════════════════════════════════════════
# 締切バッチ（PythonAnywhere Scheduled Task から日次で叩く）
# ════════════════════════════════════════════

@kitare_deliberation_bp.route('/api/deadline_batch', methods=['GET', 'POST'])
def api_deadline_batch():
    """締切リマインダの日次バッチ。
       「その日が審議期日」かつ「審議中（accepted_review）」かつ
       「まだその期日で通知していない」案件を拾い、Slack へ通知する。

       login_required は付けない（バッチ＝無人実行のため）。代わりに
       config の KITARE_DEADLINE_BATCH_TOKEN とクエリ token を照合する。

       Scheduled Task のコマンド例：
         curl -s "https://<host>/kitare_deliberation/api/deadline_batch?token=XXXX"
    """
    if request.is_json:
        token = (request.json or {}).get('token') or request.args.get('token')
    else:
        token = request.args.get('token')
    if not DEADLINE_BATCH_TOKEN or token != DEADLINE_BATCH_TOKEN:
        return jsonify({'success': False, 'error': '認証に失敗しました'}), 403

    today = get_jst_today()
    conn = None
    try:
        conn   = _conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM kitare_cases "
            "WHERE status = 'accepted_review' AND deadline = %s "
            "  AND (deadline_notified_for IS NULL "
            "       OR deadline_notified_for <> %s)",
            (today, today))
        targets = cursor.fetchall()
    except Exception as e:
        logging.error("api_deadline_batch query error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close(); conn.close()

    notified = 0
    from .slack_notifier import notify_deadline_reached
    for case in targets:
        try:
            res = notify_deadline_reached(
                case_display_id=case.get('case_display_id'),
                title=case.get('title'),
                deadline=fmt_date(case.get('deadline')),
                case_url=case_url(case['id']))
            # 通知できた場合のみ「通知済み」を記録（二重送信防止）
            if res.get('ok'):
                c2 = None
                try:
                    c2 = _conn()
                    cur2 = c2.cursor()
                    cur2.execute(
                        "UPDATE kitare_cases SET deadline_notified_for = %s "
                        "WHERE id = %s", (today, case['id']))
                    c2.commit()
                    notified += 1
                finally:
                    if c2 and c2.is_connected():
                        cur2.close(); c2.close()
        except Exception as e:
            logging.warning("api_deadline_batch: 案件%s の通知でエラー: %s",
                            case.get('case_display_id'), e)

    logging.info("kitare deadline_batch: %d件対象, %d件通知",
                 len(targets), notified)
    return jsonify({'success': True, 'targets': len(targets),
                    'notified': notified})
