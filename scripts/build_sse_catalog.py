from __future__ import annotations

import argparse
import re
from datetime import datetime, time
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION_DIR = PROJECT_ROOT / "data" / "session"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "sse_catalog.csv"


def normalize_timestamp(value: object) -> str:
    text = "" if pd.isna(value) else str(value).strip()
    text = (
        text.replace("午前", "AM")
        .replace("午後", "PM")
        .replace("ａｍ", "AM")
        .replace("ｐｍ", "PM")
        .replace("ＡＭ", "AM")
        .replace("ＰＭ", "PM")
    )
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"(?<=\d)-(?=AM|PM\b)", "", text, flags=re.IGNORECASE)
    return text


def _years_from_source_name(name: str) -> tuple[int | None, int | None, int | None]:
    stem = Path(name).stem
    period = stem.split("__", 1)[0]
    nums = [int(x) for x in re.findall(r"\d+", period)]
    years = [x for x in nums if 1900 <= x <= 2100]
    if not years:
        return None, None, None
    start_year = years[0]
    end_year = years[1] if len(years) > 1 else start_year
    start_month = next((x for x in nums[1:] if 1 <= x <= 12), 1)
    return start_year, end_year, start_month


def _infer_year(month: int, source_name: str) -> int | None:
    start_year, end_year, start_month = _years_from_source_name(source_name)
    if start_year is None:
        return None
    if end_year != start_year and start_month and month < start_month:
        return end_year
    return start_year


def _time_of_day(text: str) -> time:
    first_part = text.split("-", 1)[0].upper()
    return time(12) if "PM" in first_part else time(0)


def timestamp_sort_key(timestamp: object, source_name: str) -> pd.Timestamp:
    text = normalize_timestamp(timestamp)
    tod = _time_of_day(text)

    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
        return pd.Timestamp(datetime.combine(datetime(y, mo, d), tod))

    m = re.search(r"(\d{4})/(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
        return pd.Timestamp(datetime.combine(datetime(y, mo, d), tod))

    m = re.search(r"(\d{1,2})/(\d{1,2})", text)
    if m:
        mo, d = map(int, m.groups())
        y = _infer_year(mo, source_name)
        if y is not None:
            return pd.Timestamp(datetime.combine(datetime(y, mo, d), tod))

    m = re.search(r"(\d{4})/(\d{1,2})", text)
    if m:
        y, mo = map(int, m.groups())
        return pd.Timestamp(datetime.combine(datetime(y, mo, 1), tod))

    return pd.NaT


def load_session_csvs(session_dir: Path) -> pd.DataFrame:
    csv_paths = sorted(session_dir.glob("_*.csv"))
    if not csv_paths:
        raise FileNotFoundError(f"No session CSV files found in {session_dir}")

    parts = []
    for path in csv_paths:
        part = pd.read_csv(path)
        part["_source_file"] = path.name
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def build_catalog(session_dir: Path = DEFAULT_SESSION_DIR) -> pd.DataFrame:
    df = load_session_csvs(session_dir)
    if "timestamp" not in df.columns:
        raise KeyError("Input CSVs must contain a 'timestamp' column")

    df["timestamp"] = df["timestamp"].map(normalize_timestamp)
    df["_sort_timestamp"] = df.apply(
        lambda row: timestamp_sort_key(row["timestamp"], row["_source_file"]), axis=1
    )
    df = df.sort_values(["_sort_timestamp", "timestamp"], kind="mergesort")
    return df.drop(columns=["_sort_timestamp", "_source_file"])


def write_catalog(output_path: Path, session_dir: Path = DEFAULT_SESSION_DIR) -> int:
    df = build_catalog(session_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    return len(df)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build data/sse_catalog.csv from data/session/_*.csv files."
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        default=DEFAULT_SESSION_DIR,
        help="Directory containing per-PDF session CSV files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output catalog CSV path.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    row_count = write_catalog(args.output, args.session_dir)
    print(f"Wrote {row_count} rows to {args.output}")


if __name__ == "__main__":
    main()
