"""命令行入口。

  检索：  python cli.py search 产品图.jpg [--brief "..."] [--download none|top|all] [--top 5]
  下载：  python cli.py download <run_id> --pick 1,3,5      （序号见 search 输出）
"""
import argparse
import sys

from refsearch import pipeline


def _print(result: dict) -> list[dict]:
    flat = []
    for g in result["groups"]:
        print(f"\n■ {g['name']} —— 参考什么：{g['what_to_learn']}")
        if not g["videos"]:
            print("   （无达标结果）")
        for v in g["videos"]:
            flat.append(v)
            print(f" [{len(flat):>2}] {v.get('score','-'):>2}分 {v['platform']:<8} {v['title'][:50]}")
            print(f"      {v['url']}")
            if v.get("reference"):
                print(f"      参考：{v['reference']}")
    print("\n■ 参考平台（手动浏览）")
    for pl in result["platform_links"]:
        print(f"  - {pl['name']}：{pl['learn']}\n    {pl['url']}")
    if result.get("errors"):
        print("\n⚠ 异常：\n  " + "\n  ".join(result["errors"]))
    return flat


def main():
    ap = argparse.ArgumentParser(description="产品图 → 参考广告视频检索")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("image")
    s.add_argument("--brief", default="", help="补充简报：目标、人群、调性等")
    s.add_argument("--platforms", default="youtube,bilibili,douyin,web_cn,web_intl",
                   help="可选 youtube,bilibili,douyin,web_cn,web_intl")
    s.add_argument("--per-query", type=int, default=8, help="每个关键词取多少条")
    s.add_argument("--max-duration", type=int, default=300, help="过滤超过该秒数的视频")
    s.add_argument("--top-per-dim", type=int, default=8)
    s.add_argument("--min-score", type=int, default=5)
    s.add_argument("--download", choices=["none", "top", "all"], default="none")
    s.add_argument("--top", type=int, default=5, help="--download top 时下载前 N 条")
    s.add_argument("--max-height", type=int, default=720)
    d = sub.add_parser("download")
    d.add_argument("run_id")
    d.add_argument("--pick", required=True, help="序号，逗号分隔，或 all")
    d.add_argument("--max-height", type=int, default=720)
    a = ap.parse_args()

    if a.cmd == "search":
        result = pipeline.run_search(
            a.image, a.brief, tuple(a.platforms.split(",")), a.per_query, a.max_duration,
            a.top_per_dim, a.min_score, a.download, a.top, a.max_height, progress=print)
        _print(result)
        print(f"\n报告：runs/{result['run_id']}/report.md")
        print(f"下载：python cli.py download {result['run_id']} --pick 1,2,3")
    else:
        result = pipeline.load(a.run_id)
        flat = [v for g in result["groups"] for v in g["videos"]]
        if a.pick == "all":
            pick = flat
        else:
            try:
                pick = [flat[int(i) - 1] for i in a.pick.split(",")]
            except (ValueError, IndexError):
                sys.exit(f"序号无效，可选范围 1-{len(flat)}")
        for r in pipeline.download_into(result, pick, a.max_height, progress=print):
            print(("✅ " if r["ok"] else "❌ ") + (r["file"] or r["error"]))


if __name__ == "__main__":
    main()
