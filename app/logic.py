"""核心业务逻辑：CSV 导入、清单同步（自动换用/停供）、编辑确认、授权撤回。

设计要点：
- manifest_items 记录每张（展图 × 原图）当前选用的授权与署名确认状态。
- sync_exhibition 是幂等的：任何读写路径都会先同步，保证下载/页面/接口
  看到的始终是“当前有效且已确认署名”的清单，服务重启后状态由 SQLite 恢复。
- 授权一旦撤回即为终态；导入时授权号已存在则整行跳过，重传不能恢复。
"""
import csv
import io
from datetime import datetime


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- CSV 解析

HEADER_ALIASES = {
    "exhibition_no": ["exhibition_no", "exhibition", "exhibition_id", "展图号", "展图编号"],
    "exhibition_title": ["exhibition_title", "title", "展图名称", "展图名"],
    "original_no": ["original_no", "original", "original_id", "原图号", "原图编号"],
    "license_no": ["license_no", "license", "授权号", "授权编号"],
    "commercial": ["commercial", "是否允许商用", "允许商用", "商用"],
    "attribution": ["attribution", "署名", "署名信息"],
}

TRUE_WORDS = {"1", "true", "yes", "y", "是", "允许", "允许商用", "可商用", "商用"}
FALSE_WORDS = {"0", "false", "no", "n", "否", "不允许", "禁止"}


def _read_table(content: bytes):
    text = content.decode("utf-8-sig")  # 兼容 Excel 导出的 BOM
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("CSV 内容为空")
    header = [h.strip() for h in rows[0]]
    return header, rows[1:]


def _column_map(header, required):
    lower = [h.lower() for h in header]
    mapping = {}
    for canon, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias.lower() in lower:
                mapping[canon] = lower.index(alias.lower())
                break
    missing = [k for k in required if k not in mapping]
    if missing:
        raise ValueError("CSV 缺少必需列: " + ", ".join(missing))
    return mapping


def _cell(row, idx):
    return row[idx].strip() if idx < len(row) else ""


def parse_commercial(s: str) -> int:
    v = s.strip().lower()
    if v in TRUE_WORDS:
        return 1
    if v in FALSE_WORDS:
        return 0
    raise ValueError(f"无法识别的“是否允许商用”取值: {s!r}")


# ---------------------------------------------------------------- 基础工具

def _get_or_create_original(conn, original_no: str) -> int:
    row = conn.execute("SELECT id FROM originals WHERE original_no=?", (original_no,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO originals (original_no) VALUES (?)", (original_no,)).lastrowid


def _get_or_create_exhibition(conn, exhibition_no: str, title: str = "") -> int:
    row = conn.execute("SELECT id, title FROM exhibitions WHERE exhibition_no=?", (exhibition_no,)).fetchone()
    if row:
        if title and title != row["title"]:
            conn.execute("UPDATE exhibitions SET title=? WHERE id=?", (title, row["id"]))
        return row["id"]
    return conn.execute(
        "INSERT INTO exhibitions (exhibition_no, title) VALUES (?,?)",
        (exhibition_no, title or exhibition_no),
    ).lastrowid


# ---------------------------------------------------------------- 导入

def import_mapping(conn, content: bytes) -> dict:
    """导入展图-原图对照 CSV。幂等：已存在的对照关系不会重复。"""
    header, rows = _read_table(content)
    cols = _column_map(header, ["exhibition_no", "original_no"])
    stats = {"rows": 0, "links_added": 0, "errors": []}
    for i, row in enumerate(rows, start=2):
        try:
            ex_no = _cell(row, cols["exhibition_no"])
            orig_no = _cell(row, cols["original_no"])
            title = _cell(row, cols["exhibition_title"]) if "exhibition_title" in cols else ""
            if not ex_no or not orig_no:
                raise ValueError("展图号或原图号为空")
            ex_id = _get_or_create_exhibition(conn, ex_no, title)
            orig_id = _get_or_create_original(conn, orig_no)
            cur = conn.execute(
                "INSERT OR IGNORE INTO exhibition_originals (exhibition_id, original_id) VALUES (?,?)",
                (ex_id, orig_id),
            )
            stats["links_added"] += cur.rowcount
            stats["rows"] += 1
        except Exception as e:  # 单行失败不影响其他行
            stats["errors"].append(f"第{i}行: {e}")
    return stats


def import_licenses(conn, content: bytes, batch_label: str = "") -> dict:
    """分批导入授权 CSV。

    授权号已存在的行直接跳过：既不覆盖已有数据，也绝不恢复已撤回的授权。
    """
    header, rows = _read_table(content)
    cols = _column_map(header, ["license_no", "original_no", "commercial", "attribution"])
    stats = {"added": 0, "duplicates": 0, "errors": []}
    for i, row in enumerate(rows, start=2):
        try:
            lic_no = _cell(row, cols["license_no"])
            orig_no = _cell(row, cols["original_no"])
            commercial = parse_commercial(_cell(row, cols["commercial"]))
            attribution = _cell(row, cols["attribution"])
            if not lic_no or not orig_no:
                raise ValueError("授权号或原图号为空")
            if not attribution:
                raise ValueError("署名为空")
            orig_id = _get_or_create_original(conn, orig_no)
            exists = conn.execute("SELECT id FROM licenses WHERE license_no=?", (lic_no,)).fetchone()
            if exists:
                stats["duplicates"] += 1  # 重传不生效，撤回状态保持不变
                continue
            conn.execute(
                "INSERT INTO licenses (license_no, original_id, commercial, attribution, batch_label)"
                " VALUES (?,?,?,?,?)",
                (lic_no, orig_id, commercial, attribution, batch_label),
            )
            stats["added"] += 1
        except Exception as e:
            stats["errors"].append(f"第{i}行: {e}")
    return stats


# ---------------------------------------------------------------- 清单同步

def _pick_license(conn, original_id: int, preferred_attribution):
    """为原图挑选有效商用授权：优先与在用署名一致的（用于自动换用）。"""
    if preferred_attribution:
        row = conn.execute(
            "SELECT * FROM licenses WHERE original_id=? AND commercial=1 AND revoked=0"
            " AND attribution=? ORDER BY id LIMIT 1",
            (original_id, preferred_attribution),
        ).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT * FROM licenses WHERE original_id=? AND commercial=1 AND revoked=0"
        " ORDER BY id LIMIT 1",
        (original_id,),
    ).fetchone()


def sync_exhibition(conn, exhibition_id: int) -> None:
    """同步一个展图的清单项（幂等）。

    - 在用授权仍有效（未撤回且允许商用）：保持不变；
    - 在用授权失效：存在同署名的有效商用授权 → 自动换用，保留署名确认；
      只有不同署名的 → 换用并置为“待确认署名”；
      完全没有 → 标记缺授权、待确认。
    """
    origs = conn.execute(
        "SELECT o.id AS original_id FROM exhibition_originals eo"
        " JOIN originals o ON o.id = eo.original_id WHERE eo.exhibition_id=?",
        (exhibition_id,),
    ).fetchall()
    for o in origs:
        oid = o["original_id"]
        item = conn.execute(
            "SELECT * FROM manifest_items WHERE exhibition_id=? AND original_id=?",
            (exhibition_id, oid),
        ).fetchone()
        if item is None:
            lic = _pick_license(conn, oid, None)
            conn.execute(
                "INSERT INTO manifest_items (exhibition_id, original_id, license_id, attribution,"
                " confirmed, updated_at) VALUES (?,?,?,?,0,?)",
                (exhibition_id, oid, lic["id"] if lic else None,
                 lic["attribution"] if lic else "", now()),
            )
            continue
        cur = None
        if item["license_id"]:
            cur = conn.execute("SELECT * FROM licenses WHERE id=?", (item["license_id"],)).fetchone()
        if cur and cur["commercial"] == 1 and cur["revoked"] == 0:
            continue  # 在用授权有效
        lic = _pick_license(conn, oid, item["attribution"] or None)
        if lic is None:
            conn.execute(
                "UPDATE manifest_items SET license_id=NULL, confirmed=0, updated_at=? WHERE id=?",
                (now(), item["id"]),
            )
        else:
            # 同署名换用保留确认状态；署名变化则必须重新确认
            confirmed = item["confirmed"] if lic["attribution"] == item["attribution"] else 0
            conn.execute(
                "UPDATE manifest_items SET license_id=?, attribution=?, confirmed=?, updated_at=?"
                " WHERE id=?",
                (lic["id"], lic["attribution"], confirmed, now(), item["id"]),
            )


def sync_all(conn) -> None:
    for r in conn.execute("SELECT id FROM exhibitions").fetchall():
        sync_exhibition(conn, r["id"])


def get_items(conn, exhibition_id: int) -> list:
    rows = conn.execute(
        "SELECT mi.id, mi.exhibition_id, mi.original_id, mi.license_id, mi.attribution,"
        " mi.confirmed, o.original_no,"
        " l.license_no, l.commercial, l.revoked"
        " FROM manifest_items mi"
        " JOIN originals o ON o.id = mi.original_id"
        " LEFT JOIN licenses l ON l.id = mi.license_id"
        " WHERE mi.exhibition_id=? ORDER BY o.original_no",
        (exhibition_id,),
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["license_valid"] = bool(d["license_id"]) and d["commercial"] == 1 and d["revoked"] == 0
        items.append(d)
    return items


def exhibition_status(conn, exhibition_id: int) -> dict:
    """先同步再汇总：downloadable 当且仅当每张原图都有有效商用授权且署名已确认。"""
    sync_exhibition(conn, exhibition_id)
    items = get_items(conn, exhibition_id)
    missing = [it["original_no"] for it in items if not it["license_valid"]]
    unconfirmed = [it["original_no"] for it in items if it["license_valid"] and not it["confirmed"]]
    return {
        "items": items,
        "missing": missing,
        "unconfirmed": unconfirmed,
        "downloadable": bool(items) and not missing and not unconfirmed,
    }


# ---------------------------------------------------------------- 撤回与确认

def revoke_license(conn, license_id: int):
    """撤回授权（终态）。返回 (license_row, message)；授权不存在时 row 为 None。"""
    lic = conn.execute("SELECT * FROM licenses WHERE id=?", (license_id,)).fetchone()
    if lic is None:
        return None, "授权不存在"
    if lic["revoked"]:
        return lic, f"授权 {lic['license_no']} 此前已撤回，重复撤回或重传 CSV 都不会恢复"
    conn.execute("UPDATE licenses SET revoked=1, revoked_at=? WHERE id=?", (now(), license_id))
    ex_ids = [
        r["exhibition_id"]
        for r in conn.execute(
            "SELECT exhibition_id FROM exhibition_originals WHERE original_id=?",
            (lic["original_id"],),
        ).fetchall()
    ]
    for ex_id in ex_ids:
        sync_exhibition(conn, ex_id)  # 有同署名有效授权则自动换用，否则停供
    return lic, f"授权 {lic['license_no']} 已撤回"


def confirm_manifest(conn, exhibition_id: int, editor: str, submitted: dict) -> list:
    """编辑确认当前清单。submitted: {original_id: (license_no, attribution)}。

    在调用方的事务内执行；返回问题列表，非空表示维持停供（调用方应回滚）。
    交错场景：确认时重新核对授权有效性，若清单中的授权刚被撤回或清单已变更，
    则拒绝确认并提示重新核对。
    """
    sync_exhibition(conn, exhibition_id)
    items = get_items(conn, exhibition_id)
    if not items:
        return ["该展图没有关联原图，无法确认"]
    problems = []
    for it in items:
        if not it["license_valid"]:
            problems.append(
                f"原图 {it['original_no']}：清单中的授权已被撤回或失效，请重新核对"
            )
    for it in items:
        if not it["license_valid"] or it["confirmed"]:
            continue
        sub = submitted.get(it["original_id"])
        if sub is None or sub[0] != (it["license_no"] or "") or sub[1] != it["attribution"]:
            problems.append(f"原图 {it['original_no']}：清单已变更，请重新核对后再确认")
    if problems:
        return problems
    conn.execute(
        "UPDATE manifest_items SET confirmed=1, updated_at=? WHERE exhibition_id=?",
        (now(), exhibition_id),
    )
    detail = "; ".join(f"{it['original_no']}={it['license_no']}" for it in items)
    conn.execute(
        "INSERT INTO confirmations (exhibition_id, editor, detail) VALUES (?,?,?)",
        (exhibition_id, editor, detail),
    )
    return []
