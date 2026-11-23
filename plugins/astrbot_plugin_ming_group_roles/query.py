"""查一个人在某群是什么身份（只读缓存，不连 OneBot）。

用法：
  python query.py 700000001 100000001      # 单查
  python query.py 700000001                # 列这个群的群主和管理员
  python query.py --all                    # 列所有群各有多少群主/管理员
"""
import json
import os
import sys

CACHE_PATH = os.path.join(r"C:\dsh\astrbot\data\ming", "group-roles.json")
ZH = {"owner": "群主", "admin": "管理员", "member": "普通群友", "unknown": "身份不明（缓存里没这个群）"}


def load() -> dict:
    with open(CACHE_PATH, encoding="utf-8") as f:
        return json.load(f)


def name(p: dict) -> str:
    return (p.get("card") or p.get("nick") or p.get("qq") or "?").strip()


def main() -> int:
    if not os.path.exists(CACHE_PATH):
        print(f"没有缓存：{CACHE_PATH}（插件还没跑过，或者拉取全失败）")
        return 1
    data = load()
    args = [a for a in sys.argv[1:] if a]

    if not args or args[0] == "--all":
        meta = data.get("$meta") or {}
        print(f"缓存时间 {meta.get('updated_at', '?')}，共 {meta.get('groups', '?')} 群")
        for gid, g in data.items():
            if gid.startswith("$") or not isinstance(g, dict):
                continue
            print(
                f"{gid}  {g.get('members_count', '?')} 人  "
                f"群主 {len(g.get('owner') or [])} / 管理员 {len(g.get('admins') or [])}  "
                f"更新 {g.get('updated_at', '?')}"
            )
        return 0

    gid = str(args[0])
    g = data.get(gid)
    if not isinstance(g, dict):
        print(f"{gid}：缓存里没有这个群")
        return 1

    if len(args) < 2:
        print(f"{gid}  {g.get('members_count', '?')} 人  更新 {g.get('updated_at', '?')}")
        print("群主：" + ("、".join(f"{name(p)}({p['qq']})" for p in g.get("owner") or []) or "（无）"))
        admins = g.get("admins") or []
        print("管理员：" + ("、".join(f"{name(p)}({p['qq']})" for p in admins) or "（无）"))
        return 0

    uid = str(args[1])
    for p in g.get("owner") or []:
        if str(p.get("qq")) == uid:
            print(f"{uid} 在 {gid} 是 {ZH['owner']}：{name(p)}")
            return 0
    for p in g.get("admins") or []:
        if str(p.get("qq")) == uid:
            print(f"{uid} 在 {gid} 是 {ZH['admin']}：{name(p)}")
            return 0
    print(f"{uid} 在 {gid} 是 {ZH['member']}（缓存里不是群主/管理员）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
