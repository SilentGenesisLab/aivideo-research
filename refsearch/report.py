"""输出 Markdown 报告（方便贴进飞书/Notion 素材库）。"""
from pathlib import Path

from . import filters


def _dur(s):
    return f"{s // 60}:{s % 60:02d}" if s else "-"


def _views(n):
    if n is None:
        return "-"
    return f"{n / 1e4:.1f}万" if n >= 1e4 else str(n)


def write_markdown(result: dict, path: Path) -> None:
    a = result["analysis"]
    p = a["product"]
    L = [f"# 参考视频检索报告 · {p.get('brand','')} {p.get('name','')}", "",
         f"生成时间：{result['created']}　|　检索 {result['stats']['queries']} 次，"
         f"原始 {result['stats']['raw']} 条，保留 {result['stats']['kept']} 条", ""]
    if result.get("brief"):
        L += [f"**简报**：{result['brief']}", ""]
    if (f := filters.describe(filters.normalize(result.get("filters")))):
        L += [f"**筛选条件**：{f}", ""]
    L += ["## 产品画像", "",
          f"- 品牌 / 产品：{p.get('brand','')} / {p.get('name','')}",
          f"- 品类：{p.get('category','')}",
          f"- 外观：{p.get('appearance','')}",
          f"- 卖点：{'、'.join(p.get('selling_points', []))}",
          f"- 目标人群：{p.get('target_audience','')}",
          f"- 调性：{p.get('tone','')}",
          f"- 竞品：{'、'.join(p.get('competitors', []))}", ""]
    L += ["## 参考视频（按维度）", "",
          "> 参考点由 AI 根据标题与元数据推断，请点开原片核实后再写入拆解表。", ""]
    for g in result["groups"]:
        L += [f"### {g.get('name','')}", "", f"**参考什么**：{g.get('what_to_learn','')}", "",
              f"关键词：{' / '.join(g.get('queries_zh', []) + g.get('queries_en', []))}", ""]
        if not g["videos"]:
            L += ["（未找到达标结果，可换关键词或去下方参考平台手动搜）", ""]
            continue
        L += ["| 分 | 类型 | 平台 | 标题 | 时长 | 播放 | 参考点 |", "|---|---|---|---|---|---|---|"]
        for v in g["videos"]:
            title = v["title"].replace("|", "/")
            ref = (v.get("reference") or "").replace("|", "/")
            L.append(f"| {v.get('score','-')} | {filters.TYPE_NAMES.get(v.get('vtype'), '-')} | {v['platform']} | [{title}]({v['url']}) | "
                     f"{_dur(v.get('duration'))} | {_views(v.get('views'))} | {ref} |")
        L.append("")
    L += ["## 参考平台（需手动浏览）", "", "| 平台 | 类型 | 参考什么 | 直达搜索 |", "|---|---|---|---|"]
    for pl in result["platform_links"]:
        L.append(f"| {pl['name']} | {pl['kind_name']} | {pl['learn']} | [{pl['query'] or '打开'}]({pl['url']}) |")
    L.append("")
    if result.get("downloads"):
        L += ["## 已下载", ""]
        for d in result["downloads"]:
            L.append(f"- {'✅' if d['ok'] else '❌'} {d['title']} → "
                     f"{d['file'] or d['error']}")
        L.append("")
    if result.get("errors"):
        L += ["## 检索异常", ""] + [f"- {e}" for e in result["errors"]] + [""]
    path.write_text("\n".join(L), encoding="utf-8")
