"""
stl_manager (えすてぃまね) — Blueprint routes

【権限モデル】
  admin ユーザ (user_category='admin') は設定にかかわらず全権限を持つ。

  staff   : 事務局。提出状況の確認、受理・指示、開示記事の作成、
            団体・メンバー・レポート型の管理。machinaka の approve 相当。
            stl_staff に active 登録されたユーザ。
  editor  : 団体メンバーのうち role='editor'。自団体の報告書を
            作成・編集・提出できる。
  viewer  : 団体メンバーのうち role='viewer'。自団体の報告書を閲覧できる。
  view    : 開示記事(disclosure)の閲覧ポリシー。private/group/domestic/public。

【アクセス設定の保存方式】
  stl_access_settings にグループ名 (group_name) で保存する（まいぐる移行対応）。
  view ポリシーは access_type='view', group_name='__policy_<name>__' で表現。

【締め日】
  stl_settings.closing_day（1〜31）。当月レポートの提出判定に使う。
  締め日がその月に存在しない場合は月末日にクランプする。

【報告書のルール】
  * 提出日ベース（submitted_at）。月キーは持たない。
  * status: draft（下書き）/ submitted（提出済）。
  * 事務局の受理 = received_at / received_by。本文を編集すると
    受理はクリアされ、instruction_note は保持する。
"""

import os
import json
import datetime
import calendar as cal_module
from datetime import timedelta
from functools import wraps
from auth import redirect_to_dashboard  # 冒頭のimportに

from flask import (render_template, request, redirect, url_for,
                   session, flash, abort, jsonify, send_from_directory)
from pytz import timezone
import mysql.connector
from werkzeug.utils import secure_filename

from decorators import login_required
from db import DatabaseConfig
from . import stl_bp

# ─────────────────────────────────────────────────────────────────
# 画像アップロード設定
# ─────────────────────────────────────────────────────────────────
# 報告書に貼る画像はアプリ配下に置く（プラットフォーム規約．GitHub には送られない）．
# 配信は下の image ルートが行う．旧版は ~/static/stl_imgs/ に置き /static/stl_imgs/ で配信していた．
STL_UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'stl_imgs')
STL_LEGACY_UPLOAD_FOLDER = os.path.join(os.path.expanduser('~'), 'static', 'stl_imgs')
STL_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def _stl_allowed_file(filename):
    return '.' in filename and \
        filename.rsplit('.', 1)[1].lower() in STL_ALLOWED_EXTENSIONS


# Markdown プレビュー（共有部品）。フォールバック付き。
try:
    from markdown_converter import process_markdown_for_preview
except ImportError:
    def process_markdown_for_preview(text):
        return text


JST = timezone('Asia/Tokyo')

def get_jst_now():
    return datetime.datetime.now(JST).replace(tzinfo=None)


PUBLIC_SENTINEL = '__public__'


# ─────────────────────────────────────────────────────────────────
# アクセスログ記録
# ─────────────────────────────────────────────────────────────────
def _log_access(endpoint, ref_team_id=None, ref_report_id=None,
                ref_disclosure_id=None):
    """stl_access_logs に1行記録。例外は握りつぶしてアプリを止めない。"""
    try:
        ip = (request.headers.get('X-Forwarded-For', '') or '').split(',')[0].strip() \
             or request.remote_addr
        ua = (request.user_agent.string or '')[:512]
        uid = current_uid()
        now = get_jst_now()

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO stl_access_logs
              (accessed_at, endpoint, user_id, ip_address, user_agent,
               ref_team_id, ref_report_id, ref_disclosure_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (now, endpoint, uid, ip, ua,
              ref_team_id, ref_report_id, ref_disclosure_id))
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────
# Jinja2 フィルタ（衝突回避で _s サフィックス）
# ─────────────────────────────────────────────────────────────────
@stl_bp.app_template_filter('fmtdate_s')
def fmtdate_s(d, fmt='%Y/%m/%d'):
    if d is None:
        return ''
    if isinstance(d, str):
        try:
            d = datetime.date.fromisoformat(d)
        except ValueError:
            return d
    return d.strftime(fmt)


@stl_bp.app_template_filter('fmtdatetime_s')
def fmtdatetime_s(dt, fmt='%Y/%m/%d %H:%M'):
    if dt is None:
        return ''
    if isinstance(dt, str):
        return dt
    return dt.strftime(fmt)


# ─────────────────────────────────────────────────────────────────
# セッション / 権限の基本
# ─────────────────────────────────────────────────────────────────
def is_admin_user():
    return session.get('user_category') == 'admin'


def current_uid():
    return session.get('user_id')


def _ug(name):
    """まいぐる（ユーザとグループ）の公開APIを遅延で引く．無ければ None．"""
    try:
        from fujinp.user_groups import utils as _ug_utils
        return getattr(_ug_utils, name, None)
    except Exception:
        return None


def user_group_names(user_id):
    """現在所属するグループ名．まいぐるの公開API（台帳由来のグループも含む）を使い，
       古いまいぐるで API が無いときだけ user_group_memberships を直接読む．"""
    if not user_id:
        return []
    fn = _ug('get_user_group_names')
    if fn:
        try:
            return list(fn(user_id) or [])
        except Exception:
            pass
    try:
        now = get_jst_now()
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT g.name
              FROM user_group_memberships m
              JOIN user_groups g ON g.id = m.group_id
             WHERE m.user_id = %s
               AND (m.valid_from  IS NULL OR m.valid_from  <= %s)
               AND (m.valid_until IS NULL OR m.valid_until >= %s)
        """, (user_id, now, now))
        return [r['name'] for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


def access_group_names(access_type):
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT group_name FROM stl_access_settings
             WHERE access_type = %s
        """, (access_type,))
        return [r['group_name'] for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


# ─────────────────────────────────────────────────────────────────
# 事務局（staff = approve 相当）
# ─────────────────────────────────────────────────────────────────
def is_registered_staff(user_id):
    """stl_staff に active 登録されているか"""
    if not user_id:
        return False
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor()
        cur.execute("""
            SELECT 1 FROM stl_staff
             WHERE user_id=%s AND active=1 LIMIT 1
        """, (user_id,))
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


def is_staff(user_id=None):
    if is_admin_user():
        return True
    if user_id is None:
        user_id = current_uid()
    if is_stl_manager(user_id):   # ← 追加: STL管理グループは staff の上位
        return True
    return is_registered_staff(user_id)


# ─────────────────────────────────────────────────────────────────
# STL 管理者（設定画面ガード）
# ─────────────────────────────────────────────────────────────────
MANAGER_GROUP = 'STL管理'


def is_stl_manager(user_id=None):
    if is_admin_user():
        return True
    if user_id is None:
        user_id = current_uid()
    if not user_id:
        return False
    return MANAGER_GROUP in user_group_names(user_id)


def require_manager(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not is_stl_manager(current_uid()):
            abort(403)
        return f(*args, **kwargs)
    return wrapped


def require_staff(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not is_staff(current_uid()):
            abort(403)
        return f(*args, **kwargs)
    return wrapped


# ─────────────────────────────────────────────────────────────────
# 団体メンバーシップ（editor / viewer）
# ─────────────────────────────────────────────────────────────────
def user_team_roles(user_id):
    """
    ユーザが所属する team の {team_id: role} を返す。
    team は年度別レコードなので、全年度ぶんが返る。
    """
    if not user_id:
        return {}
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT team_id, role FROM stl_team_member WHERE user_id=%s
        """, (user_id,))
        return {r['team_id']: r['role'] for r in cur.fetchall()}
    except Exception:
        return {}
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


def member_role_for_team(user_id, team_id):
    """指定 team における role を返す（'editor'|'viewer'|None）。admin/staff は editor 扱い。"""
    if is_admin_user() or is_registered_staff(user_id):
        return 'editor'
    if not user_id:
        return None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT role FROM stl_team_member
             WHERE user_id=%s AND team_id=%s LIMIT 1
        """, (user_id, team_id))
        row = cur.fetchone()
        return row['role'] if row else None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


def can_edit_team(user_id, team_id):
    return member_role_for_team(user_id, team_id) == 'editor'


def can_view_team(user_id, team_id):
    return member_role_for_team(user_id, team_id) in ('editor', 'viewer')


# ─────────────────────────────────────────────────────────────────
# view ポリシー（開示記事の閲覧。machinaka と同型）
# ─────────────────────────────────────────────────────────────────
VIEW_POLICY_PREFIX = '__policy_'
VIEW_POLICY_SUFFIX = '__'
VALID_VIEW_POLICIES = ('private', 'group', 'domestic', 'public')
DEFAULT_VIEW_POLICY = 'private'


def _policy_marker(policy):
    return f'{VIEW_POLICY_PREFIX}{policy}{VIEW_POLICY_SUFFIX}'


def _is_policy_marker(group_name):
    return group_name.startswith(VIEW_POLICY_PREFIX) and \
        group_name.endswith(VIEW_POLICY_SUFFIX)


def get_view_policy():
    for gname in access_group_names('view'):
        if _is_policy_marker(gname):
            policy = gname[len(VIEW_POLICY_PREFIX):-len(VIEW_POLICY_SUFFIX)]
            if policy in VALID_VIEW_POLICIES:
                return policy
    return DEFAULT_VIEW_POLICY


def get_view_allowed_groups():
    return [g for g in access_group_names('view') if not _is_policy_marker(g)]


# ─────────────────────────────────────────────────────────────────
# 締め日
# ─────────────────────────────────────────────────────────────────
def get_setting(skey, default=None):
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT sval FROM stl_settings WHERE skey=%s", (skey,))
        row = cur.fetchone()
        return row['sval'] if row else default
    except Exception:
        return default
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


def set_setting(skey, sval):
    conn = mysql.connector.connect(**DatabaseConfig.default())
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO stl_settings (skey, sval, updated_at)
        VALUES (%s,%s,%s)
        ON DUPLICATE KEY UPDATE sval=VALUES(sval), updated_at=VALUES(updated_at)
    """, (skey, sval, get_jst_now()))
    conn.commit()
    cur.close(); conn.close()


def get_closing_day():
    raw = get_setting('closing_day', '25')
    try:
        d = int(raw)
    except (TypeError, ValueError):
        d = 25
    return min(31, max(1, d))


def effective_closing_date(year, month, closing_day=None):
    """指定年月の実効締切日。締め日がその月に無ければ月末にクランプ。"""
    if closing_day is None:
        closing_day = get_closing_day()
    last = cal_module.monthrange(year, month)[1]
    return datetime.date(year, month, min(closing_day, last))


# ─────────────────────────────────────────────────────────────────
# 年度ユーティリティ（4月始まり）
# ─────────────────────────────────────────────────────────────────
def fiscal_year_of(d):
    return d.year if d.month >= 4 else d.year - 1


def current_fiscal_year():
    return fiscal_year_of(datetime.date.today())


def fiscal_months(fy):
    """年度 fy の (year, month) を 4月〜翌3月の順で返す。"""
    out = []
    for m in range(4, 13):
        out.append((fy, m))
    for m in range(1, 4):
        out.append((fy + 1, m))
    return out


# ─────────────────────────────────────────────────────────────────
# ラベル・装飾
# ─────────────────────────────────────────────────────────────────
REPORT_STATUS_META = {
    'draft':     ('下書き', '#94a3b8'),
    'submitted': ('提出済', '#10b981'),
}

SCOPE_META = {
    'group':    ('STL関係者', '#0ea5e9'),
    'internal': ('学内',     '#f59e0b'),
    'public':   ('一般公開', '#10b981'),
}


def enrich_report(r):
    st = r.get('status', 'draft')
    r['status_label'], r['status_color'] = REPORT_STATUS_META.get(st, (st, '#888'))
    r['is_received'] = r.get('received_at') is not None
    return r


def enrich_disclosure(d):
    sc = d.get('scope', 'group')
    d['scope_label'], d['scope_color'] = SCOPE_META.get(sc, (sc, '#888'))
    d['is_published'] = d.get('published_at') is not None
    return d


# ═════════════════════════════════════════════════════════════════
# 事務局ダッシュボード（提出状況マトリクス：月 × 団体）
# ═════════════════════════════════════════════════════════════════
@stl_bp.route('/')
@login_required
def index():
    """アプリのトップ．事務局は提出状況の一覧，それ以外は自分の団体へ．"""
    _log_access('dashboard')
    uid = current_uid()
    staff_ok = is_staff(uid)
    manager_ok = is_stl_manager(uid)

    # 事務局でなければ自分の団体ダッシュボードへ
    if not staff_ok:
        return redirect(url_for('stl.my_teams'))

    try:
        fy = int(request.args.get('fy', current_fiscal_year()))
    except ValueError:
        fy = current_fiscal_year()

    closing_day = get_closing_day()
    months = fiscal_months(fy)   # [(year, month), ...] 4月〜翌3月

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)

        # 利用可能な年度一覧
        cur.execute("SELECT DISTINCT fiscal_year FROM stl_team ORDER BY fiscal_year DESC")
        fiscal_years = [r['fiscal_year'] for r in cur.fetchall()]
        if fy not in fiscal_years:
            fiscal_years = sorted(set(fiscal_years + [fy]), reverse=True)

        # 当年度の団体（active のみ、表示順）
        cur.execute("""
            SELECT id, name, summary, display_order
              FROM stl_team
             WHERE fiscal_year=%s AND active=1
             ORDER BY display_order, name
        """, (fy,))
        teams = cur.fetchall()

        # 当年度団体の提出済レポート（射影用）
        team_ids = [t['id'] for t in teams]
        submissions = []
        if team_ids:
            fmt = ','.join(['%s'] * len(team_ids))
            cur.execute(f"""
                SELECT r.id, r.team_id, r.title, r.status,
                       r.submitted_at, r.received_at,
                       rt.name AS type_name
                  FROM stl_report r
                  LEFT JOIN stl_report_type rt ON rt.id = r.report_type_id
                 WHERE r.team_id IN ({fmt})
                   AND r.status='submitted'
                   AND r.submitted_at IS NOT NULL
                 ORDER BY r.submitted_at
            """, team_ids)
            submissions = cur.fetchall()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        teams, submissions, fiscal_years = [], [], [fy]
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    # 射影: matrix[team_id][(y,m)] = [reports...]
    matrix = {t['id']: {ym: [] for ym in months} for t in teams}
    for s in submissions:
        sd = s['submitted_at']
        ym = (sd.year, sd.month)
        if s['team_id'] in matrix and ym in matrix[s['team_id']]:
            matrix[s['team_id']][ym].append(s)

    # テンプレートに渡しやすい形へ
    month_labels = [f'{m}月' for (_, m) in months]
    rows = []
    for t in teams:
        cells = []
        for ym in months:
            reps = matrix[t['id']][ym]
            received = sum(1 for x in reps if x['received_at'] is not None)
            cells.append({
                'ym': ym, 'count': len(reps),
                'received': received, 'reports': reps,
            })
        rows.append({'team': t, 'cells': cells})

    return render_template('stl_manager/stl_dashboard.html',
        uid=uid, staff_ok=staff_ok, manager_ok=manager_ok,
        fy=fy, fiscal_years=fiscal_years,
        months=months, month_labels=month_labels,
        rows=rows, closing_day=closing_day,
        today=datetime.date.today())


# ═════════════════════════════════════════════════════════════════
# 学生団体メンバー向け：自分の団体一覧
# ═════════════════════════════════════════════════════════════════
@stl_bp.route('/my')
@login_required
def my_teams():
    _log_access('my_teams')
    uid = current_uid()
    today = datetime.date.today()
    closing_day = get_closing_day()
    cur_fy = current_fiscal_year()

    roles = user_team_roles(uid)

    # どの団体にも所属しておらず、事務局でもないユーザは
    # 公開記事（開示記事の公開一覧）へ誘導する
    if not roles and not is_staff(uid):
        return redirect(url_for('stl.disclosures'))

    teams = []
    if roles:
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur = conn.cursor(dictionary=True)
            ids = list(roles.keys())
            fmt = ','.join(['%s'] * len(ids))
            cur.execute(f"""
                SELECT id, fiscal_year, name, summary, active
                  FROM stl_team
                 WHERE id IN ({fmt})
                 ORDER BY fiscal_year DESC, display_order, name
            """, ids)
            teams = cur.fetchall()

            # 当月（today の月）の提出有無を団体ごとに判定
            this_y, this_m = today.year, today.month
            m_start = datetime.date(this_y, this_m, 1)
            last = cal_module.monthrange(this_y, this_m)[1]
            m_end = datetime.date(this_y, this_m, last)
            for t in teams:
                t['role'] = roles.get(t['id'])
                cur.execute("""
                    SELECT COUNT(*) AS n FROM stl_report
                     WHERE team_id=%s AND status='submitted'
                       AND submitted_at >= %s AND submitted_at < %s
                """, (t['id'], m_start, m_end + timedelta(days=1)))
                t['submitted_this_month'] = cur.fetchone()['n'] > 0
                t['closing_date'] = effective_closing_date(this_y, this_m, closing_day)
                t['is_current_fy'] = (t['fiscal_year'] == cur_fy)
        except Exception as e:
            flash(f'DB エラー: {e}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    return render_template('stl_manager/stl_my_teams.html',
        uid=uid, teams=teams, today=today,
        this_month=today.month, staff_ok=is_staff(uid),
        manager_ok=is_stl_manager(uid))


# ═════════════════════════════════════════════════════════════════
# 団体のレポート一覧
# ═════════════════════════════════════════════════════════════════
@stl_bp.route('/team/<int:team_id>')
@login_required
def team_reports(team_id):
    _log_access('team_reports', ref_team_id=team_id)
    uid = current_uid()
    today = datetime.date.today()
    closing_day = get_closing_day()

    if not (can_view_team(uid, team_id) or is_staff(uid)):
        abort(403)

    role = member_role_for_team(uid, team_id)
    can_edit = (role == 'editor')

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM stl_team WHERE id=%s", (team_id,))
        team = cur.fetchone()
        if not team:
            abort(404)
        cur.execute("""
            SELECT r.*, rt.name AS type_name,
                   u.full_name AS author_name,
                   rv.full_name AS receiver_name
              FROM stl_report r
              LEFT JOIN stl_report_type rt ON rt.id = r.report_type_id
              LEFT JOIN users u  ON u.id  = r.created_by
              LEFT JOIN users rv ON rv.id = r.received_by
             WHERE r.team_id=%s
             ORDER BY COALESCE(r.submitted_at, r.created_at) DESC
        """, (team_id,))
        reports = [enrich_report(r) for r in cur.fetchall()]
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        team, reports = None, []
        if team is None:
            abort(500)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    # 当月の提出判定（今月分が出ているか）
    m_start = datetime.date(today.year, today.month, 1)
    last = cal_module.monthrange(today.year, today.month)[1]
    submitted_this_month = any(
        r['status'] == 'submitted' and r['submitted_at']
        and m_start <= r['submitted_at'].date() <= datetime.date(today.year, today.month, last)
        for r in reports
    )
    closing_date = effective_closing_date(today.year, today.month, closing_day)

    return render_template('stl_manager/stl_team_reports.html',
        uid=uid, team=team, reports=reports,
        role=role, can_edit=can_edit, staff_ok=is_staff(uid),
        today=today, this_month=today.month,
        submitted_this_month=submitted_this_month,
        closing_date=closing_date, closing_day=closing_day,
        past_due=(today > closing_date and not submitted_this_month))


# ═════════════════════════════════════════════════════════════════
# レポート詳細
# ═════════════════════════════════════════════════════════════════
@stl_bp.route('/report/<int:rid>')
@login_required
def report_detail(rid):
    _log_access('report_detail', ref_report_id=rid)
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT r.*, rt.name AS type_name,
                   t.name AS team_name, t.fiscal_year,
                   u.full_name AS author_name,
                   rv.full_name AS receiver_name
              FROM stl_report r
              LEFT JOIN stl_report_type rt ON rt.id = r.report_type_id
              LEFT JOIN stl_team t ON t.id = r.team_id
              LEFT JOIN users u  ON u.id  = r.created_by
              LEFT JOIN users rv ON rv.id = r.received_by
             WHERE r.id=%s
        """, (rid,))
        report = cur.fetchone()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        abort(500)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    if not report:
        abort(404)

    staff_ok = is_staff(uid)
    if not (can_view_team(uid, report['team_id']) or staff_ok):
        abort(403)

    enrich_report(report)
    can_edit = can_edit_team(uid, report['team_id'])

    body_html = ''
    if report.get('body'):
        try:
            body_html = process_markdown_for_preview(report['body'])
        except Exception:
            body_html = report['body']

    return render_template('stl_manager/stl_report_detail.html',
        uid=uid, report=report, body_html=body_html,
        can_edit=can_edit, staff_ok=staff_ok,
        is_author=(uid is not None and report['created_by'] == uid))


# ═════════════════════════════════════════════════════════════════
# レポート作成・編集（Markdown エディタ）
# ═════════════════════════════════════════════════════════════════
def _load_active_report_types():
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT id, name FROM stl_report_type
             WHERE active=1 ORDER BY display_order, name
        """)
        return cur.fetchall()
    except Exception:
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


@stl_bp.route('/team/<int:team_id>/report/new', methods=['GET', 'POST'])
@login_required
def report_new(team_id):
    uid = current_uid()
    if not can_edit_team(uid, team_id):
        abort(403)

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM stl_team WHERE id=%s", (team_id,))
        team = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    if not team:
        abort(404)

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        body = request.form.get('body', '').strip()
        rtype = request.form.get('report_type_id', '').strip() or None
        action = request.form.get('action', 'save')   # 'save'(draft) | 'submit'

        if not title:
            flash('タイトルは必須です', 'error')
            return redirect(url_for('stl.report_new', team_id=team_id))

        status = 'submitted' if action == 'submit' else 'draft'
        submitted_at = get_jst_now() if status == 'submitted' else None
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO stl_report
                  (team_id, report_type_id, title, body, status,
                   submitted_at, created_by, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (team_id, rtype, title, body, status,
                  submitted_at, uid, get_jst_now()))
            conn.commit()
            new_id = cur.lastrowid
            flash('提出しました' if status == 'submitted' else '下書きを保存しました', 'success')
            return redirect(url_for('stl.report_detail', rid=new_id))
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    return render_template('stl_manager/stl_report_form.html',
        mode='new', team=team, report=None,
        report_types=_load_active_report_types())


@stl_bp.route('/report/<int:rid>/edit', methods=['GET', 'POST'])
@login_required
def report_edit(rid):
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT r.*, t.name AS team_name
              FROM stl_report r
              LEFT JOIN stl_team t ON t.id = r.team_id
             WHERE r.id=%s
        """, (rid,))
        report = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    if not report:
        abort(404)
    if not (can_edit_team(uid, report['team_id']) or is_staff(uid)):
        abort(403)

    team = {'id': report['team_id'], 'name': report['team_name']}

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        body = request.form.get('body', '').strip()
        rtype = request.form.get('report_type_id', '').strip() or None
        action = request.form.get('action', 'save')

        if not title:
            flash('タイトルは必須です', 'error')
            return redirect(url_for('stl.report_edit', rid=rid))

        # 提出済みを編集すると受理はクリア（再受理を要求）。指示は保持。
        if action == 'submit':
            status = 'submitted'
            submitted_at_sql = "submitted_at=COALESCE(submitted_at,%s),"
            submitted_param = [get_jst_now()]
        elif action == 'unsubmit':
            status = 'draft'
            submitted_at_sql = "submitted_at=NULL,"
            submitted_param = []
        else:
            status = report['status']
            submitted_at_sql = ""
            submitted_param = []

        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur = conn.cursor()
            sql = f"""
                UPDATE stl_report SET
                  title=%s, body=%s, report_type_id=%s, status=%s,
                  {submitted_at_sql}
                  received_at=NULL, received_by=NULL,
                  updated_at=%s
                 WHERE id=%s
            """
            params = [title, body, rtype, status] + submitted_param + [get_jst_now(), rid]
            cur.execute(sql, params)
            conn.commit()
            flash('保存しました（受理記録はクリアされました）', 'success')
            return redirect(url_for('stl.report_detail', rid=rid))
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    enrich_report(report)
    return render_template('stl_manager/stl_report_form.html',
        mode='edit', team=team, report=report,
        report_types=_load_active_report_types())


@stl_bp.route('/report/<int:rid>/delete', methods=['POST'])
@login_required
def report_delete(rid):
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT team_id FROM stl_report WHERE id=%s", (rid,))
        row = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    if not row:
        abort(404)
    team_id = row['team_id']
    if not (can_edit_team(uid, team_id) or is_staff(uid)):
        abort(403)
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor()
        cur.execute("DELETE FROM stl_report WHERE id=%s", (rid,))
        conn.commit()
        flash('レポートを削除しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('stl.team_reports', team_id=team_id))


@stl_bp.route('/report/upload_image', methods=['POST'])
@login_required
def report_upload_image():
    """レポート本文用 画像アップロード"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400
    if not _stl_allowed_file(file.filename):
        return jsonify({'success': False,
                        'error': '許可されていないファイル形式です（png/jpg/jpeg/gif/webp）'}), 400
    try:
        os.makedirs(STL_UPLOAD_FOLDER, exist_ok=True)
        timestamp = get_jst_now().strftime('%Y%m%d_%H%M%S')
        filename = secure_filename(file.filename)
        name, ext = os.path.splitext(filename)
        unique = f"{name}_{timestamp}{ext}"
        file.save(os.path.join(STL_UPLOAD_FOLDER, unique))
        return jsonify({'success': True, 'filename': unique,
                        'url': url_for('stl.image', filename=unique)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@stl_bp.route('/img/<path:filename>')
def image(filename):
    """報告書・開示記事の画像．開示記事は未ログインでも読めるので，ここもログイン不要．
       名前はアップロード時刻つきで推測しにくい．アプリ配下に無ければ旧置き場を見る．"""
    name = secure_filename(filename)
    if not name or name != filename:
        abort(404)
    for folder in (STL_UPLOAD_FOLDER, STL_LEGACY_UPLOAD_FOLDER):
        if os.path.isfile(os.path.join(folder, name)):
            return send_from_directory(folder, name)
    abort(404)


# ═════════════════════════════════════════════════════════════════
# 事務局：レポート受理・指示
# ═════════════════════════════════════════════════════════════════
@stl_bp.route('/report/<int:rid>/receive', methods=['POST'])
@require_staff
def report_receive(rid):
    uid = current_uid()
    note = request.form.get('instruction_note', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor()
        cur.execute("""
            UPDATE stl_report SET
              received_at=%s, received_by=%s, instruction_note=%s
             WHERE id=%s
        """, (get_jst_now(), uid, note, rid))
        conn.commit()
        flash('受理しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('stl.report_detail', rid=rid))


@stl_bp.route('/report/<int:rid>/unreceive', methods=['POST'])
@require_staff
def report_unreceive(rid):
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor()
        cur.execute("""
            UPDATE stl_report SET received_at=NULL, received_by=NULL WHERE id=%s
        """, (rid,))
        conn.commit()
        flash('受理を取り消しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('stl.report_detail', rid=rid))


@stl_bp.route('/report/<int:rid>/instruction', methods=['POST'])
@require_staff
def report_instruction(rid):
    note = request.form.get('instruction_note', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor()
        cur.execute("UPDATE stl_report SET instruction_note=%s WHERE id=%s", (note, rid))
        conn.commit()
        flash('指示事項を更新しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('stl.report_detail', rid=rid))


# ═════════════════════════════════════════════════════════════════
# 事務局：団体管理（追加・編集・論理削除）
# ═════════════════════════════════════════════════════════════════
@stl_bp.route('/manage/teams', methods=['GET', 'POST'])
@require_staff
def manage_teams():
    uid = current_uid()

    if request.method == 'POST':
        action = request.form.get('action', '')
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur = conn.cursor()

            if action == 'add':
                fy = request.form.get('fiscal_year', '').strip()
                name = request.form.get('name', '').strip()
                summary = request.form.get('summary', '').strip()
                if not fy or not name:
                    flash('年度と団体名は必須です', 'error')
                else:
                    cur.execute("""
                        INSERT INTO stl_team
                          (fiscal_year, name, summary, active, display_order, created_at)
                        VALUES (%s,%s,%s,1,0,%s)
                    """, (fy, name, summary, get_jst_now()))
                    conn.commit()
                    flash('団体を追加しました', 'success')

            elif action == 'edit':
                tid = request.form.get('team_id', '')
                name = request.form.get('name', '').strip()
                summary = request.form.get('summary', '').strip()
                order = request.form.get('display_order', '0').strip() or '0'
                cur.execute("""
                    UPDATE stl_team SET name=%s, summary=%s, display_order=%s, updated_at=%s
                     WHERE id=%s
                """, (name, summary, int(order), get_jst_now(), tid))
                conn.commit()
                flash('団体情報を更新しました', 'success')

            elif action == 'toggle':
                tid = request.form.get('team_id', '')
                cur.execute("UPDATE stl_team SET active=1-active, updated_at=%s WHERE id=%s",
                            (get_jst_now(), tid))
                conn.commit()
                flash('団体の状態を切り替えました', 'success')

            else:
                flash('不明な操作です', 'error')
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()
        return redirect(url_for('stl.manage_teams', fy=request.form.get('fiscal_year_view', '')))

    # GET
    try:
        fy = int(request.args.get('fy') or current_fiscal_year())
    except ValueError:
        fy = current_fiscal_year()

    teams = []
    fiscal_years = []
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT DISTINCT fiscal_year FROM stl_team ORDER BY fiscal_year DESC")
        fiscal_years = [r['fiscal_year'] for r in cur.fetchall()]
        if fy not in fiscal_years:
            fiscal_years = sorted(set(fiscal_years + [fy]), reverse=True)
        cur.execute("""
            SELECT t.*,
                   (SELECT COUNT(*) FROM stl_team_member m WHERE m.team_id=t.id) AS member_count,
                   (SELECT COUNT(*) FROM stl_report r WHERE r.team_id=t.id) AS report_count
              FROM stl_team t
             WHERE t.fiscal_year=%s
             ORDER BY t.active DESC, t.display_order, t.name
        """, (fy,))
        teams = cur.fetchall()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    return render_template('stl_manager/stl_manage_teams.html',
        uid=uid, fy=fy, fiscal_years=fiscal_years, teams=teams,
        current_fy=current_fiscal_year(),
        manager_ok=is_stl_manager(uid))


# ═════════════════════════════════════════════════════════════════
# 事務局：団体メンバー名簿管理（role 付き）
# ═════════════════════════════════════════════════════════════════
@stl_bp.route('/manage/team/<int:team_id>/members', methods=['GET', 'POST'])
@require_staff
def manage_members(team_id):
    uid = current_uid()

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM stl_team WHERE id=%s", (team_id,))
        team = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    if not team:
        abort(404)

    if request.method == 'POST':
        action = request.form.get('action', '')
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur = conn.cursor()

            if action == 'add':
                user_id = request.form.get('user_id', '').strip()
                role = request.form.get('role', 'editor')
                if role not in ('editor', 'viewer'):
                    role = 'editor'
                if user_id:
                    cur.execute("""
                        INSERT INTO stl_team_member (team_id, user_id, role, added_by, added_at)
                        VALUES (%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE role=VALUES(role)
                    """, (team_id, user_id, role, uid, get_jst_now()))
                    conn.commit()
                    flash('メンバーを追加しました', 'success')

            elif action == 'set_role':
                user_id = request.form.get('user_id', '').strip()
                role = request.form.get('role', 'editor')
                if role not in ('editor', 'viewer'):
                    role = 'editor'
                cur.execute("""
                    UPDATE stl_team_member SET role=%s WHERE team_id=%s AND user_id=%s
                """, (role, team_id, user_id))
                conn.commit()
                flash('役割を変更しました', 'success')

            elif action == 'remove':
                user_id = request.form.get('user_id', '').strip()
                cur.execute("""
                    DELETE FROM stl_team_member WHERE team_id=%s AND user_id=%s
                """, (team_id, user_id))
                conn.commit()
                flash('メンバーを外しました', 'success')

            else:
                flash('不明な操作です', 'error')
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()
        return redirect(url_for('stl.manage_members', team_id=team_id))

    # GET
    members, candidates = [], []
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT m.user_id, m.role, m.added_at,
                   u.full_name, u.email,
                   a.full_name AS added_by_name
              FROM stl_team_member m
              LEFT JOIN users u ON u.id = m.user_id
              LEFT JOIN users a ON a.id = m.added_by
             WHERE m.team_id=%s
             ORDER BY m.role, u.full_name
        """, (team_id,))
        members = cur.fetchall()

        cur.execute("""
            SELECT id, full_name, email, category
              FROM users
             WHERE id NOT IN (SELECT user_id FROM stl_team_member WHERE team_id=%s)
             ORDER BY full_name, id
        """, (team_id,))
        candidates = cur.fetchall()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    return render_template('stl_manager/stl_manage_members.html',
        uid=uid, team=team, members=members, candidates=candidates,
        manager_ok=is_stl_manager(uid))


# ═════════════════════════════════════════════════════════════════
# 事務局：レポート型マスタ管理
# ═════════════════════════════════════════════════════════════════
@stl_bp.route('/manage/report_types', methods=['GET', 'POST'])
@require_staff
def manage_report_types():
    uid = current_uid()
    if request.method == 'POST':
        action = request.form.get('action', '')
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur = conn.cursor()
            if action == 'add':
                name = request.form.get('name', '').strip()
                order = request.form.get('display_order', '0').strip() or '0'
                if name:
                    cur.execute("""
                        INSERT INTO stl_report_type (name, display_order, active, created_at)
                        VALUES (%s,%s,1,%s)
                        ON DUPLICATE KEY UPDATE active=1, display_order=VALUES(display_order)
                    """, (name, int(order), get_jst_now()))
                    conn.commit()
                    flash('レポート型を追加しました', 'success')
            elif action == 'edit':
                tid = request.form.get('type_id', '')
                name = request.form.get('name', '').strip()
                order = request.form.get('display_order', '0').strip() or '0'
                cur.execute("UPDATE stl_report_type SET name=%s, display_order=%s WHERE id=%s",
                            (name, int(order), tid))
                conn.commit()
                flash('レポート型を更新しました', 'success')
            elif action == 'toggle':
                tid = request.form.get('type_id', '')
                cur.execute("UPDATE stl_report_type SET active=1-active WHERE id=%s", (tid,))
                conn.commit()
                flash('レポート型の状態を切り替えました', 'success')
            else:
                flash('不明な操作です', 'error')
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()
        return redirect(url_for('stl.manage_report_types'))

    types = []
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT rt.*,
                   (SELECT COUNT(*) FROM stl_report r WHERE r.report_type_id=rt.id) AS use_count
              FROM stl_report_type rt
             ORDER BY rt.active DESC, rt.display_order, rt.name
        """)
        types = cur.fetchall()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    return render_template('stl_manager/stl_manage_report_types.html',
        uid=uid, types=types, manager_ok=is_stl_manager(uid))


# ═════════════════════════════════════════════════════════════════
# 開示記事：事務局による作成・編集・公開
# ═════════════════════════════════════════════════════════════════
@stl_bp.route('/manage/disclosures')
@require_staff
def manage_disclosures():
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT d.*, u.full_name AS author_name
              FROM stl_disclosure d
              LEFT JOIN users u ON u.id = d.created_by
             ORDER BY COALESCE(d.published_at, d.created_at) DESC
        """)
        rows = [enrich_disclosure(d) for d in cur.fetchall()]
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        rows = []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return render_template('stl_manager/stl_manage_disclosures.html',
        uid=uid, disclosures=rows, manager_ok=is_stl_manager(uid))


def _load_all_groups():
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, name FROM user_groups ORDER BY name")
        return cur.fetchall()
    except Exception:
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


@stl_bp.route('/manage/disclosure/new', methods=['GET', 'POST'])
@require_staff
def disclosure_new():
    uid = current_uid()
    src = request.args.get('from_report', '')

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        body = request.form.get('body', '').strip()
        scope = request.form.get('scope', 'group')
        if scope not in SCOPE_META:
            scope = 'group'
        source_report_id = request.form.get('source_report_id', '').strip() or None
        action = request.form.get('action', 'save')   # 'save' | 'publish'
        groups = [g.strip() for g in request.form.getlist('allow_groups') if g.strip()]

        if not title:
            flash('タイトルは必須です', 'error')
            return redirect(url_for('stl.disclosure_new'))

        published_at = get_jst_now() if action == 'publish' else None
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO stl_disclosure
                  (source_report_id, title, body, scope, published_at, created_by, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (source_report_id, title, body, scope, published_at, uid, get_jst_now()))
            new_id = cur.lastrowid
            for g in groups:
                cur.execute("""
                    INSERT IGNORE INTO stl_disclosure_group (disclosure_id, group_name)
                    VALUES (%s,%s)
                """, (new_id, g))
            conn.commit()
            flash('公開しました' if published_at else '下書きを保存しました', 'success')
            return redirect(url_for('stl.manage_disclosures'))
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    # GET — 元レポートからのコピー初期値
    prefill = {'title': '', 'body': ''}
    source_report_id = ''
    if src:
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur = conn.cursor(dictionary=True)
            cur.execute("""
                SELECT r.id, r.title, r.body, t.name AS team_name
                  FROM stl_report r LEFT JOIN stl_team t ON t.id=r.team_id
                 WHERE r.id=%s
            """, (src,))
            row = cur.fetchone()
            if row:
                prefill['title'] = f"【{row['team_name']}】{row['title']}"
                prefill['body'] = row['body'] or ''
                source_report_id = row['id']
        except Exception:
            pass
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    return render_template('stl_manager/stl_disclosure_form.html',
        mode='new', disclosure=None, prefill=prefill,
        source_report_id=source_report_id,
        scope_meta=SCOPE_META, all_groups=_load_all_groups(),
        allow_groups=set())


@stl_bp.route('/manage/disclosure/<int:did>/edit', methods=['GET', 'POST'])
@require_staff
def disclosure_edit(did):
    uid = current_uid()

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        body = request.form.get('body', '').strip()
        scope = request.form.get('scope', 'group')
        if scope not in SCOPE_META:
            scope = 'group'
        action = request.form.get('action', 'save')
        groups = [g.strip() for g in request.form.getlist('allow_groups') if g.strip()]

        if not title:
            flash('タイトルは必須です', 'error')
            return redirect(url_for('stl.disclosure_edit', did=did))

        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur = conn.cursor()
            if action == 'publish':
                cur.execute("""
                    UPDATE stl_disclosure SET title=%s, body=%s, scope=%s,
                      published_at=COALESCE(published_at,%s), updated_at=%s
                     WHERE id=%s
                """, (title, body, scope, get_jst_now(), get_jst_now(), did))
            elif action == 'unpublish':
                cur.execute("""
                    UPDATE stl_disclosure SET title=%s, body=%s, scope=%s,
                      published_at=NULL, updated_at=%s
                     WHERE id=%s
                """, (title, body, scope, get_jst_now(), did))
            else:
                cur.execute("""
                    UPDATE stl_disclosure SET title=%s, body=%s, scope=%s, updated_at=%s
                     WHERE id=%s
                """, (title, body, scope, get_jst_now(), did))
            # 許可グループ入れ替え
            cur.execute("DELETE FROM stl_disclosure_group WHERE disclosure_id=%s", (did,))
            for g in groups:
                cur.execute("""
                    INSERT IGNORE INTO stl_disclosure_group (disclosure_id, group_name)
                    VALUES (%s,%s)
                """, (did, g))
            conn.commit()
            flash('保存しました', 'success')
            return redirect(url_for('stl.manage_disclosures'))
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    # GET
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM stl_disclosure WHERE id=%s", (did,))
        disclosure = cur.fetchone()
        if not disclosure:
            abort(404)
        cur.execute("SELECT group_name FROM stl_disclosure_group WHERE disclosure_id=%s", (did,))
        allow_groups = {r['group_name'] for r in cur.fetchall()}
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    enrich_disclosure(disclosure)
    return render_template('stl_manager/stl_disclosure_form.html',
        mode='edit', disclosure=disclosure,
        prefill={'title': disclosure['title'], 'body': disclosure['body'] or ''},
        source_report_id=disclosure.get('source_report_id') or '',
        scope_meta=SCOPE_META, all_groups=_load_all_groups(),
        allow_groups=allow_groups)


@stl_bp.route('/manage/disclosure/<int:did>/delete', methods=['POST'])
@require_staff
def disclosure_delete(did):
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor()
        cur.execute("DELETE FROM stl_disclosure WHERE id=%s", (did,))
        conn.commit()
        flash('開示記事を削除しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('stl.manage_disclosures'))


# ═════════════════════════════════════════════════════════════════
# 開示記事：閲覧（一覧・詳細）
# ═════════════════════════════════════════════════════════════════
def _can_view_disclosure(d, uid, allow_groups):
    """記事 d を uid が閲覧できるか。scope OR 許可グループ所属。"""
    if is_admin_user() or is_staff(uid):
        return True
    if d['published_at'] is None:
        return False
    scope = d['scope']
    if scope == 'public':
        return True
    if scope == 'internal':
        if session.get('user_category') == 'regular':
            return True
    if scope == 'group':
        # STL 関係者（団体メンバー or STLグループ view ポリシー）を許可
        if uid and user_team_roles(uid):
            return True
    # 追加許可グループ
    if uid and allow_groups:
        if set(user_group_names(uid)) & set(allow_groups):
            return True
    return False


@stl_bp.route('/disclosures')
def disclosures():
    _log_access('disclosures')
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT * FROM stl_disclosure
             WHERE published_at IS NOT NULL
             ORDER BY published_at DESC
        """)
        rows = cur.fetchall()
        # 許可グループをまとめて取得
        gmap = {}
        if rows:
            ids = [r['id'] for r in rows]
            fmt = ','.join(['%s'] * len(ids))
            cur.execute(f"""
                SELECT disclosure_id, group_name FROM stl_disclosure_group
                 WHERE disclosure_id IN ({fmt})
            """, ids)
            for g in cur.fetchall():
                gmap.setdefault(g['disclosure_id'], []).append(g['group_name'])
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        rows, gmap = [], {}
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    visible = []
    for d in rows:
        if _can_view_disclosure(d, uid, gmap.get(d['id'], [])):
            visible.append(enrich_disclosure(d))

    return render_template('stl_manager/stl_disclosures.html',
        uid=uid, disclosures=visible, staff_ok=is_staff(uid))


@stl_bp.route('/disclosure/<int:did>')
def disclosure_detail(did):
    _log_access('disclosure_detail', ref_disclosure_id=did)
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM stl_disclosure WHERE id=%s", (did,))
        d = cur.fetchone()
        if not d:
            abort(404)
        cur.execute("SELECT group_name FROM stl_disclosure_group WHERE disclosure_id=%s", (did,))
        allow_groups = [r['group_name'] for r in cur.fetchall()]
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not _can_view_disclosure(d, uid, allow_groups):
        abort(403)
    enrich_disclosure(d)

    body_html = ''
    if d.get('body'):
        try:
            body_html = process_markdown_for_preview(d['body'])
        except Exception:
            body_html = d['body']

    return render_template('stl_manager/stl_disclosure_detail.html',
        uid=uid, d=d, body_html=body_html, staff_ok=is_staff(uid))


# ═════════════════════════════════════════════════════════════════
# 設定（view ポリシー / 事務局 staff / 締め日）— STL管理者専用
# ═════════════════════════════════════════════════════════════════
@stl_bp.route('/settings', methods=['GET', 'POST'])
@require_manager
def settings():
    if request.method == 'POST':
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur = conn.cursor(dictionary=True)

            # ─ view ポリシー ─
            view_policy = request.form.get('view_policy', DEFAULT_VIEW_POLICY).strip()
            if view_policy not in VALID_VIEW_POLICIES:
                view_policy = DEFAULT_VIEW_POLICY
            cur.execute("DELETE FROM stl_access_settings WHERE access_type='view'")
            cur.execute("""
                INSERT IGNORE INTO stl_access_settings (access_type, group_name)
                VALUES (%s,%s)
            """, ('view', _policy_marker(view_policy)))
            if view_policy == 'group':
                for gname in request.form.getlist('view_groups'):
                    gname = (gname or '').strip()
                    if gname and not _is_policy_marker(gname):
                        cur.execute("""
                            INSERT IGNORE INTO stl_access_settings (access_type, group_name)
                            VALUES (%s,%s)
                        """, ('view', gname))

            # ─ staff（事務局）ユーザ指定 ─
            uid_self = current_uid()
            selected = [s.strip() for s in request.form.getlist('staff_users') if s.strip()]
            try:
                selected_ids = set(int(s) for s in selected)
            except ValueError:
                selected_ids = set()
            cur.execute("SELECT id, user_id, active FROM stl_staff")
            current = {r['user_id']: r for r in cur.fetchall()}
            for sid in selected_ids - set(current.keys()):
                cur.execute("""
                    INSERT INTO stl_staff (user_id, registered_by, active, registered_at)
                    VALUES (%s,%s,1,%s)
                """, (sid, uid_self, get_jst_now()))
            for sid, rec in current.items():
                want = 1 if sid in selected_ids else 0
                if rec['active'] != want:
                    cur.execute("UPDATE stl_staff SET active=%s WHERE id=%s", (want, rec['id']))

            conn.commit()

            # ─ 締め日（1〜31） ─
            cd = request.form.get('closing_day', '25').strip()
            try:
                cd_int = min(31, max(1, int(cd)))
            except ValueError:
                cd_int = 25
            set_setting('closing_day', str(cd_int))

            flash('設定を保存しました', 'success')
            return redirect(url_for('stl.settings'))
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    # GET
    rows, all_groups, all_users = [], [], []
    current_staff_ids = set()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT access_type, group_name FROM stl_access_settings
             WHERE access_type='view' ORDER BY group_name
        """)
        rows = cur.fetchall()
        cur.execute("SELECT id, name FROM user_groups ORDER BY name")
        all_groups = cur.fetchall()
        cur.execute("SELECT id, full_name, email, category FROM users ORDER BY full_name, id")
        all_users = cur.fetchall()
        cur.execute("SELECT user_id FROM stl_staff WHERE active=1")
        current_staff_ids = {r['user_id'] for r in cur.fetchall()}
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    view_policy = DEFAULT_VIEW_POLICY
    view_groups = set()
    for r in rows:
        if _is_policy_marker(r['group_name']):
            p = r['group_name'][len(VIEW_POLICY_PREFIX):-len(VIEW_POLICY_SUFFIX)]
            if p in VALID_VIEW_POLICIES:
                view_policy = p
        else:
            view_groups.add(r['group_name'])

    return render_template('stl_manager/stl_settings.html',
        view_policy=view_policy, view_groups=view_groups,
        valid_view_policies=VALID_VIEW_POLICIES,
        all_groups=all_groups, all_users=all_users,
        current_staff_ids=current_staff_ids,
        closing_day=get_closing_day(),
        manager_group=MANAGER_GROUP, is_admin=is_admin_user())


# ═════════════════════════════════════════════════════════════════
# アクセスログ（STL管理者専用）
# ═════════════════════════════════════════════════════════════════
@stl_bp.route('/admin/access_log')
@require_manager
def access_log():
    page = max(1, int(request.args.get('page', 1)))
    per_page = 50
    offset = (page - 1) * per_page
    ep_filter = request.args.get('endpoint', '')

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(DISTINCT DATE(accessed_at)) AS active_days,
                   COUNT(DISTINCT user_id) AS unique_users,
                   SUM(user_id IS NULL) AS anon_count,
                   SUM(user_id IS NOT NULL) AS login_count
              FROM stl_access_logs
        """)
        stats = cur.fetchone()
        cur.execute("""
            SELECT endpoint, COUNT(*) AS cnt FROM stl_access_logs
             GROUP BY endpoint ORDER BY cnt DESC
        """)
        by_endpoint = cur.fetchall()
        cur.execute("""
            SELECT DATE(accessed_at) AS day, COUNT(*) AS cnt FROM stl_access_logs
             WHERE accessed_at >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)
             GROUP BY day ORDER BY day
        """)
        daily = cur.fetchall()

        where = "WHERE endpoint=%s" if ep_filter else ""
        p_count = (ep_filter,) if ep_filter else ()
        cur.execute(f"SELECT COUNT(*) AS n FROM stl_access_logs {where}", p_count)
        total_rows = cur.fetchone()['n']
        p_list = (ep_filter, per_page, offset) if ep_filter else (per_page, offset)
        cur.execute(f"""
            SELECT l.*, u.full_name AS user_name
              FROM stl_access_logs l
              LEFT JOIN users u ON u.id = l.user_id
             {where}
             ORDER BY l.accessed_at DESC
             LIMIT %s OFFSET %s
        """, p_list)
        logs = cur.fetchall()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        stats, by_endpoint, daily, logs, total_rows = {}, [], [], [], 0
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    total_pages = max(1, (total_rows + per_page - 1) // per_page)
    return render_template('stl_manager/stl_access_log.html',
        stats=stats, by_endpoint=by_endpoint, daily=daily, logs=logs,
        page=page, total_pages=total_pages, ep_filter=ep_filter,
        endpoints=['dashboard', 'my_teams', 'team_reports', 'report_detail',
                   'disclosures', 'disclosure_detail'])

@stl_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJINダッシュボードに戻る"""
    return redirect_to_dashboard()


# 旧エンドポイント名 stl.dashboard でも同じトップを開けるようにしておく（ブックマーク・旧ランチャ用）
stl_bp.add_url_rule('/', endpoint='dashboard', view_func=index)
