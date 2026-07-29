"""Legacy/local helper for reading the fixed 250×30 assignment file.

The production Flask application reads assignments from PostgreSQL. This module
is retained for diagnostics and local inspection only; it must never reshuffle
or regenerate pairs at runtime.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

from config import (
    ASSIGNMENT_TABLE_PATH,
    EXPECTED_PAIR_COUNT,
    EXPECTED_PAIRS_PER_ATTEMPT,
    EXPECTED_SLOT_COUNT,
    OSS_BASE_URL,
)


REQUIRED_COLUMNS = {
    "pair_id",
    "participant_slot",
    "order_in_participant",
    "left_qid",
    "right_qid",
    "left_image_id",
    "right_image_id",
    "left_image_filename",
    "right_image_filename",
    "left_image_relative_path",
    "right_image_relative_path",
}


def _clean(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def _build_url(explicit_url, relative_path, filename):
    explicit = _clean(explicit_url)
    if explicit:
        return explicit
    path_value = _clean(relative_path) or _clean(filename)
    if not path_value:
        raise ValueError("固定配对行缺少图片路径和文件名")
    path_value = path_value.replace("\\", "/").lstrip("/")
    if path_value.startswith("images/"):
        path_value = path_value[len("images/"):]
    return f"{OSS_BASE_URL}/{path_value}"


@lru_cache(maxsize=1)
def load_assignment_table() -> pd.DataFrame:
    path = Path(ASSIGNMENT_TABLE_PATH)
    if not path.exists():
        raise FileNotFoundError(f"未找到固定配对表：{path}")

    if path.suffix.lower() in {".xlsx", ".xls"}:
        frame = pd.read_excel(path)
    else:
        frame = pd.read_csv(path, encoding="utf-8-sig")
    frame.columns = frame.columns.astype(str).str.strip()

    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError("固定配对表缺少字段：" + ", ".join(missing))
    if len(frame) != EXPECTED_PAIR_COUNT:
        raise ValueError(
            f"固定配对表应为{EXPECTED_PAIR_COUNT}行，实际为{len(frame)}行"
        )

    counts = frame.groupby("participant_slot").size()
    if len(counts) != EXPECTED_SLOT_COUNT:
        raise ValueError(
            f"固定配对表应有{EXPECTED_SLOT_COUNT}个槽位，实际为{len(counts)}个"
        )
    if not (counts == EXPECTED_PAIRS_PER_ATTEMPT).all():
        raise ValueError("固定配对表存在不是30组题的槽位")

    frame["participant_slot"] = frame["participant_slot"].astype(str).str.strip()
    frame["order_in_participant"] = pd.to_numeric(
        frame["order_in_participant"], errors="raise"
    ).astype(int)
    return frame


def normalize_slot(slot_id):
    text_value = str(slot_id).strip().upper()
    if text_value.startswith("P_SLOT_"):
        return text_value
    try:
        number = int(text_value)
    except ValueError as exc:
        raise ValueError(f"无效槽位：{slot_id}") from exc
    return f"P_SLOT_{number:03d}"


def get_survey_questions(slot_id):
    target = normalize_slot(slot_id)
    frame = load_assignment_table()
    subset = frame[frame["participant_slot"] == target].copy()
    if subset.empty:
        raise KeyError(f"固定配对表中不存在槽位：{target}")
    subset = subset.sort_values("order_in_participant")
    if len(subset) != EXPECTED_PAIRS_PER_ATTEMPT:
        raise ValueError(f"槽位{target}不是30组题")

    questions = []
    for row in subset.to_dict("records"):
        questions.append(
            {
                "order": int(row["order_in_participant"]),
                "pair_id": _clean(row["pair_id"]),
                "left_qid": _clean(row["left_qid"]),
                "right_qid": _clean(row["right_qid"]),
                "left_image_id": _clean(row["left_image_id"]),
                "right_image_id": _clean(row["right_image_id"]),
                "left_img_url": _build_url(
                    row.get("left_oss_url"),
                    row.get("left_image_relative_path"),
                    row.get("left_image_filename"),
                ),
                "right_img_url": _build_url(
                    row.get("right_oss_url"),
                    row.get("right_image_relative_path"),
                    row.get("right_image_filename"),
                ),
            }
        )
    return questions
