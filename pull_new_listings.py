#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取官方公租房月表，并只导出当前 listings.csv 中没有的项目。

本脚本是独立的核对工具：不会修改 listings.csv，也不会调用 enrich.py。
输出前 9 列与 listings.csv 保持一致，后面追加官方公示字段，方便人工核对。
官方没有单套最低/最高月租时，脚本按面积 × 平均租金/㎡估算，并用“租金口径”标记。

用法：
    python pull_new_listings.py
    python pull_new_listings.py --only-available
    python pull_new_listings.py --output /tmp/new_listings.csv
"""

import argparse
import csv
import datetime as dt
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path


BASE = Path(__file__).resolve().parent
INDEX_URL = "https://fgj.sh.gov.cn/ggzlzfgsgg/"
USER_AGENT = "shanghai-landing-kit/new-listings-checker (+https://fgj.sh.gov.cn/)"
MISSING = {"", "—", "--", "——", "/", "暂无", "无"}
BASE_FIELDS = ["项目名称", "区域", "地址", "户型", "最低租金", "最高租金", "最小面积", "最大面积", "联系电话"]
EXTRA_FIELDS = [
    "核对状态", "匹配依据", "供应状态", "公示可供摘要", "公示可供套数", "公示户型面积",
    "平均租金元每平方米", "租金口径", "运营机构", "微信公众号", "source_title", "source_url",
]
# 官方月表有些项目地址只写道路名。以下是已由政府/运营机构资料确认的项目归属，
# 只用于补齐 listings.csv 的“区域”列，不做模糊推断。
KNOWN_DISTRICTS = {
    "金鹤新城水岸金桥苑": "嘉定区",
    "金鹤新城城杰苑": "嘉定区",
    "金鹤新城双佳翠庭": "嘉定区",
    "昱龙家园(南区)": "浦东新区",
    "安阁苑(中区)": "浦东新区",
    "体育花苑": "徐汇区",
    "驰骋苑": "静安区",
    "宜川三村小区": "普陀区",
    "曹杨九村金杨园": "普陀区",
    "真建一小区": "普陀区",
    "君莲慧馨苑": "闵行区",
}


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "ignore"), response.geturl()


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.href = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.href = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.href is not None:
            self.links.append((" ".join("".join(self.text).split()), self.href))
            self.href = None
            self.text = []


def find_latest_notice(index_html, index_url):
    parser = LinkParser()
    parser.feed(index_html)
    candidates = []
    for title, href in parser.links:
        if "公共租赁住房" not in title or "信息" not in title:
            continue
        match = re.search(r"(20\d{2})年\s*(\d{1,2})月", title)
        if not match:
            continue
        month = dt.date(int(match.group(1)), int(match.group(2)), 1)
        candidates.append((month, title, urllib.parse.urljoin(index_url, href)))
    if not candidates:
        raise RuntimeError("官方公告列表中没有找到带月份的公共租赁住房信息")
    return max(candidates, key=lambda item: item[0])


class TableParser(HTMLParser):
    """读取公告表的 td，并保留 colspan/rowspan 信息。"""

    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag = tag.lower()
        if tag == "tr":
            self.row = []
        elif tag == "td" and self.row is not None:
            self.cell = {
                "text": [],
                "colspan": int(attrs.get("colspan", "1") or "1"),
                "rowspan": int(attrs.get("rowspan", "1") or "1"),
            }
        elif tag == "br" and self.cell is not None:
            self.cell["text"].append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"].append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "td" and self.cell is not None:
            self.cell["text"] = " ".join("".join(self.cell["text"]).split())
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def expand_rows(rows):
    pending = {}
    expanded = []
    for cells in rows:
        output = []
        col = 0
        for cell in cells:
            while col in pending:
                while len(output) <= col:
                    output.append("")
                output[col] = pending[col][1]
                col += 1
            text = cell["text"]
            for offset in range(cell["colspan"]):
                target = col + offset
                while len(output) <= target:
                    output.append("")
                output[target] = text
                if cell["rowspan"] > 1:
                    pending[target] = (cell["rowspan"], text)
            col += cell["colspan"]
        for target, (_, text) in pending.items():
            while len(output) <= target:
                output.append("")
            if not output[target]:
                output[target] = text
        pending = {
            target: (left - 1, text)
            for target, (left, text) in pending.items()
            if left - 1 > 0
        }
        expanded.append(output)
    return expanded


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def is_project_row(row):
    if len(row) < 3:
        return False
    first, second = clean(row[0]), clean(row[1])
    if first.startswith("面向社会供应") or first.startswith("市筹公共租赁住房"):
        return False
    return bool(first and second and first not in {"项目名称", "一房", "总数（可供）", "轮候户数"})


def numbers(value):
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", clean(value))]


def last_integer(value):
    values = re.findall(r"\d+", clean(value))
    return values[-1] if values else ""


def district_from_address(address):
    match = re.match(r"(黄浦|徐汇|长宁|静安|普陀|虹口|杨浦|闵行|宝山|嘉定|浦东|金山|松江|青浦|奉贤|崇明)区", address)
    return f"{match.group(1)}区" if match else ""


def parse_notice(notice_html, title, source_url):
    parser = TableParser()
    parser.feed(notice_html)
    projects = []
    for raw_row in expand_rows(parser.rows):
        if not is_project_row(raw_row):
            continue
        row = [clean(value) for value in raw_row]
        if len(row) < 17:
            continue
        area_cells = row[12:16]
        area_values = [number for cell in area_cells for number in numbers(cell) if cell not in MISSING]
        room_types = [
            room for room, cell in zip(("一居室", "二居室", "三居室", "宿舍"), area_cells)
            if cell not in MISSING
        ]
        available = last_integer(row[3])
        if available:
            supply_status = "公示有可供套数" if int(available) > 0 else "公示可供为0"
        elif row[3] in MISSING:
            supply_status = "公示未给出套数"
        else:
            supply_status = "需人工判断"
        average_rent = row[16] if row[16] not in MISSING else ""
        average_rent_values = numbers(average_rent)
        if area_values and average_rent_values:
            estimated_low = str(int(round(min(area_values) * average_rent_values[0])))
            estimated_high = str(int(round(max(area_values) * average_rent_values[0])))
            rent_basis = "估算：最小/最大面积 × 平均租金/㎡"
        else:
            estimated_low = ""
            estimated_high = ""
            rent_basis = "未估算：缺少面积或平均租金"
        projects.append({
            "项目名称": row[0],
            "区域": district_from_address(row[1]) or KNOWN_DISTRICTS.get(row[0], ""),
            "地址": row[1],
            "户型": "、".join(room_types),
            "最低租金": estimated_low,
            "最高租金": estimated_high,
            "最小面积": str(min(area_values)).rstrip("0").rstrip(".") if area_values else "",
            "最大面积": str(max(area_values)).rstrip("0").rstrip(".") if area_values else "",
            "联系电话": row[18] if len(row) > 18 and row[18] not in MISSING else "",
            "供应状态": supply_status,
            "公示可供摘要": row[3],
            "公示可供套数": available,
            "公示户型面积": "；".join(f"{room}:{cell}" for room, cell in zip(("一居室", "二居室", "三居室", "宿舍"), area_cells) if cell not in MISSING),
            "平均租金元每平方米": average_rent,
            "租金口径": rent_basis,
            "运营机构": row[17] if len(row) > 17 and row[17] not in MISSING else "",
            "微信公众号": row[19] if len(row) > 19 and row[19] not in MISSING else "",
            "source_title": title,
            "source_url": source_url,
        })
    if not projects:
        raise RuntimeError("公告页面没有解析出项目行，可能是官方表格结构已变化")
    return projects


def norm(value):
    """用于比对的保守归一化：不做模糊匹配，只统一空白和括号/标点。"""
    value = clean(value).lower()
    return re.sub(r"[\s　、,，。．·•()（）【】\[\]《》：:—–-]", "", value)


def load_existing(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [field for field in BASE_FIELDS[:3] if field not in (reader.fieldnames or [])]
        if missing:
            raise RuntimeError(f"现有房源表缺少必填列：{', '.join(missing)}")
        rows = list(reader)
    by_pair = {(norm(row.get("项目名称")), norm(row.get("地址"))): row for row in rows}
    by_name = {}
    by_address = {}
    for row in rows:
        name, address = norm(row.get("项目名称")), norm(row.get("地址"))
        if name:
            by_name.setdefault(name, []).append(row)
        if address:
            by_address.setdefault(address, []).append(row)
    return by_pair, by_name, by_address, len(rows)


def match_project(project, by_pair, by_name, by_address):
    pair = (norm(project["项目名称"]), norm(project["地址"]))
    if pair in by_pair:
        return "已存在", "项目名称+地址"
    name_matches = by_name.get(pair[0], []) if pair[0] else []
    if len(name_matches) == 1:
        return "已存在", "项目名称"
    address_matches = by_address.get(pair[1], []) if pair[1] else []
    if len(address_matches) == 1:
        return "已存在", "地址"
    return "新增候选", "无唯一匹配"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listings", default="listings.csv", help="现有房源 CSV")
    parser.add_argument("--output", default="new_listings_candidates.csv", help="新增候选 CSV")
    parser.add_argument("--source-json", default="new_listings_source.json", help="来源元数据 JSON")
    parser.add_argument("--only-available", action="store_true", help="只保留公示明确有可供套数的项目")
    args = parser.parse_args()

    listings_path = Path(args.listings)
    if not listings_path.is_absolute():
        listings_path = BASE / listings_path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = BASE / output_path
    source_path = Path(args.source_json)
    if not source_path.is_absolute():
        source_path = BASE / source_path

    try:
        by_pair, by_name, by_address, existing_count = load_existing(listings_path)
        index_html, index_final = fetch(INDEX_URL)
        notice_month, title, notice_url = find_latest_notice(index_html, index_final)
        notice_html, notice_final = fetch(notice_url)
        projects = parse_notice(notice_html, title, notice_final)
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    candidates = []
    existing_count_in_notice = 0
    for project in projects:
        status, basis = match_project(project, by_pair, by_name, by_address)
        if status == "已存在":
            existing_count_in_notice += 1
            continue
        if args.only_available and project["供应状态"] != "公示有可供套数":
            continue
        project["核对状态"] = "新增候选"
        project["匹配依据"] = basis
        candidates.append(project)

    fields = BASE_FIELDS + EXTRA_FIELDS
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(candidates)

    metadata = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "notice_month": notice_month.isoformat(),
        "source_index_url": index_final,
        "source_url": notice_final,
        "source_title": title,
        "existing_listings_count": existing_count,
        "official_project_count": len(projects),
        "already_in_existing_count": existing_count_in_notice,
        "new_candidate_count": len(candidates),
        "only_available": args.only_available,
        "note": "候选行未写回 listings.csv；最低/最高租金缺失时按面积×平均租金/㎡估算，仍请人工核对区域、地址、实租和当期供应后再合并。",
    }
    source_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"官方公示：{title}")
    print(f"来源：{notice_final}")
    print(f"现有房源：{existing_count} 条；官方项目：{len(projects)} 条；已匹配：{existing_count_in_notice} 条")
    print(f"新增候选：{len(candidates)} 条")
    print(f"输出：{output_path}")
    print(f"元数据：{source_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
