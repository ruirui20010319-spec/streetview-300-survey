"""Production configuration for the 500-image TrueSkill survey platform.

The survey dimensions are defined here once and are passed by ``app.py`` to
``survey.html``.  Do not duplicate dimension keys or wording in JavaScript.
"""

from __future__ import annotations

import os
from pathlib import Path


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必需环境变量 {name}")
    return value


DATABASE_URL = _required_env("DATABASE_URL")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Security-sensitive values must never fall back to public defaults.
SECRET_KEY = _required_env("SECRET_KEY")
ADMIN_PASSWORD = _required_env("ADMIN_PASSWORD")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"

OSS_BASE_URL = os.environ.get(
    "OSS_BASE_URL",
    "https://streetview-images.oss-cn-hangzhou.aliyuncs.com",
).strip().rstrip("/")
if not OSS_BASE_URL.startswith(("https://", "http://")):
    raise RuntimeError("OSS_BASE_URL 必须以 http:// 或 https:// 开头")

BASE_DIR = Path(__file__).resolve().parent
TABLE_DIR = BASE_DIR / "tables"
ASSIGNMENT_TABLE_PATH = TABLE_DIR / "questionnaire_pair_assignment_250x30.xlsx"
IMAGE_METADATA_TABLE_PATH = TABLE_DIR / "questionnaire_sample_500_platform_metadata.xlsx"
IMAGE_DETAIL_TABLE_PATH = TABLE_DIR / "questionnaire_sample_500_final_for_survey_id_fixed.xlsx"

SURVEY_VERSION = os.environ.get(
    "SURVEY_VERSION", "survey_500img_9dim_v1"
).strip()
ASSIGNMENT_VERSION = os.environ.get(
    "ASSIGNMENT_VERSION", "assignment_500img_250slot_v1"
).strip()
DIMENSION_CONFIG_VERSION = os.environ.get(
    "DIMENSION_CONFIG_VERSION", "dimensions_500img_fixed_order_9dim_v1"
).strip()
CONSENT_VERSION = os.environ.get(
    "CONSENT_VERSION", "consent_2026_v1"
).strip()

EXPECTED_IMAGE_COUNT = 500
EXPECTED_PAIR_COUNT = 7500
EXPECTED_SLOT_COUNT = 250
EXPECTED_PAIRS_PER_ATTEMPT = 30
EXPECTED_DIMENSION_COUNT = 9

# Minimum inactive duration before an administrator can release a slot without
# explicitly checking the force-release box.
SLOT_RELEASE_MIN_INACTIVE_MINUTES = int(
    os.environ.get("SLOT_RELEASE_MIN_INACTIVE_MINUTES", "60")
)

SURVEY_TITLE = "街景感知学术问卷调查"
SURVEY_ESTIMATED_TIME = "约15—20分钟"

# One fixed, deliberately interleaved display order. Related indicators are
# separated to reduce mechanical same-side responses. The order is frozen for
# the entire formal collection period.
SURVEY_DIMENSIONS = [
    {
        "key": "aesthetic",
        "label": "美观度",
        "description": "哪一侧街景整体更美观、更赏心悦目？",
        "definition": "对街景整体视觉美感与愉悦程度的判断。",
        "order": 1,
        "score_direction": 1,
        "high_score_meaning": "越高表示越美观",
    },
    {
        "key": "disorder",
        "label": "物理失序感",
        "description": "哪一侧街景显得更脏乱、破败或缺乏维护？",
        "definition": "对街景中脏乱、破败和维护不足程度的判断。",
        "order": 2,
        "score_direction": -1,
        "high_score_meaning": "越高表示物理失序越明显",
    },
    {
        "key": "restorativeness",
        "label": "心理恢复感",
        "description": "哪一侧街景更让您感到放松，并更有助于缓解疲劳、恢复精神？",
        "definition": "对环境能否带来放松、减压和精神恢复的判断。",
        "order": 3,
        "score_direction": 1,
        "high_score_meaning": "越高表示心理恢复感越强",
    },
    {
        "key": "sociality",
        "label": "社会交往适宜度",
        "description": "哪一侧街景看起来更适合人们停留、交流和开展日常交往？",
        "definition": "对街道空间是否适合停留、交流和日常交往的判断。",
        "order": 4,
        "score_direction": 1,
        "high_score_meaning": "越高表示越适宜社会交往",
    },
    {
        "key": "naturalness",
        "label": "自然感",
        "description": "哪一侧街景让您感觉自然元素更丰富、自然气息更强？",
        "definition": "对街景中自然元素丰富程度和自然气息强弱的判断。",
        "order": 5,
        "score_direction": 1,
        "high_score_meaning": "越高表示自然感越强",
    },
    {
        "key": "safety",
        "label": "安全感",
        "description": "哪一侧街景让您感觉行走或停留时更安全、更放心？",
        "definition": "对在街道中行走或停留时安全、放心程度的判断。",
        "order": 6,
        "score_direction": 1,
        "high_score_meaning": "越高表示安全感越强",
    },
    {
        "key": "oppressiveness",
        "label": "压抑感",
        "description": "哪一侧街景更让您感到沉闷、压迫或压抑？",
        "definition": "对环境带来的沉闷、压迫或压抑感受的判断。",
        "order": 7,
        "score_direction": -1,
        "high_score_meaning": "越高表示压抑感越强",
    },
    {
        "key": "vitality",
        "label": "活力感",
        "description": "哪一侧街景看起来更有人气、生机和活动氛围？",
        "definition": "对街景呈现的人气、生机和活动氛围的判断。",
        "order": 8,
        "score_direction": 1,
        "high_score_meaning": "越高表示活力感越强",
    },
    {
        "key": "overall_perception",
        "label": "综合感知",
        "description": "综合考虑上述各方面，哪一侧街景整体上给您的感受更好？",
        "definition": "综合考虑上述各方面后，对街景整体感受与总体偏好的判断。",
        "order": 9,
        "score_direction": 1,
        "high_score_meaning": "越高表示整体综合感受越好",
    },
]

if len(SURVEY_DIMENSIONS) != EXPECTED_DIMENSION_COUNT:
    raise RuntimeError("SURVEY_DIMENSIONS 数量必须为9")

_dimension_keys = [item["key"] for item in SURVEY_DIMENSIONS]
_dimension_orders = [item["order"] for item in SURVEY_DIMENSIONS]
if len(set(_dimension_keys)) != len(_dimension_keys):
    raise RuntimeError("SURVEY_DIMENSIONS 中存在重复 dimension key")
if sorted(_dimension_orders) != list(range(1, EXPECTED_DIMENSION_COUNT + 1)):
    raise RuntimeError("SURVEY_DIMENSIONS 的 order 必须为1—9且不重复")

VALID_CHOICES = {"left", "right", "tie"}
