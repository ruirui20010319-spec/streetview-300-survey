import os

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if not DATABASE_URL:
    raise RuntimeError("缺少环境变量 DATABASE_URL")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "survey2026")
OSS_BASE_URL = os.getenv(
    "OSS_BASE_URL",
    "https://streetview-images.oss-cn-hangzhou.aliyuncs.com",
).rstrip("/")

SURVEY_VERSION = os.getenv("SURVEY_VERSION", "survey_v1")
ASSIGNMENT_VERSION = os.getenv(
    "ASSIGNMENT_VERSION",
    "assignment_300img_150slot_v1",
)
DIMENSION_CONFIG_VERSION = os.getenv(
    "DIMENSION_CONFIG_VERSION",
    "dimensions_v1",
)

EXPECTED_PAIRS_PER_ATTEMPT = 30

SURVEY_DIMENSIONS = [
    {
        "key": "beauty",
        "label": "美观度",
        "description": "哪个更美丽、赏心悦目？",
        "order": 1,
    },
    {
        "key": "safety",
        "label": "安全感",
        "description": "哪个让你感觉更安全、放心？",
        "order": 2,
    },
    {
        "key": "vitality",
        "label": "活力度",
        "description": "哪个看起来更有城市生机与活力？",
        "order": 3,
    },
    {
        "key": "depression",
        "label": "压抑感",
        "description": "哪个环境让你觉得更压抑、沉闷？",
        "order": 4,
    },
    {
        "key": "wealthy",
        "label": "富裕度",
        "description": "哪个街区视觉上看起来更富裕、高级？",
        "order": 5,
    },
    {
        "key": "boring",
        "label": "枯燥度",
        "description": "哪个街道环境让你觉得更无趣、枯燥？",
        "order": 6,
    },
    {
        "key": "harmony",
        "label": "和谐度",
        "description": "哪个街景的建筑与绿化整体更和谐？",
        "order": 7,
    },
    {
        "key": "active",
        "label": "活动意愿",
        "description": "哪个街道更想让你下车去走走、逛逛？",
        "order": 8,
    },
]

VALID_CHOICES = {"left", "right", "tie"}
