"""Flask application for the 500-image TrueSkill pairwise survey."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from statistics import median

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError

from config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    ASSIGNMENT_VERSION,
    CONSENT_VERSION,
    DIMENSION_CONFIG_VERSION,
    EXPECTED_DIMENSION_COUNT,
    EXPECTED_IMAGE_COUNT,
    EXPECTED_PAIR_COUNT,
    EXPECTED_PAIRS_PER_ATTEMPT,
    EXPECTED_SLOT_COUNT,
    SECRET_KEY,
    SLOT_RELEASE_MIN_INACTIVE_MINUTES,
    SURVEY_DIMENSIONS,
    SURVEY_ESTIMATED_TIME,
    SURVEY_TITLE,
    SURVEY_VERSION,
    VALID_CHOICES,
)
from database import SessionLocal
from models import (
    ImageMaster,
    PairAssignment,
    SurveyAttempt,
    SurveyConfig,
    SurveyEventLog,
    SurveyResponse,
    SurveySlot,
)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_client_datetime(value, *, field_name="客户端时间"):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}缺失")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name}格式错误") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_optional_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def hash_ip_address(raw_ip):
    if not raw_ip:
        return None
    payload = f"{SECRET_KEY}|{raw_ip}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def get_request_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.remote_addr or ""


app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=256 * 1024,
)


@app.before_request
def open_database_session():
    g.db = SessionLocal()


@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path in {"/", "/survey", "/get_current_question"}:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.teardown_request
def close_database_session(exception=None):
    db = g.pop("db", None)
    if db is not None:
        if exception is not None:
            db.rollback()
        db.close()


def get_db():
    return g.db


def get_session_attempt(db, *, for_update=False):
    attempt_id = session.get("attempt_id")
    participant_id = session.get("participant_id")
    participant_slot = session.get("participant_slot")
    session_id = session.get("session_id")
    if not all([attempt_id, participant_id, participant_slot, session_id]):
        return None

    stmt = select(SurveyAttempt).where(
        SurveyAttempt.attempt_id == attempt_id,
        SurveyAttempt.participant_id == participant_id,
        SurveyAttempt.participant_slot == participant_slot,
        SurveyAttempt.session_id == session_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def restore_browser_session(attempt):
    session.clear()
    session.permanent = True
    session["attempt_id"] = attempt.attempt_id
    session["participant_id"] = attempt.participant_id
    session["participant_slot"] = attempt.participant_slot
    session["session_id"] = attempt.session_id


def add_event(
    db,
    *,
    attempt=None,
    event_type,
    pair=None,
    event_data=None,
    client_time=None,
):
    db.add(
        SurveyEventLog(
            attempt_id=attempt.attempt_id if attempt else None,
            participant_id=attempt.participant_id if attempt else None,
            participant_slot=attempt.participant_slot if attempt else None,
            session_id=attempt.session_id if attempt else None,
            pair_id=pair.pair_id if pair else None,
            order_in_participant=pair.order_in_participant if pair else None,
            event_type=event_type,
            client_time=client_time,
            server_time=utcnow(),
            event_data=(
                json.dumps(event_data, ensure_ascii=False, default=str)
                if event_data is not None
                else None
            ),
        )
    )


def profile_form_token():
    token = secrets.token_urlsafe(32)
    session["profile_form_token"] = token
    return token


def validate_profile_form(form):
    if form.get("consent") != "yes":
        raise ValueError("请阅读参与说明并勾选知情同意")

    submitted_token = (form.get("profile_form_token") or "").strip()
    session_token = session.get("profile_form_token", "")
    if not submitted_token or not secrets.compare_digest(submitted_token, session_token):
        raise ValueError("表单令牌已失效，请刷新首页后重新填写")

    allowed_values = {
        "gender": {"male", "female", "other_prefer_not"},
        "age_group": {"18_44", "45_59", "60plus"},
        "current_residence": {"chengdu", "chongqing", "other"},
        "chengdu_familiarity": {"1", "2", "3", "4", "5"},
        "chongqing_familiarity": {"1", "2", "3", "4", "5"},
        "professional_background": {"related", "unrelated"},
    }
    labels = {
        "gender": "性别",
        "age_group": "年龄段",
        "current_residence": "常住地",
        "chengdu_familiarity": "成都熟悉度",
        "chongqing_familiarity": "重庆熟悉度",
        "professional_background": "专业背景",
    }
    for field, allowed in allowed_values.items():
        value = form.get(field)
        if value not in allowed:
            raise ValueError(f"请正确填写：{labels[field]}")

    travel_fields = [
        "travel_walk",
        "travel_bike_ebike",
        "travel_public_transit",
        "travel_private_car",
        "travel_taxi_ridehailing",
        "travel_other",
    ]
    if not any(form.get(field) for field in travel_fields):
        raise ValueError("日常主要出行方式至少选择一项")
    if form.get("travel_other") and not (form.get("travel_other_text") or "").strip():
        raise ValueError("选择“其他”出行方式后，请补充具体说明")

    return submitted_token


def validate_choices(choices):
    if not isinstance(choices, dict):
        raise ValueError("回答数据格式错误")
    dimension_keys = [item["key"] for item in SURVEY_DIMENSIONS]
    missing = [key for key in dimension_keys if key not in choices]
    if missing:
        raise ValueError("存在未回答指标：" + "、".join(missing))
    extra = [key for key in choices if key not in dimension_keys]
    if extra:
        raise ValueError("存在未配置指标：" + "、".join(extra))
    illegal = {key: value for key, value in choices.items() if value not in VALID_CHOICES}
    if illegal:
        raise ValueError(f"存在非法选项：{illegal}")


def require_admin(view_function):
    @wraps(view_function)
    def wrapped(*args, **kwargs):
        authorization = request.authorization
        supplied_username = authorization.username if authorization else ""
        supplied_password = authorization.password if authorization else ""
        username_ok = secrets.compare_digest(supplied_username or "", ADMIN_USERNAME)
        password_ok = secrets.compare_digest(supplied_password or "", ADMIN_PASSWORD)
        if not (username_ok and password_ok):
            return Response(
                "需要管理员Basic Authentication认证",
                401,
                {"WWW-Authenticate": 'Basic realm="Survey Admin", charset="UTF-8"'},
            )
        return view_function(*args, **kwargs)

    return wrapped


def render_index_error(message, status=400):
    token = profile_form_token()
    return (
        render_template(
            "index.html",
            error_message=message,
            profile_form_token=token,
            survey_title=SURVEY_TITLE,
            estimated_time=SURVEY_ESTIMATED_TIME,
        ),
        status,
    )


@app.route("/", methods=["GET", "POST"])
def index():
    db = get_db()
    existing_attempt = get_session_attempt(db)
    if existing_attempt is not None and existing_attempt.completion_status == "in_progress":
        return redirect(url_for("survey"))

    if request.method == "GET":
        return render_template(
            "index.html",
            profile_form_token=profile_form_token(),
            survey_title=SURVEY_TITLE,
            estimated_time=SURVEY_ESTIMATED_TIME,
        )

    submitted_token = None
    try:
        submitted_token = validate_profile_form(request.form)
        now = utcnow()

        slot = db.execute(
            select(SurveySlot)
            .where(
                SurveySlot.assignment_version == ASSIGNMENT_VERSION,
                SurveySlot.slot_status == "available",
            )
            .order_by(SurveySlot.participant_slot)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        if slot is None:
            db.rollback()
            return render_index_error("当前没有可分配的问卷槽位，感谢您的关注。", 503)

        participant_id = f"U{uuid.uuid4().hex[:10].upper()}"
        attempt_id = str(uuid.uuid4())
        browser_session_id = str(uuid.uuid4())
        previous_attempt_count = db.scalar(
            select(func.count()).select_from(SurveyAttempt).where(
                SurveyAttempt.assignment_version == ASSIGNMENT_VERSION,
                SurveyAttempt.participant_slot == slot.participant_slot,
            )
        ) or 0

        attempt = SurveyAttempt(
            attempt_id=attempt_id,
            participant_id=participant_id,
            participant_slot=slot.participant_slot,
            session_id=browser_session_id,
            profile_submission_token=submitted_token,
            survey_version=SURVEY_VERSION,
            assignment_version=ASSIGNMENT_VERSION,
            dimension_config_version=DIMENSION_CONFIG_VERSION,
            attempt_number_for_slot=int(previous_attempt_count) + 1,
            completion_status="in_progress",
            started_at=now,
            last_activity_at=now,
            current_order=0,
            answered_pair_count=0,
            consent_given=True,
            consent_version=CONSENT_VERSION,
            consent_at=now,
            is_valid=False,
            gender=request.form.get("gender"),
            age_group=request.form.get("age_group"),
            current_residence=request.form.get("current_residence"),
            chengdu_familiarity=request.form.get("chengdu_familiarity"),
            chongqing_familiarity=request.form.get("chongqing_familiarity"),
            professional_background=request.form.get("professional_background"),
            travel_walk=bool(request.form.get("travel_walk")),
            travel_bike_ebike=bool(request.form.get("travel_bike_ebike")),
            travel_public_transit=bool(request.form.get("travel_public_transit")),
            travel_private_car=bool(request.form.get("travel_private_car")),
            travel_taxi_ridehailing=bool(request.form.get("travel_taxi_ridehailing")),
            travel_other=bool(request.form.get("travel_other")),
            travel_other_text=(request.form.get("travel_other_text") or "").strip(),
            device_type=(request.form.get("device_type") or "")[:50],
            browser_name=(request.form.get("browser_name") or "")[:100],
            operating_system=(request.form.get("operating_system") or "")[:100],
            screen_width=parse_optional_int(request.form.get("screen_width")),
            screen_height=parse_optional_int(request.form.get("screen_height")),
            language=(request.form.get("language") or request.headers.get("Accept-Language", ""))[:100],
            timezone=(request.form.get("timezone") or "")[:100],
            user_agent=request.headers.get("User-Agent", "")[:1000],
            ip_hash=hash_ip_address(get_request_ip()),
            created_at=now,
            updated_at=now,
        )

        slot.slot_status = "in_progress"
        slot.active_attempt_id = attempt_id
        slot.completed_attempt_id = None
        slot.claimed_at = now
        slot.last_activity_at = now
        slot.completed_at = None
        slot.expired_at = None
        slot.updated_at = now

        db.add(attempt)
        add_event(
            db,
            attempt=attempt,
            event_type="slot_claimed",
            event_data={"user_agent": attempt.user_agent, "ip_hash": attempt.ip_hash},
        )
        db.commit()
        restore_browser_session(attempt)
        session.pop("profile_form_token", None)
        return redirect(url_for("survey"))

    except IntegrityError:
        db.rollback()
        # Two nearly simultaneous POSTs carry the same client-side session token.
        # The unique token lets the second request resume the first attempt rather
        # than consuming a second slot.
        if submitted_token:
            existing = db.execute(
                select(SurveyAttempt).where(
                    SurveyAttempt.profile_submission_token == submitted_token,
                    SurveyAttempt.assignment_version == ASSIGNMENT_VERSION,
                )
            ).scalar_one_or_none()
            if existing is not None:
                restore_browser_session(existing)
                return redirect(url_for("survey"))
        app.logger.exception("首页重复提交或数据库唯一约束冲突")
        return render_index_error("检测到重复提交，请刷新页面后继续。", 409)
    except ValueError as error:
        db.rollback()
        return render_index_error(str(error), 400)
    except Exception:
        db.rollback()
        app.logger.exception("领取问卷槽位失败")
        return render_index_error("系统暂时无法分配问卷，请稍后重试。", 500)


@app.route("/survey")
def survey():
    db = get_db()
    attempt = get_session_attempt(db)
    if attempt is None:
        session.clear()
        return redirect(url_for("index"))
    if attempt.completion_status == "completed":
        return redirect(url_for("thank_you"))
    if attempt.completion_status != "in_progress":
        session.clear()
        return redirect(url_for("index"))

    return render_template(
        "survey.html",
        survey_dimensions=SURVEY_DIMENSIONS,
        expected_dimension_count=EXPECTED_DIMENSION_COUNT,
        estimated_time=SURVEY_ESTIMATED_TIME,
    )


@app.route("/get_current_question")
def get_current_question():
    db = get_db()
    attempt = get_session_attempt(db)
    if attempt is None:
        return jsonify(status="error", message="会话不存在或已过期"), 401
    if attempt.completion_status == "completed":
        return jsonify(status="completed")
    if attempt.completion_status != "in_progress":
        return jsonify(status="error", message="当前问卷状态不可继续"), 409

    next_order = attempt.answered_pair_count + 1
    if next_order > EXPECTED_PAIRS_PER_ATTEMPT:
        return jsonify(status="error", message="问卷进度超出预期，请联系管理员"), 500

    pair = db.execute(
        select(PairAssignment).where(
            PairAssignment.assignment_version == attempt.assignment_version,
            PairAssignment.participant_slot == attempt.participant_slot,
            PairAssignment.order_in_participant == next_order,
        )
    ).scalar_one_or_none()
    if pair is None:
        return jsonify(status="error", message="未找到当前槽位对应的固定题目"), 500

    served_at = utcnow()
    attempt.last_activity_at = served_at
    attempt.updated_at = served_at
    slot = db.execute(
        select(SurveySlot).where(
            SurveySlot.assignment_version == attempt.assignment_version,
            SurveySlot.participant_slot == attempt.participant_slot,
        )
    ).scalar_one_or_none()
    if slot is not None:
        slot.last_activity_at = served_at
        slot.updated_at = served_at

    add_event(db, attempt=attempt, pair=pair, event_type="question_served")
    db.commit()

    return jsonify(
        status="success",
        current_index=next_order - 1,
        total_questions=EXPECTED_PAIRS_PER_ATTEMPT,
        answered_pair_count=attempt.answered_pair_count,
        question_served_at=served_at.isoformat() + "Z",
        question={
            "order": pair.order_in_participant,
            "pair_id": pair.pair_id,
            "left_img_url": pair.left_oss_url,
            "right_img_url": pair.right_oss_url,
        },
    )


@app.route("/submit_response", methods=["POST"])
def submit_response():
    db = get_db()
    payload = request.get_json(silent=True) or {}
    try:
        attempt = get_session_attempt(db, for_update=True)
        if attempt is None:
            raise PermissionError("会话不存在或已过期")
        if attempt.completion_status == "completed":
            return jsonify(status="completed", completed=True)
        if attempt.completion_status != "in_progress":
            raise ValueError("当前问卷状态不可提交")

        submitted_pair_id = str(payload.get("pair_id", "")).strip()
        if not submitted_pair_id:
            raise ValueError("缺少pair_id")
        choices = payload.get("choices")
        validate_choices(choices)

        existing_count = db.scalar(
            select(func.count()).select_from(SurveyResponse).where(
                SurveyResponse.attempt_id == attempt.attempt_id,
                SurveyResponse.pair_id == submitted_pair_id,
            )
        ) or 0
        if existing_count == EXPECTED_DIMENSION_COUNT:
            db.rollback()
            return jsonify(
                status="success",
                duplicate_retry=True,
                completed=attempt.completion_status == "completed",
                answered_pair_count=attempt.answered_pair_count,
            )
        if existing_count != 0:
            raise RuntimeError("检测到当前题存在不完整历史记录，请联系管理员")

        next_order = attempt.answered_pair_count + 1
        expected_pair = db.execute(
            select(PairAssignment).where(
                PairAssignment.assignment_version == attempt.assignment_version,
                PairAssignment.participant_slot == attempt.participant_slot,
                PairAssignment.order_in_participant == next_order,
            )
        ).scalar_one_or_none()
        if expected_pair is None:
            raise RuntimeError("未找到服务器固定题目")
        if submitted_pair_id != expected_pair.pair_id:
            raise ValueError("提交题目与服务器固定配对不一致")

        try:
            submitted_order = int(payload.get("order_in_participant", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("提交题序格式错误") from exc
        if submitted_order != expected_pair.order_in_participant:
            raise ValueError("提交题序与服务器当前进度不一致")
        if payload.get("left_image_loaded") is not True:
            raise ValueError("左侧图片未加载成功")
        if payload.get("right_image_loaded") is not True:
            raise ValueError("右侧图片未加载成功")

        both_images_loaded_at = parse_client_datetime(
            payload.get("both_images_loaded_at"), field_name="两图完成加载时间"
        )
        submit_time_client = parse_client_datetime(
            payload.get("question_submit_time_client"), field_name="客户端提交时间"
        )
        if submit_time_client < both_images_loaded_at:
            raise ValueError("客户端提交时间早于图片完成加载时间")
        if submit_time_client > utcnow() + timedelta(minutes=10):
            raise ValueError("客户端提交时间异常")

        served_event = db.execute(
            select(SurveyEventLog)
            .where(
                SurveyEventLog.attempt_id == attempt.attempt_id,
                SurveyEventLog.pair_id == expected_pair.pair_id,
                SurveyEventLog.event_type == "question_served",
            )
            .order_by(SurveyEventLog.event_id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if served_event is None:
            raise ValueError("未找到当前题展示记录，请刷新页面")

        now = utcnow()
        response_time_sec = max(round((now - served_event.server_time).total_seconds(), 3), 0.0)
        pair_submission_id = str(uuid.uuid4())

        for dimension in SURVEY_DIMENSIONS:
            db.add(
                SurveyResponse(
                    response_id=str(uuid.uuid4()),
                    pair_submission_id=pair_submission_id,
                    attempt_id=attempt.attempt_id,
                    participant_id=attempt.participant_id,
                    participant_slot=attempt.participant_slot,
                    session_id=attempt.session_id,
                    survey_version=attempt.survey_version,
                    assignment_version=attempt.assignment_version,
                    dimension_config_version=attempt.dimension_config_version,
                    pair_id=expected_pair.pair_id,
                    order_in_participant=expected_pair.order_in_participant,
                    dimension_key=dimension["key"],
                    dimension_order=dimension["order"],
                    display_position=dimension["order"],
                    left_qid=expected_pair.left_qid,
                    right_qid=expected_pair.right_qid,
                    left_image_id=expected_pair.left_image_id,
                    right_image_id=expected_pair.right_image_id,
                    choice=choices[dimension["key"]],
                    question_served_at=served_event.server_time,
                    left_image_loaded=True,
                    right_image_loaded=True,
                    both_images_loaded_at=both_images_loaded_at,
                    question_submit_time_client=submit_time_client,
                    server_received_at=now,
                    server_saved_at=now,
                    response_time_sec=response_time_sec,
                    is_duplicate_retry=False,
                    response_status="saved",
                    created_at=now,
                )
            )

        attempt.answered_pair_count += 1
        attempt.current_order = attempt.answered_pair_count
        attempt.last_activity_at = now
        attempt.updated_at = now

        slot = db.execute(
            select(SurveySlot).where(
                SurveySlot.assignment_version == attempt.assignment_version,
                SurveySlot.participant_slot == attempt.participant_slot,
            ).with_for_update()
        ).scalar_one()
        slot.last_activity_at = now
        slot.updated_at = now

        add_event(
            db,
            attempt=attempt,
            pair=expected_pair,
            event_type="response_saved",
            event_data={
                "pair_submission_id": pair_submission_id,
                "response_time_sec": response_time_sec,
                "answered_dimension_count": EXPECTED_DIMENSION_COUNT,
            },
            client_time=submit_time_client,
        )

        completed = attempt.answered_pair_count == EXPECTED_PAIRS_PER_ATTEMPT
        if completed:
            attempt.completion_status = "completed"
            attempt.completed_at = now
            # Here valid means structurally complete. Behavioural quality review
            # remains an offline step and is exported separately.
            attempt.is_valid = True
            attempt.invalid_reason = None
            slot.slot_status = "completed"
            slot.active_attempt_id = None
            slot.completed_attempt_id = attempt.attempt_id
            slot.completed_at = now
            add_event(db, attempt=attempt, event_type="survey_completed")

        db.commit()
        return jsonify(
            status="success",
            completed=completed,
            answered_pair_count=attempt.answered_pair_count,
        )

    except PermissionError as error:
        db.rollback()
        return jsonify(status="error", message=str(error)), 401
    except (ValueError, RuntimeError) as error:
        db.rollback()
        app.logger.warning("回答校验失败：%s", error)
        return jsonify(status="error", message=str(error)), 400
    except IntegrityError:
        db.rollback()
        app.logger.exception("数据库唯一约束拦截重复提交")
        return jsonify(status="error", message="检测到重复提交，请刷新页面继续"), 409
    except Exception:
        db.rollback()
        app.logger.exception("保存答题记录失败")
        return jsonify(status="error", message="服务器保存失败，请稍后重试"), 500


@app.route("/thank_you")
def thank_you():
    db = get_db()
    attempt = get_session_attempt(db)
    if attempt is None:
        return redirect(url_for("index"))
    if attempt.completion_status != "completed":
        return redirect(url_for("survey"))
    participant_slot = attempt.participant_slot
    session.clear()
    return f"""
    <div style="text-align:center;margin-top:100px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      <h1 style="color:#34c759;font-size:32px;">问卷全部完成</h1>
      <p style="color:#666;font-size:16px;">感谢您的参与，您的完整作答已安全保存。</p>
      <p style="color:#999;font-size:13px;">问卷编号：{participant_slot}</p>
    </div>
    """


def admin_csrf_token():
    token = session.get("admin_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["admin_csrf_token"] = token
    return token


@app.route("/admin")
@require_admin
def admin_dashboard():
    db = get_db()
    slot_counts = dict(
        db.execute(
            select(SurveySlot.slot_status, func.count(SurveySlot.id))
            .where(SurveySlot.assignment_version == ASSIGNMENT_VERSION)
            .group_by(SurveySlot.slot_status)
        ).all()
    )
    attempt_count = db.scalar(
        select(func.count()).select_from(SurveyAttempt).where(
            SurveyAttempt.assignment_version == ASSIGNMENT_VERSION
        )
    ) or 0
    response_count = db.scalar(
        select(func.count()).select_from(SurveyResponse).where(
            SurveyResponse.assignment_version == ASSIGNMENT_VERSION
        )
    ) or 0
    in_progress = db.execute(
        select(SurveySlot)
        .where(
            SurveySlot.assignment_version == ASSIGNMENT_VERSION,
            SurveySlot.slot_status == "in_progress",
        )
        .order_by(SurveySlot.last_activity_at.asc())
    ).scalars().all()

    rows = "".join(
        f"<tr><td>{slot.participant_slot}</td><td>{slot.active_attempt_id or ''}</td>"
        f"<td>{slot.last_activity_at or ''}</td><td>{slot.release_count}</td></tr>"
        for slot in in_progress
    ) or "<tr><td colspan='4'>当前无进行中槽位</td></tr>"

    return f"""
    <div style="font-family:Arial,sans-serif;max-width:1050px;margin:40px auto;line-height:1.7">
      <h1>街景感知问卷管理后台（500张正式版）</h1>
      <p>当前版本：{ASSIGNMENT_VERSION}</p>
      <p>可用槽位：{slot_counts.get('available', 0)}；进行中：{slot_counts.get('in_progress', 0)}；已完成：{slot_counts.get('completed', 0)}</p>
      <p>尝试总数：{attempt_count}；回答总行数：{response_count}</p>
      <hr>
      <h2>研究归档导出</h2>
      <p><a href="{url_for('export_slots')}">survey_slots.csv</a></p>
      <p><a href="{url_for('export_attempts')}">survey_attempts.csv</a></p>
      <p><a href="{url_for('export_responses_raw')}">survey_responses_raw.csv</a></p>
      <p><a href="{url_for('export_responses_valid')}">survey_responses_valid.csv</a></p>
      <p><a href="{url_for('export_survey_config')}">survey_config.csv</a></p>
      <p><a href="{url_for('export_pair_assignments')}">pair_assignments.csv</a></p>
      <p><a href="{url_for('export_image_master')}">image_master_500_final.csv</a></p>
      <p><a href="{url_for('export_event_logs')}">survey_event_logs.csv</a></p>
      <p><a href="{url_for('export_quality_report')}">survey_quality_report.csv</a></p>
      <p><a href="{url_for('export_archive')}"><strong>下载完整CSV归档ZIP</strong></a></p>
      <hr>
      <h2>受控释放中断槽位</h2>
      <p>默认仅允许释放已无活动至少{SLOT_RELEASE_MIN_INACTIVE_MINUTES}分钟的进行中槽位。原始回答不会删除。</p>
      <form method="post" action="{url_for('release_slot')}" style="display:grid;gap:8px;max-width:650px">
        <input type="hidden" name="admin_csrf_token" value="{admin_csrf_token()}">
        <input name="participant_slot" placeholder="例如 P_SLOT_001" required>
        <input name="release_reason" placeholder="释放原因（必填）" required>
        <label><input type="checkbox" name="force_release" value="yes"> 强制释放（未达到最短无活动时间时使用）</label>
        <button type="submit">确认释放</button>
      </form>
      <h3>进行中槽位</h3>
      <table border="1" cellpadding="6" cellspacing="0"><tr><th>槽位</th><th>attempt</th><th>最后活动</th><th>已释放次数</th></tr>{rows}</table>
    </div>
    """


@app.route("/admin/release_slot", methods=["POST"])
@require_admin
def release_slot():
    db = get_db()
    supplied_token = request.form.get("admin_csrf_token", "")
    expected_token = session.get("admin_csrf_token", "")
    if not supplied_token or not secrets.compare_digest(supplied_token, expected_token):
        return Response("管理员表单令牌无效", 400)

    participant_slot = (request.form.get("participant_slot") or "").strip().upper()
    reason = (request.form.get("release_reason") or "").strip()
    force_release = request.form.get("force_release") == "yes"
    if not participant_slot or not reason:
        return Response("槽位和释放原因均为必填", 400)

    try:
        slot = db.execute(
            select(SurveySlot).where(
                SurveySlot.assignment_version == ASSIGNMENT_VERSION,
                SurveySlot.participant_slot == participant_slot,
            ).with_for_update()
        ).scalar_one_or_none()
        if slot is None:
            raise ValueError("未找到该槽位")
        if slot.slot_status != "in_progress" or not slot.active_attempt_id:
            raise ValueError("该槽位当前不是进行中状态")

        attempt = db.execute(
            select(SurveyAttempt).where(
                SurveyAttempt.attempt_id == slot.active_attempt_id
            ).with_for_update()
        ).scalar_one_or_none()
        if attempt is None:
            raise ValueError("槽位对应attempt不存在")
        if attempt.completion_status != "in_progress":
            raise ValueError("attempt当前不是进行中状态")

        now = utcnow()
        last_activity = attempt.last_activity_at or attempt.started_at
        inactive_minutes = (now - last_activity).total_seconds() / 60
        if inactive_minutes < SLOT_RELEASE_MIN_INACTIVE_MINUTES and not force_release:
            raise ValueError(
                f"该槽位仅无活动{inactive_minutes:.1f}分钟；未达到{SLOT_RELEASE_MIN_INACTIVE_MINUTES}分钟。"
            )

        attempt.completion_status = "expired"
        attempt.expired_at = now
        attempt.is_valid = False
        attempt.invalid_reason = f"admin_released: {reason}"
        attempt.admin_note = reason
        attempt.updated_at = now

        slot.slot_status = "available"
        slot.active_attempt_id = None
        slot.claimed_at = None
        slot.last_activity_at = None
        slot.expired_at = now
        slot.release_count = int(slot.release_count or 0) + 1
        slot.release_reason = reason
        slot.updated_at = now

        add_event(
            db,
            attempt=attempt,
            event_type="slot_released_by_admin",
            event_data={
                "reason": reason,
                "inactive_minutes": round(inactive_minutes, 2),
                "force_release": force_release,
            },
        )
        db.commit()
        return redirect(url_for("admin_dashboard"))
    except ValueError as error:
        db.rollback()
        return Response(str(error), 400)
    except Exception:
        db.rollback()
        app.logger.exception("管理员释放槽位失败")
        return Response("释放槽位失败", 500)


def csv_bytes(headers, rows):
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def csv_response(filename, headers, rows):
    response = app.response_class(csv_bytes(headers, rows), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def serialize_records(records, headers):
    return [[getattr(item, column) for column in headers] for item in records]


def slots_export_data(db):
    headers = [
        "assignment_version", "participant_slot", "slot_status",
        "active_attempt_id", "completed_attempt_id", "claimed_at",
        "last_activity_at", "completed_at", "expired_at", "release_count",
        "release_reason", "created_at", "updated_at",
    ]
    records = db.execute(
        select(SurveySlot).where(
            SurveySlot.assignment_version == ASSIGNMENT_VERSION
        ).order_by(SurveySlot.participant_slot)
    ).scalars().all()
    return headers, serialize_records(records, headers)


def attempts_export_data(db):
    headers = [
        "attempt_id", "participant_id", "participant_slot", "session_id",
        "survey_version", "assignment_version", "dimension_config_version",
        "attempt_number_for_slot", "completion_status", "started_at",
        "last_activity_at", "completed_at", "expired_at", "current_order",
        "answered_pair_count", "consent_given", "consent_version", "consent_at",
        "is_valid", "invalid_reason", "admin_note", "gender", "age_group",
        "current_residence", "chengdu_familiarity", "chongqing_familiarity",
        "professional_background", "travel_walk", "travel_bike_ebike",
        "travel_public_transit", "travel_private_car", "travel_taxi_ridehailing",
        "travel_other", "travel_other_text", "device_type", "browser_name",
        "operating_system", "screen_width", "screen_height", "language",
        "timezone", "user_agent", "ip_hash", "created_at", "updated_at",
    ]
    records = db.execute(
        select(SurveyAttempt).where(
            SurveyAttempt.assignment_version == ASSIGNMENT_VERSION
        ).order_by(SurveyAttempt.started_at)
    ).scalars().all()
    return headers, serialize_records(records, headers)


def response_records(db, valid_only):
    stmt = select(SurveyResponse).join(
        SurveyAttempt, SurveyAttempt.attempt_id == SurveyResponse.attempt_id
    ).where(SurveyResponse.assignment_version == ASSIGNMENT_VERSION)
    if valid_only:
        stmt = stmt.where(
            SurveyAttempt.completion_status == "completed",
            SurveyAttempt.is_valid.is_(True),
            SurveyAttempt.answered_pair_count == EXPECTED_PAIRS_PER_ATTEMPT,
        )
    return db.execute(
        stmt.order_by(
            SurveyResponse.participant_slot,
            SurveyResponse.order_in_participant,
            SurveyResponse.dimension_order,
        )
    ).scalars().all()


def responses_export_data(db, valid_only):
    headers = [
        "response_id", "pair_submission_id", "attempt_id", "participant_id",
        "participant_slot", "session_id", "survey_version", "assignment_version",
        "dimension_config_version", "pair_id", "order_in_participant",
        "dimension_key", "dimension_order", "display_position", "left_qid",
        "right_qid", "left_image_id", "right_image_id", "choice", "winner_qid",
        "winner_image_id", "question_served_at", "left_image_loaded",
        "right_image_loaded", "both_images_loaded_at", "question_submit_time_client",
        "server_received_at", "server_saved_at", "response_time_sec",
        "is_duplicate_retry", "response_status",
    ]
    rows = []
    for item in response_records(db, valid_only):
        winner_qid = item.left_qid if item.choice == "left" else item.right_qid if item.choice == "right" else None
        winner_image_id = item.left_image_id if item.choice == "left" else item.right_image_id if item.choice == "right" else None
        values = {
            **{column: getattr(item, column) for column in headers if hasattr(item, column)},
            "winner_qid": winner_qid,
            "winner_image_id": winner_image_id,
        }
        rows.append([values.get(column) for column in headers])
    return headers, rows


def config_export_data(db):
    headers = [
        "survey_version", "assignment_version", "dimension_config_version",
        "dimension_key", "dimension_label", "dimension_description",
        "dimension_definition", "dimension_order", "score_direction",
        "high_score_meaning", "is_active", "expected_pairs_per_attempt",
        "expected_dimension_count", "created_at", "activated_at",
    ]
    records = db.execute(
        select(SurveyConfig).where(
            SurveyConfig.dimension_config_version == DIMENSION_CONFIG_VERSION
        ).order_by(SurveyConfig.dimension_order)
    ).scalars().all()
    return headers, serialize_records(records, headers)


def pair_export_data(db):
    headers = [
        "assignment_version", "source_row_id", "pair_id", "participant_slot",
        "order_in_participant", "left_qid", "right_qid", "left_image_id",
        "right_image_id", "left_image_filename", "right_image_filename",
        "left_image_relative_path", "right_image_relative_path", "left_oss_url",
        "right_oss_url", "left_city", "right_city", "left_cluster",
        "right_cluster", "same_city", "same_cluster", "pair_type",
        "left_right_random_seed", "created_at",
    ]
    records = db.execute(
        select(PairAssignment).where(
            PairAssignment.assignment_version == ASSIGNMENT_VERSION
        ).order_by(PairAssignment.participant_slot, PairAssignment.order_in_participant)
    ).scalars().all()
    return headers, serialize_records(records, headers)


def image_export_data(db):
    qids = db.execute(
        select(PairAssignment.left_qid).where(
            PairAssignment.assignment_version == ASSIGNMENT_VERSION
        ).union(
            select(PairAssignment.right_qid).where(
                PairAssignment.assignment_version == ASSIGNMENT_VERSION
            )
        )
    ).scalars().all()
    headers = [
        "qid", "image_id", "city", "longitude", "latitude", "capture_date",
        "capture_year", "year_month", "cluster_id", "image_filename",
        "image_relative_path", "oss_url", "source_master_version",
        "image_sha256_or_oss_etag", "sample_origin", "sample_role", "point_id",
        "road_segment_id", "is_active",
    ]
    records = db.execute(
        select(ImageMaster).where(ImageMaster.qid.in_(qids)).order_by(ImageMaster.qid)
    ).scalars().all()
    return headers, serialize_records(records, headers)


def event_export_data(db):
    headers = [
        "event_id", "attempt_id", "participant_id", "participant_slot",
        "session_id", "pair_id", "order_in_participant", "event_type",
        "client_time", "server_time", "event_data",
    ]
    current_attempt_ids = select(SurveyAttempt.attempt_id).where(
        SurveyAttempt.assignment_version == ASSIGNMENT_VERSION
    )
    records = db.execute(
        select(SurveyEventLog).where(
            SurveyEventLog.attempt_id.in_(current_attempt_ids)
        ).order_by(SurveyEventLog.event_id)
    ).scalars().all()
    return headers, serialize_records(records, headers)


def quality_export_data(db):
    headers = [
        "attempt_id", "participant_id", "participant_slot", "completion_status",
        "answered_pair_count", "response_row_count", "unique_pair_count",
        "median_pair_time_sec", "total_duration_sec", "left_rate", "right_rate",
        "tie_rate", "too_fast_pair_count_lt2s", "late_stage_median_time_sec",
        "quality_flag", "quality_reason",
    ]
    attempts = db.execute(
        select(SurveyAttempt).where(
            SurveyAttempt.assignment_version == ASSIGNMENT_VERSION
        ).order_by(SurveyAttempt.started_at)
    ).scalars().all()
    rows = []
    for attempt in attempts:
        responses = db.execute(
            select(SurveyResponse).where(
                SurveyResponse.attempt_id == attempt.attempt_id
            ).order_by(SurveyResponse.order_in_participant, SurveyResponse.dimension_order)
        ).scalars().all()
        pair_times = {}
        choices = []
        for response in responses:
            pair_times.setdefault(response.pair_id, response.response_time_sec)
            choices.append(response.choice)
        times = list(pair_times.values())
        late_times = [
            pair_times[pair_id]
            for pair_id in {
                response.pair_id for response in responses if response.order_in_participant > 20
            }
            if pair_id in pair_times
        ]
        total_choices = len(choices)
        reasons = []
        if attempt.completion_status == "completed" and len(pair_times) != EXPECTED_PAIRS_PER_ATTEMPT:
            reasons.append("completed_but_pair_count_not_30")
        if len(responses) not in {0, EXPECTED_PAIRS_PER_ATTEMPT * EXPECTED_DIMENSION_COUNT} and attempt.completion_status == "completed":
            reasons.append("completed_but_response_rows_not_240")
        too_fast = sum(1 for value in times if value < 2.0)
        if too_fast >= 5:
            reasons.append("too_many_pairs_under_2_seconds")
        rows.append([
            attempt.attempt_id,
            attempt.participant_id,
            attempt.participant_slot,
            attempt.completion_status,
            attempt.answered_pair_count,
            len(responses),
            len(pair_times),
            median(times) if times else None,
            (attempt.completed_at - attempt.started_at).total_seconds() if attempt.completed_at else None,
            choices.count("left") / total_choices if total_choices else None,
            choices.count("right") / total_choices if total_choices else None,
            choices.count("tie") / total_choices if total_choices else None,
            too_fast,
            median(late_times) if late_times else None,
            "review" if reasons else "structurally_ok",
            ";".join(reasons),
        ])
    return headers, rows


@app.route("/admin/export/slots")
@require_admin
def export_slots():
    return csv_response("survey_slots.csv", *slots_export_data(get_db()))


@app.route("/admin/export/attempts")
@require_admin
def export_attempts():
    return csv_response("survey_attempts.csv", *attempts_export_data(get_db()))


@app.route("/admin/export/responses_raw")
@require_admin
def export_responses_raw():
    return csv_response("survey_responses_raw.csv", *responses_export_data(get_db(), False))


@app.route("/admin/export/responses_valid")
@require_admin
def export_responses_valid():
    return csv_response("survey_responses_valid.csv", *responses_export_data(get_db(), True))


@app.route("/admin/export/survey_config")
@require_admin
def export_survey_config():
    return csv_response("survey_config.csv", *config_export_data(get_db()))


@app.route("/admin/export/pair_assignments")
@require_admin
def export_pair_assignments():
    return csv_response("pair_assignments.csv", *pair_export_data(get_db()))


@app.route("/admin/export/image_master")
@require_admin
def export_image_master():
    return csv_response("image_master_500_final.csv", *image_export_data(get_db()))


@app.route("/admin/export/event_logs")
@require_admin
def export_event_logs():
    return csv_response("survey_event_logs.csv", *event_export_data(get_db()))


@app.route("/admin/export/quality_report")
@require_admin
def export_quality_report():
    return csv_response("survey_quality_report.csv", *quality_export_data(get_db()))


@app.route("/admin/export/archive")
@require_admin
def export_archive():
    db = get_db()
    datasets = {
        "survey_slots.csv": slots_export_data(db),
        "survey_attempts.csv": attempts_export_data(db),
        "survey_responses_raw.csv": responses_export_data(db, False),
        "survey_responses_valid.csv": responses_export_data(db, True),
        "survey_config.csv": config_export_data(db),
        "pair_assignments.csv": pair_export_data(db),
        "image_master_500_final.csv": image_export_data(db),
        "survey_event_logs.csv": event_export_data(db),
        "survey_quality_report.csv": quality_export_data(db),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, (headers, rows) in datasets.items():
            archive.writestr(filename, csv_bytes(headers, rows))
    response = app.response_class(buffer.getvalue(), mimetype="application/zip")
    response.headers["Content-Disposition"] = 'attachment; filename="survey_research_archive.zip"'
    return response


@app.route("/health")
def health():
    db = get_db()
    counts = {
        "survey_config": db.scalar(
            select(func.count()).select_from(SurveyConfig).where(
                SurveyConfig.dimension_config_version == DIMENSION_CONFIG_VERSION
            )
        ) or 0,
        "pair_assignments": db.scalar(
            select(func.count()).select_from(PairAssignment).where(
                PairAssignment.assignment_version == ASSIGNMENT_VERSION
            )
        ) or 0,
        "survey_slots": db.scalar(
            select(func.count()).select_from(SurveySlot).where(
                SurveySlot.assignment_version == ASSIGNMENT_VERSION
            )
        ) or 0,
    }
    qid_count = db.scalar(
        select(func.count(func.distinct(PairAssignment.left_qid))).where(
            PairAssignment.assignment_version == ASSIGNMENT_VERSION
        )
    ) or 0
    right_only = db.scalar(
        select(func.count(func.distinct(PairAssignment.right_qid))).where(
            PairAssignment.assignment_version == ASSIGNMENT_VERSION
        )
    ) or 0
    # Exact 500-image verification uses a union because left/right sets overlap.
    image_count = len(
        set(
            db.execute(
                select(PairAssignment.left_qid).where(
                    PairAssignment.assignment_version == ASSIGNMENT_VERSION
                ).union(
                    select(PairAssignment.right_qid).where(
                        PairAssignment.assignment_version == ASSIGNMENT_VERSION
                    )
                )
            ).scalars().all()
        )
    )
    counts["image_master"] = image_count
    expected = {
        "survey_config": EXPECTED_DIMENSION_COUNT,
        "image_master": EXPECTED_IMAGE_COUNT,
        "pair_assignments": EXPECTED_PAIR_COUNT,
        "survey_slots": EXPECTED_SLOT_COUNT,
    }
    ok = counts == expected
    return jsonify(
        status="ok" if ok else "error",
        survey_version=SURVEY_VERSION,
        assignment_version=ASSIGNMENT_VERSION,
        dimension_config_version=DIMENSION_CONFIG_VERSION,
        counts=counts,
        expected=expected,
    ), 200 if ok else 503


if __name__ == "__main__":
    app.run(debug=False, port=5000)
