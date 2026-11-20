"""FastAPI 网页与接口：展图授权清单管理。"""
import csv
import io
import os
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db, logic

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="展图授权清单管理", lifespan=lifespan)


def redirect(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    params = {}
    if msg:
        params["msg"] = msg
    if err:
        params["err"] = err
    if params:
        url = f"{url}?{urlencode(params)}"
    return RedirectResponse(url, status_code=303)


def flash_of(request: Request) -> dict:
    return {"msg": request.query_params.get("msg", ""),
            "err": request.query_params.get("err", "")}


def get_exhibition_or_404(conn, ex_id: int):
    ex = conn.execute("SELECT * FROM exhibitions WHERE id=?", (ex_id,)).fetchone()
    if ex is None:
        raise HTTPException(status_code=404, detail="展图不存在")
    return ex


# ---------------------------------------------------------------- 页面

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        logic.sync_all(conn)
        rows = conn.execute(
            "SELECT e.*, (SELECT COUNT(*) FROM exhibition_originals eo"
            " WHERE eo.exhibition_id=e.id) AS total"
            " FROM exhibitions e ORDER BY e.exhibition_no"
        ).fetchall()
        exhibitions = []
        for ex in rows:
            st = logic.exhibition_status(conn, ex["id"])
            exhibitions.append({
                "id": ex["id"], "exhibition_no": ex["exhibition_no"], "title": ex["title"],
                "total": ex["total"], "downloadable": st["downloadable"],
                "missing": st["missing"], "unconfirmed": st["unconfirmed"],
            })
        conn.commit()
    finally:
        conn.close()
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"exhibitions": exhibitions, **flash_of(request)})


@app.get("/exhibitions/{ex_id}", response_class=HTMLResponse)
def exhibition_detail(request: Request, ex_id: int):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ex = get_exhibition_or_404(conn, ex_id)
        st = logic.exhibition_status(conn, ex_id)
        for it in st["items"]:
            it["alternatives"] = [dict(a) for a in conn.execute(
                "SELECT license_no, attribution FROM licenses"
                " WHERE original_id=? AND commercial=1 AND revoked=0 AND id != ?"
                " ORDER BY id",
                (it["original_id"], it["license_id"] or -1),
            ).fetchall()]
        confirmations = [dict(r) for r in conn.execute(
            "SELECT editor, detail, created_at FROM confirmations"
            " WHERE exhibition_id=? ORDER BY id DESC LIMIT 10", (ex_id,),
        ).fetchall()]
        conn.commit()
    finally:
        conn.close()
    return templates.TemplateResponse(
        request=request, name="exhibition.html",
        context={"ex": dict(ex), "st": st, "confirmations": confirmations,
                 **flash_of(request)})


@app.get("/licenses", response_class=HTMLResponse)
def licenses_page(request: Request):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        logic.sync_all(conn)
        rows = conn.execute(
            "SELECT l.*, o.original_no,"
            " GROUP_CONCAT(DISTINCT e.exhibition_no) AS used_by"
            " FROM licenses l"
            " JOIN originals o ON o.id = l.original_id"
            " LEFT JOIN manifest_items mi ON mi.license_id = l.id"
            " LEFT JOIN exhibitions e ON e.id = mi.exhibition_id"
            " GROUP BY l.id ORDER BY l.id"
        ).fetchall()
        conn.commit()
    finally:
        conn.close()
    return templates.TemplateResponse(
        request=request, name="licenses.html",
        context={"licenses": [dict(r) for r in rows], **flash_of(request)})


# ---------------------------------------------------------------- 导入

@app.post("/upload/mapping")
async def upload_mapping(file: UploadFile):
    content = await file.read()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            stats = logic.import_mapping(conn, content)
        except ValueError as e:
            conn.rollback()
            return redirect("/", err=f"对照 CSV 导入失败：{e}")
        logic.sync_all(conn)
        conn.commit()
    finally:
        conn.close()
    msg = f"对照导入完成：{stats['rows']} 行，新增 {stats['links_added']} 条对照关系"
    if stats["errors"]:
        return redirect("/", err=msg + "；部分行出错：" + "；".join(stats["errors"][:5]))
    return redirect("/", msg=msg)


@app.post("/upload/licenses")
async def upload_licenses(request: Request, file: UploadFile):
    form = await request.form()
    batch = str(form.get("batch", "")).strip() or (file.filename or "")
    content = await file.read()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            stats = logic.import_licenses(conn, content, batch)
        except ValueError as e:
            conn.rollback()
            return redirect("/", err=f"授权 CSV 导入失败：{e}")
        logic.sync_all(conn)
        conn.commit()
    finally:
        conn.close()
    msg = f"授权导入完成：新增 {stats['added']} 条"
    if stats["duplicates"]:
        msg += f"，跳过已存在授权号 {stats['duplicates']} 条（重传不会恢复已撤回的授权）"
    if stats["errors"]:
        return redirect("/", err=msg + "；部分行出错：" + "；".join(stats["errors"][:5]))
    return redirect("/", msg=msg)


# ---------------------------------------------------------------- 撤回 / 确认 / 下载

@app.post("/licenses/{license_id}/revoke")
async def revoke(license_id: int):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        lic, message = logic.revoke_license(conn, license_id)
        if lic is None:
            conn.rollback()
            return redirect("/licenses", err=message)
        conn.commit()
    finally:
        conn.close()
    if lic["revoked"] and "此前已撤回" in message:
        return redirect("/licenses", err=message)
    return redirect("/licenses", msg=message)


@app.post("/exhibitions/{ex_id}/confirm")
async def confirm(ex_id: int, request: Request):
    form = await request.form()
    editor = str(form.get("editor", "")).strip()
    if not editor:
        return redirect(f"/exhibitions/{ex_id}", err="请填写编辑姓名")
    submitted = {}
    for key in form.keys():
        if key.startswith("license_"):
            try:
                oid = int(key[len("license_"):])
            except ValueError:
                continue
            submitted[oid] = (str(form.get(key, "")),
                              str(form.get(f"attribution_{oid}", "")))
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        get_exhibition_or_404(conn, ex_id)
        problems = logic.confirm_manifest(conn, ex_id, editor, submitted)
        if problems:
            conn.rollback()  # 维持停供
            return redirect(f"/exhibitions/{ex_id}", err="；".join(problems))
        conn.commit()
    finally:
        conn.close()
    return redirect(f"/exhibitions/{ex_id}", msg=f"编辑 {editor} 已确认清单，下载恢复")


@app.get("/exhibitions/{ex_id}/manifest.csv")
def download_manifest(ex_id: int):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        get_exhibition_or_404(conn, ex_id)
        st = logic.exhibition_status(conn, ex_id)
        if not st["downloadable"]:
            conn.rollback()
            return JSONResponse(status_code=409, content={
                "detail": "展图停供中：存在缺授权或署名未确认的原图",
                "missing": st["missing"],
                "unconfirmed": st["unconfirmed"],
            })
        items = st["items"]
        conn.commit()
    finally:
        conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["原图号", "授权号", "署名"])
    for it in items:
        writer.writerow([it["original_no"], it["license_no"], it["attribution"]])
    content = "﻿" + output.getvalue()  # BOM，便于 Excel 打开
    return Response(
        content, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="manifest_{ex_id}.csv"'})


# ---------------------------------------------------------------- JSON API

@app.get("/api/exhibitions")
def api_exhibitions():
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        logic.sync_all(conn)
        rows = conn.execute("SELECT * FROM exhibitions ORDER BY exhibition_no").fetchall()
        result = []
        for ex in rows:
            st = logic.exhibition_status(conn, ex["id"])
            result.append({
                "id": ex["id"], "exhibition_no": ex["exhibition_no"], "title": ex["title"],
                "downloadable": st["downloadable"],
                "missing": st["missing"], "unconfirmed": st["unconfirmed"],
            })
        conn.commit()
        return result
    finally:
        conn.close()


@app.get("/api/exhibitions/{ex_id}")
def api_exhibition(ex_id: int):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ex = get_exhibition_or_404(conn, ex_id)
        st = logic.exhibition_status(conn, ex_id)
        conn.commit()
    finally:
        conn.close()
    return {
        "id": ex["id"], "exhibition_no": ex["exhibition_no"], "title": ex["title"],
        "downloadable": st["downloadable"],
        "missing": st["missing"], "unconfirmed": st["unconfirmed"],
        "items": [{
            "original_id": it["original_id"], "original_no": it["original_no"],
            "license_no": it["license_no"], "attribution": it["attribution"],
            "license_valid": it["license_valid"], "confirmed": bool(it["confirmed"]),
        } for it in st["items"]],
    }


@app.get("/api/licenses")
def api_licenses():
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        logic.sync_all(conn)
        rows = conn.execute(
            "SELECT l.id, l.license_no, o.original_no, l.commercial, l.attribution,"
            " l.revoked, l.revoked_at, l.batch_label,"
            " GROUP_CONCAT(DISTINCT e.exhibition_no) AS used_by"
            " FROM licenses l"
            " JOIN originals o ON o.id = l.original_id"
            " LEFT JOIN manifest_items mi ON mi.license_id = l.id"
            " LEFT JOIN exhibitions e ON e.id = mi.exhibition_id"
            " GROUP BY l.id ORDER BY l.id"
        ).fetchall()
        conn.commit()
        return [dict(r) for r in rows]
    finally:
        conn.close()
